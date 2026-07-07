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

Clear order (atomic within a single transaction, all keys in a namespace):
  1. SELECT all live keys and versions for the namespace
  2. executemany INSERT tombstones into state_history for each key
  3. DELETE all rows from state_records WHERE namespace = ?

Export order (read-only, consistent snapshot, all keys in a namespace):
  1. SELECT all live keys for the namespace (ordered by key)
  2. For each live key: SELECT all history entries (ordered by id DESC, no limit)

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

from agenthold.exceptions import BusyError, ConflictError, NotFoundError
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


_MAX_IDENTIFIER_LENGTH = 512


def _validate_identifier(value: str, name: str) -> None:
    """Validate a namespace or key string."""
    if not value:
        raise ValueError(f"{name} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{name} must not contain null bytes")
    if len(value) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{name} must not exceed {_MAX_IDENTIFIER_LENGTH} characters")


class StateStore:
    """SQLite-backed state store with optimistic concurrency control.

    Thread safety:
      All public methods acquire self._lock. Writes use _transaction() which
      issues explicit BEGIN IMMEDIATE / COMMIT. Reads acquire the lock but run
      as autocommit statements — single-statement SQLite reads are atomic and
      do not need an explicit transaction. Multi-statement reads use
      _read_transaction() which issues BEGIN DEFERRED / COMMIT.

    Multi-process safety:
      _transaction() uses BEGIN IMMEDIATE so that the SQLite write lock is
      acquired before any reads. This prevents the stale-snapshot race where
      two processes both read the same version and both write version + 1.
      busy_timeout is set so a second writer waits rather than failing
      immediately when the write lock is held by another process.

    WAL mode:
      WAL is enabled to allow concurrent readers alongside one writer across
      processes. _read_transaction() (BEGIN DEFERRED) acquires only a shared
      lock and does not block writers in other processes.
    """

    def __init__(
        self, db_path: str | Path = ":memory:", *, read_only: bool = False
    ) -> None:
        self.db_path = str(db_path)
        self.read_only = read_only
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # autocommit on; BEGIN/COMMIT managed explicitly
        )
        self._conn.row_factory = sqlite3.Row
        if read_only:
            # Inspection mode: never mutate the target database. Skip the
            # journal-mode switch and all schema creation/migration DDL, and
            # set query_only so any accidental write is rejected. This reads
            # both WAL and rollback-journal databases without changing them.
            self._conn.execute("PRAGMA query_only=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            return
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
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
        """Write transaction with immediate lock acquisition.

        BEGIN IMMEDIATE acquires the SQLite reserved (write) lock before
        any reads execute. This prevents the stale-snapshot race across
        processes: a second writer blocks until the first commits, then
        reads the fresh state and detects the OCC conflict properly.

        If the write lock cannot be acquired within busy_timeout (5 s),
        sqlite3.OperationalError is caught and re-raised as BusyError.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    raise BusyError() from e
                raise
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    @contextmanager
    def _read_transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Consistent read snapshot without blocking writers.

        Uses BEGIN DEFERRED — the snapshot is established at the first
        SELECT and holds only a shared lock for the duration. Writers
        using BEGIN IMMEDIATE can proceed concurrently in WAL mode.
        """
        with self._lock:
            self._conn.execute("BEGIN DEFERRED")
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
        _validate_identifier(namespace, "namespace")
        _validate_identifier(key, "key")
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
        _validate_identifier(namespace, "namespace")
        _validate_identifier(key, "key")
        try:
            value_json = json.dumps(value)
        except TypeError as e:
            raise ValueError(f"Value is not JSON-serialisable: {e}") from e

        with self._transaction() as conn:
            now = self._now()
            existing = conn.execute(
                "SELECT version, updated_by, updated_at, value "
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
                        actual_value=json.loads(existing["value"])
                        if existing
                        else None,
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
        _validate_identifier(namespace, "namespace")
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

    def list_namespaces(self) -> list[str]:
        """Return the distinct namespaces that currently hold live records.

        Read-only; sorted alphabetically. Namespaces whose every record has been
        deleted do not appear (only live records are considered). Returns an
        empty list for an empty store.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT namespace FROM state_records ORDER BY namespace"
            ).fetchall()
        return [r["namespace"] for r in rows]

    def history(
        self, namespace: str, key: str, limit: int = 10
    ) -> list[StateRecordHistory]:
        """Return the last N history entries for a record, newest first.

        Entries include both writes (event_type='write') and delete tombstones
        (event_type='delete'). Ordered by insertion id descending rather than
        version descending — tombstones share the version of the live record
        they deleted, so id is the only unambiguous ordering.
        """
        _validate_identifier(namespace, "namespace")
        _validate_identifier(key, "key")
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
        _validate_identifier(namespace, "namespace")
        _validate_identifier(key, "key")
        with self._transaction() as conn:
            now = self._now()
            existing = conn.execute(
                "SELECT version, updated_by, updated_at, value FROM state_records "
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
                        actual_value=json.loads(existing["value"]),
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

    def clear_namespace(
        self,
        namespace: str,
        deleted_by: str,
    ) -> list[str]:
        """Delete all live records in a namespace.

        Writes a tombstone to state_history for each deleted record before
        removing the live record (history-first invariant). All changes are
        atomic within a single transaction.

        Returns a list of the key names that were deleted, sorted alphabetically.
        Returns an empty list if the namespace has no live records.
        """
        _validate_identifier(namespace, "namespace")
        with self._transaction() as conn:
            now = self._now()

            rows = conn.execute(
                "SELECT key, version FROM state_records "
                "WHERE namespace = ? ORDER BY key",
                (namespace,),
            ).fetchall()

            if not rows:
                return []

            tombstones = [
                (
                    namespace,
                    row["key"],
                    json.dumps(None),
                    row["version"],
                    deleted_by,
                    now,
                )
                for row in rows
            ]
            conn.executemany(
                "INSERT INTO state_history "
                "(namespace, key, value, version, updated_by, updated_at, event_type) "
                "VALUES (?, ?, ?, ?, ?, ?, 'delete')",
                tombstones,
            )

            conn.execute(
                "DELETE FROM state_records WHERE namespace = ?",
                (namespace,),
            )

        return [row["key"] for row in rows]

    def export_namespace(
        self,
        namespace: str,
    ) -> tuple[str, list[tuple[StateRecord, list[StateRecordHistory]]]]:
        """Return all live records and their complete history for a namespace.

        Records are sorted alphabetically by key. History entries per key are
        ordered newest first (by insertion id descending). All reads occur
        within a single transaction for a consistent snapshot.

        Issues N+1 queries (one SELECT for live records, one per key for
        history). Acceptable for the debugging/audit use case; not suitable
        for hot paths. This is the only store method that queries state_history
        without a LIMIT clause — "complete history" means all entries.

        Returns (exported_at, entries) where:
          exported_at : ISO timestamp string of when the snapshot was taken.
                        Uses the same clock source as write methods via
                        self._now(), but is not persisted to the database.
          entries     : list of (StateRecord, list[StateRecordHistory]) pairs,
                        one per live key, sorted alphabetically

        Returns (exported_at, []) if the namespace has no live records.
        """
        _validate_identifier(namespace, "namespace")
        with self._read_transaction() as conn:
            exported_at = self._now()

            live_rows = conn.execute(
                "SELECT * FROM state_records WHERE namespace = ? ORDER BY key",
                (namespace,),
            ).fetchall()

            if not live_rows:
                return exported_at, []

            entries: list[tuple[StateRecord, list[StateRecordHistory]]] = []
            for row in live_rows:
                record = StateRecord(
                    namespace=row["namespace"],
                    key=row["key"],
                    value=json.loads(row["value"]),
                    version=row["version"],
                    updated_by=row["updated_by"],
                    updated_at=datetime.fromisoformat(row["updated_at"]),
                )
                history_rows = conn.execute(
                    "SELECT * FROM state_history "
                    "WHERE namespace = ? AND key = ? "
                    "ORDER BY id DESC",
                    (namespace, row["key"]),
                ).fetchall()
                history = [
                    StateRecordHistory(
                        namespace=h["namespace"],
                        key=h["key"],
                        value=json.loads(h["value"]),
                        version=h["version"],
                        updated_by=h["updated_by"],
                        updated_at=datetime.fromisoformat(h["updated_at"]),
                        event_type=h["event_type"],
                    )
                    for h in history_rows
                ]
                entries.append((record, history))

        return exported_at, entries

    def close(self) -> None:
        with self._lock:
            self._conn.close()
