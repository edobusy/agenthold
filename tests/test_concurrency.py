"""Tests for multi-process / multi-connection correctness.

These tests open multiple StateStore instances against the same on-disk
database file to simulate the real MCP deployment model where each agent
spawns its own agenthold process sharing a single SQLite file.
"""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from agenthold.exceptions import BusyError, ConflictError
from agenthold.server import _dispatch
from agenthold.store import StateStore


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return a temporary database file path for multi-connection tests."""
    return tmp_path / "test.db"


# ---------------------------------------------------------------------------
# Item 1 — BEGIN IMMEDIATE correctness
# ---------------------------------------------------------------------------


def test_concurrent_writes_same_key_one_gets_conflict(db_path: Path) -> None:
    """Two connections write the same key with expected_version=1.

    Exactly one must succeed and the other must get ConflictError.
    The final version must be 2, not a corrupted duplicate.
    """
    store_a = StateStore(db_path)
    store_b = StateStore(db_path)

    # Seed the key at version 1
    store_a.set("ns", "key", "initial", updated_by="setup")

    results: dict[str, str] = {}  # "a"/"b" → "ok"/"conflict"
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer(store: StateStore, label: str) -> None:
        try:
            barrier.wait(timeout=5)
            store.set(
                "ns",
                "key",
                f"written-by-{label}",
                updated_by=f"agent-{label}",
                expected_version=1,
            )
            results[label] = "ok"
        except ConflictError:
            results[label] = "conflict"
        except Exception as e:
            errors.append(e)

    t_a = threading.Thread(target=writer, args=(store_a, "a"))
    t_b = threading.Thread(target=writer, args=(store_b, "b"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not errors, f"Unexpected errors: {errors}"
    assert sorted(results.values()) == ["conflict", "ok"], (
        f"Expected one ok and one conflict, got: {results}"
    )

    # Final version must be exactly 2
    record = store_a.get("ns", "key")
    assert record.version == 2

    store_a.close()
    store_b.close()


def test_concurrent_writes_different_keys_both_succeed(db_path: Path) -> None:
    """Two connections writing to different keys must both succeed.

    BEGIN IMMEDIATE serialises writers at the SQLite level, but the
    second writer should succeed after the first commits because the
    keys don't conflict at the OCC level.
    """
    store_a = StateStore(db_path)
    store_b = StateStore(db_path)

    results: dict[str, int] = {}
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def writer(store: StateStore, key: str, label: str) -> None:
        try:
            barrier.wait(timeout=5)
            result = store.set("ns", key, f"value-{label}", updated_by=f"agent-{label}")
            results[label] = result.version
        except Exception as e:
            errors.append(e)

    t_a = threading.Thread(target=writer, args=(store_a, "key-a", "a"))
    t_b = threading.Thread(target=writer, args=(store_b, "key-b", "b"))
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not errors, f"Unexpected errors: {errors}"
    assert results["a"] == 1
    assert results["b"] == 1

    store_a.close()
    store_b.close()


# ---------------------------------------------------------------------------
# Item 2 — _read_transaction does not block writers
# ---------------------------------------------------------------------------


def test_export_does_not_block_concurrent_write(db_path: Path) -> None:
    """An export (read transaction) must not block writes from another
    connection in WAL mode.

    Two connections run concurrently: one exports, the other writes.
    Both must complete without error. In WAL mode, a DEFERRED read
    transaction (export) does not block an IMMEDIATE write transaction.
    """
    store_a = StateStore(db_path)
    store_b = StateStore(db_path)

    # Seed enough data that the export reads multiple rows
    for i in range(50):
        store_a.set("ns", f"key-{i:03d}", f"value-{i}", updated_by="setup")

    export_result: list[int] = []
    write_result: list[int] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def exporter() -> None:
        try:
            barrier.wait(timeout=5)
            _, entries = store_a.export_namespace("ns")
            export_result.append(len(entries))
        except Exception as e:
            errors.append(e)

    def writer() -> None:
        try:
            barrier.wait(timeout=5)
            result = store_b.set("ns", "new-key", "new-value", updated_by="writer")
            write_result.append(result.version)
        except Exception as e:
            errors.append(e)

    t_export = threading.Thread(target=exporter)
    t_write = threading.Thread(target=writer)
    t_export.start()
    t_write.start()
    t_export.join(timeout=15)
    t_write.join(timeout=15)

    assert not errors, f"Unexpected errors: {errors}"
    assert len(export_result) == 1, "Export did not complete"
    assert len(write_result) == 1, "Write did not complete"
    assert write_result[0] == 1  # new key, first version

    store_a.close()
    store_b.close()


# ---------------------------------------------------------------------------
# Item 3 — busy_timeout pragma
# ---------------------------------------------------------------------------


def test_busy_timeout_pragma_is_set(db_path: Path) -> None:
    """The busy_timeout pragma must be set to 5000 ms."""
    store = StateStore(db_path)
    row = store._conn.execute("PRAGMA busy_timeout").fetchone()
    assert row is not None
    assert row[0] == 5000
    store.close()


def test_busy_error_from_store_raises_busy_error(db_path: Path) -> None:
    """When BEGIN IMMEDIATE fails with 'database is locked', BusyError
    must be raised instead of a raw sqlite3.OperationalError."""
    import sqlite3

    store = StateStore(db_path)
    store.set("ns", "k", "v", updated_by="a")

    # sqlite3.Connection.execute is a C-level read-only attribute and
    # cannot be patched. Instead, swap store._conn with a wrapper that
    # raises OperationalError on BEGIN IMMEDIATE.
    real_conn = store._conn

    class LockedConn:
        """Proxy that raises 'database is locked' on BEGIN IMMEDIATE."""

        def execute(self, sql: str, *args: object) -> object:
            if "BEGIN IMMEDIATE" in sql:
                raise sqlite3.OperationalError("database is locked")
            return real_conn.execute(sql, *args)  # type: ignore[return-value]

        def __getattr__(self, name: str) -> object:
            return getattr(real_conn, name)

    store._conn = LockedConn()  # type: ignore[assignment]
    with pytest.raises(BusyError):
        store.set("ns", "k", "v2", updated_by="b")

    store._conn = real_conn  # restore for clean close
    store.close()


def test_dispatch_busy_error_returns_structured_response() -> None:
    """When the store raises BusyError, _dispatch must return a
    structured response with status='busy' and a hint."""
    store = StateStore(":memory:")
    store.set("ns", "k", "v", updated_by="a")

    with patch.object(
        store,
        "set",
        side_effect=BusyError(),
    ):
        result = _dispatch(
            store,
            "agenthold_set",
            {
                "namespace": "ns",
                "key": "k",
                "value": "v2",
                "updated_by": "b",
                "expected_version": 1,
            },
        )
    assert result["status"] == "busy"
    assert "hint" in result
    assert "message" in result


# ---------------------------------------------------------------------------
# Item 4 — thread-safe close (existing test covers happy path;
# this verifies the lock is held)
# ---------------------------------------------------------------------------


def test_close_acquires_lock(db_path: Path) -> None:
    """close() must acquire self._lock before closing the connection."""
    store = StateStore(db_path)
    # Acquire the lock externally — close() should block until released
    acquired = threading.Event()
    closed = threading.Event()

    def close_thread() -> None:
        acquired.wait(timeout=5)
        store.close()
        closed.set()

    t = threading.Thread(target=close_thread)
    t.start()

    with store._lock:
        acquired.set()
        # Give the close thread time to attempt the lock
        assert not closed.wait(timeout=0.3), (
            "close() completed while lock was held — it did not acquire the lock"
        )
    # Lock released — close should now complete
    assert closed.wait(timeout=5), "close() did not complete after lock released"
    t.join(timeout=5)
