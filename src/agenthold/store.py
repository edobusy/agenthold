"""
Storage layer for agenthold.

Schema:
  state_records : current live state, one row per namespace/key
  state_history : append-only audit log; includes both writes and delete tombstones

Write order (atomic within a transaction):
  1. INSERT into state_history (event_type='write')
  2. INSERT OR REPLACE into state_records

Delete order (atomic within a transaction):
  1. INSERT tombstone into state_history (event_type='delete', value=JSON null)
  2. DELETE from state_records

Conflict detection uses optimistic concurrency control (OCC):
  The caller passes expected_version. If the stored version differs,
  we raise ConflictError without writing. The caller re-reads and retries.
  This is the same pattern as Postgres's UPDATE ... WHERE version = N
  and DynamoDB's conditional writes.
"""

import json
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from agenthold.exceptions import ConflictError, NotFoundError
from agenthold.models import (
    ConflictDetail,
    SetResult,
    StateRecord,
    StateRecordHistory,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS state_records (
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (namespace, key)
);

CREATE TABLE IF NOT EXISTS state_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace   TEXT NOT NULL,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    version     INTEGER NOT NULL,
    updated_by  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    event_type  TEXT NOT NULL DEFAULT 'write'
);

CREATE INDEX IF NOT EXISTS idx_history_ns_key
    ON state_history (namespace, key);
"""


class StateStore:
    """SQLite-backed state store with optimistic concurrency control.

    Thread safety:
      All public methods acquire self._lock. Writes use _transaction() which
      issues explicit BEGIN/COMMIT. Reads acquire the lock but run as autocommit
      statements — single-statement SQLite reads are atomic and do not need an
      explicit transaction. Any future method with multiple read statements must
      use _transaction().

    WAL mode:
      WAL is set for forward-compatibility. The current single-lock model
      serialises all reads and writes, so concurrent-reader performance is not
      realised yet. Splitting into separate read and write locks is a future
      improvement.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit on; BEGIN/COMMIT managed explicitly
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        # Migration: add event_type to databases that predate this column.
        try:
            self._conn.execute(
                "ALTER TABLE state_history "
                "ADD COLUMN event_type TEXT NOT NULL DEFAULT 'write'"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists — db already migrated or just created

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _now(self) -> str:
        return datetime.now(UTC).isoformat()

    def get(self, namespace: str, key: str) -> StateRecord:
        """Return the current state record. Raises NotFoundError if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM state_records WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()
        if row is None:
            raise NotFoundError(namespace, key)
        return StateRecord(
            namespace=row["namespace"],
            key=row["key"],
            value=json.loads(row["value"]),
            version=row["version"],
            updated_by=row["updated_by"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def set(
        self,
        namespace: str,
        key: str,
        value: object,
        updated_by: str,
        expected_version: int | None = None,
    ) -> SetResult:
        """
        Write a value.

        If expected_version is provided and does not match the stored version,
        raises ConflictError. The caller should re-read and retry.

        If expected_version is None, the write is unconditional (use for
        first writes or deliberate overwrites).
        """
        value_json = json.dumps(value)
        now = self._now()

        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT version, updated_by, updated_at "
                "FROM state_records WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()

            current_version = existing["version"] if existing else 0
            previous_version = current_version if existing else None

            if expected_version is not None and expected_version != current_version:
                raise ConflictError(
                    ConflictDetail(
                        namespace=namespace,
                        key=key,
                        expected_version=expected_version,
                        actual_version=current_version,
                        updated_by=(
                            existing["updated_by"]
                            if existing
                            else "(key does not exist)"
                        ),
                        updated_at=datetime.fromisoformat(existing["updated_at"])
                        if existing
                        else datetime.now(UTC),
                    )
                )

            new_version = current_version + 1

            # 1. Append to history first
            conn.execute(
                "INSERT INTO state_history "
                "(namespace, key, value, version, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (namespace, key, value_json, new_version, updated_by, now),
            )

            # 2. Upsert live record
            conn.execute(
                "INSERT INTO state_records "
                "(namespace, key, value, version, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(namespace, key) DO UPDATE SET "
                "value=excluded.value, version=excluded.version, "
                "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (namespace, key, value_json, new_version, updated_by, now),
            )

        return SetResult(
            namespace=namespace,
            key=key,
            version=new_version,
            previous_version=previous_version,
        )

    def list_keys(self, namespace: str) -> list[StateRecord]:
        """Return all current records in a namespace."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_records WHERE namespace = ? ORDER BY key",
                (namespace,),
            ).fetchall()
        return [
            StateRecord(
                namespace=r["namespace"],
                key=r["key"],
                value=json.loads(r["value"]),
                version=r["version"],
                updated_by=r["updated_by"],
                updated_at=datetime.fromisoformat(r["updated_at"]),
            )
            for r in rows
        ]

    def history(
        self, namespace: str, key: str, limit: int = 10
    ) -> list[StateRecordHistory]:
        """Return the last N history entries for a record, newest first.

        Entries include both writes (event_type='write') and delete tombstones
        (event_type='delete'). Ordered by insertion id descending rather than
        version descending — tombstones share the version of the live record
        they deleted, so id is the only unambiguous ordering.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_history "
                "WHERE namespace = ? AND key = ? "
                "ORDER BY id DESC LIMIT ?",
                (namespace, key, limit),
            ).fetchall()
        return [
            StateRecordHistory(
                namespace=r["namespace"],
                key=r["key"],
                value=json.loads(r["value"]),
                version=r["version"],
                updated_by=r["updated_by"],
                updated_at=datetime.fromisoformat(r["updated_at"]),
                event_type=r["event_type"],
            )
            for r in rows
        ]

    def delete(
        self,
        namespace: str,
        key: str,
        deleted_by: str,
        expected_version: int | None = None,
    ) -> int | None:
        """Delete a record. Returns the deleted version, or None if not found.

        If expected_version is provided and does not match the stored version,
        raises ConflictError. Pass the version from a prior get() to prevent
        accidentally deleting a record that was updated since your read.

        On deletion, a tombstone entry (event_type='delete') is written to
        state_history recording deleted_by and the version that was deleted.
        The live record is removed. Prior history entries are preserved.
        """
        now = self._now()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT version, updated_by, updated_at FROM state_records "
                "WHERE namespace = ? AND key = ?",
                (namespace, key),
            ).fetchone()

            if existing is None:
                return None

            current_version: int = existing["version"]

            if expected_version is not None and expected_version != current_version:
                raise ConflictError(
                    ConflictDetail(
                        namespace=namespace,
                        key=key,
                        expected_version=expected_version,
                        actual_version=current_version,
                        updated_by=existing["updated_by"],
                        updated_at=datetime.fromisoformat(existing["updated_at"]),
                    )
                )

            # Record the deletion in history before removing the live record
            conn.execute(
                "INSERT INTO state_history "
                "(namespace, key, value, version, updated_by, updated_at, event_type) "
                "VALUES (?, ?, ?, ?, ?, ?, 'delete')",
                (
                    namespace,
                    key,
                    json.dumps(None),
                    current_version,
                    deleted_by,
                    now,
                ),
            )
            conn.execute(
                "DELETE FROM state_records WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
        return current_version

    def close(self) -> None:
        self._conn.close()
