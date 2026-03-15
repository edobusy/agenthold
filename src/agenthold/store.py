"""
Storage layer for agenthold.

Schema:
  state_records : current live state, one row per namespace/key
  state_history : append-only log of every write

Write order (atomic within a transaction):
  1. INSERT into state_history
  2. INSERT OR REPLACE into state_records

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
    updated_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_ns_key
    ON state_history (namespace, key);
"""


class StateStore:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit off, we manage transactions
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

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
                        updated_by=existing["updated_by"] if existing else "",
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
        """Return the last N versions of a record, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM state_history "
                "WHERE namespace = ? AND key = ? "
                "ORDER BY version DESC LIMIT ?",
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
            )
            for r in rows
        ]

    def delete(self, namespace: str, key: str) -> bool:
        """Delete a record. Returns True if it existed, False if not."""
        with self._transaction() as conn:
            result = conn.execute(
                "DELETE FROM state_records WHERE namespace = ? AND key = ?",
                (namespace, key),
            )
        return result.rowcount > 0

    def close(self) -> None:
        self._conn.close()
