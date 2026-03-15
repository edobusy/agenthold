"""Conflict-focused tests for agenthold.

These tests specifically exercise the optimistic concurrency patterns
that are the core value proposition of agenthold.
"""

from datetime import datetime

import pytest

from agenthold.exceptions import ConflictError, NotFoundError
from agenthold.models import ConflictDetail, SetResult
from agenthold.store import StateStore

# ---------------------------------------------------------------------------
# Core conflict scenarios
# ---------------------------------------------------------------------------


def test_read_modify_write_succeeds_when_no_concurrent_writer(
    store: StateStore,
) -> None:
    """The happy path : read, modify, write back with expected_version."""
    store.set("ns", "counter", 10, updated_by="system")

    record = store.get("ns", "counter")
    new_value = record.value + 5

    result = store.set(
        "ns",
        "counter",
        new_value,
        updated_by="agent-a",
        expected_version=record.version,
    )
    assert isinstance(result, SetResult)
    assert result.version == 2

    final = store.get("ns", "counter")
    assert final.value == 15


def test_concurrent_writers_one_loses_with_conflict_error(store: StateStore) -> None:
    """Two agents read the same version; only the first writer succeeds."""
    store.set("ns", "status", "open", updated_by="system")

    # Both agents read version 1
    read_a = store.get("ns", "status")
    read_b = store.get("ns", "status")
    assert read_a.version == read_b.version == 1

    # agent-a writes first : succeeds
    result = store.set(
        "ns",
        "status",
        "processing",
        updated_by="agent-a",
        expected_version=1,
    )
    assert result.version == 2

    # agent-b tries with stale version : conflict
    with pytest.raises(ConflictError) as exc_info:
        store.set(
            "ns",
            "status",
            "shipped",
            updated_by="agent-b",
            expected_version=1,
        )
    assert exc_info.value.detail.expected_version == 1
    assert exc_info.value.detail.actual_version == 2
    assert exc_info.value.detail.updated_by == "agent-a"

    # Original value should be what agent-a wrote, not agent-b
    current = store.get("ns", "status")
    assert current.value == "processing"
    assert current.updated_by == "agent-a"


def test_conflict_retry_pattern_converges(store: StateStore) -> None:
    """
    Demonstrates the intended conflict resolution pattern:
    read -> modify -> write with expected_version -> retry on conflict.
    This is the same pattern as optimistic locking in relational databases.
    """
    # Initial state
    store.set("order", "total", 100, updated_by="system")

    # agent-a reads version 1 and plans to add 50
    stale_read = store.get("order", "total")
    assert stale_read.version == 1
    assert stale_read.value == 100

    # agent-b writes concurrently, changing total to 150
    store.set("order", "total", 150, updated_by="agent-b", expected_version=1)

    # agent-a now enters a retry loop. Its first attempt uses the stale read
    # and will fail. The retry re-reads the current state and succeeds.
    attempts = 0
    written = False
    version = stale_read.version
    value = stale_read.value
    while not written:
        assert attempts <= 10, f"Retry loop did not converge after {attempts} attempts"
        attempts += 1
        new_total = value + 50  # add 50 to current value
        try:
            store.set(
                "order",
                "total",
                new_total,
                updated_by="agent-a",
                expected_version=version,
            )
            written = True
        except ConflictError:
            # Re-read and retry
            record = store.get("order", "total")
            version = record.version
            value = record.value

    # Should have taken exactly 2 attempts (first failed, second succeeded)
    assert attempts == 2
    # Final total should be 150 + 50 = 200
    final = store.get("order", "total")
    assert final.value == 200
    assert final.version == 3


def test_unconditional_write_never_conflicts(store: StateStore) -> None:
    """Writes without expected_version bypass conflict detection entirely."""
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b", expected_version=1)

    # Unconditional write succeeds regardless of current version
    result = store.set("ns", "key", "v3", updated_by="c")
    assert result.version == 3

    # Even after many writes, unconditional still works
    for i in range(10):
        result = store.set("ns", "key", f"v{i + 4}", updated_by="d")
    assert result.version == 13


# ---------------------------------------------------------------------------
# Conflict error detail validation
# ---------------------------------------------------------------------------


def test_conflict_message_contains_hint_for_resolution(store: StateStore) -> None:
    """The ConflictError message should contain enough context to debug."""
    store.set("order-1234", "status", "pending", updated_by="agent-a")
    store.set("order-1234", "status", "shipped", updated_by="agent-b")

    with pytest.raises(ConflictError) as exc_info:
        store.set(
            "order-1234",
            "status",
            "cancelled",
            updated_by="agent-c",
            expected_version=1,
        )

    message = str(exc_info.value)
    # Message should identify the key, expected vs actual version, and who wrote
    assert "order-1234" in message
    assert "status" in message
    assert "expected 1" in message
    assert "got 2" in message
    assert "agent-b" in message


def test_conflict_error_detail_is_conflict_detail_model(store: StateStore) -> None:
    """The ConflictError.detail should be a proper ConflictDetail Pydantic model."""
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b")

    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "v3", updated_by="c", expected_version=1)

    detail = exc_info.value.detail
    assert isinstance(detail, ConflictDetail)
    assert detail.namespace == "ns"
    assert detail.key == "key"
    assert detail.expected_version == 1
    assert detail.actual_version == 2
    assert detail.updated_by == "b"
    assert isinstance(detail.updated_at, datetime)


# ---------------------------------------------------------------------------
# Version integrity
# ---------------------------------------------------------------------------


def test_version_never_decreases(store: StateStore) -> None:
    """Versions must monotonically increase regardless of write pattern."""
    prev_version = 0
    for i in range(20):
        result = store.set("ns", "key", f"v{i}", updated_by="a")
        assert result.version > prev_version
        prev_version = result.version


def test_deleted_key_restarts_at_version_1(store: StateStore) -> None:
    """After deletion, re-creating the same key starts fresh at version 1."""
    store.set("ns", "key", "original", updated_by="a")
    store.set("ns", "key", "updated", updated_by="a")
    assert store.get("ns", "key").version == 2

    store.delete("ns", "key", deleted_by="operator")

    result = store.set("ns", "key", "reborn", updated_by="b")
    assert result.version == 1
    assert result.previous_version is None

    record = store.get("ns", "key")
    assert record.value == "reborn"
    assert record.version == 1
    assert record.updated_by == "b"


# ---------------------------------------------------------------------------
# Additional coverage : edge cases not in the 7 listed tests
# ---------------------------------------------------------------------------


def test_conflict_on_nonexistent_key_with_expected_version(store: StateStore) -> None:
    """Passing expected_version for a key that doesn't exist raises ConflictError."""
    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "value", updated_by="a", expected_version=1)
    # actual_version is 0 because the key doesn't exist
    assert exc_info.value.detail.actual_version == 0
    assert exc_info.value.detail.expected_version == 1


def test_conflict_on_nonexistent_key_updated_by_descriptive(store: StateStore) -> None:
    """When a key doesn't exist, updated_by must not be an empty string."""
    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "value", updated_by="a", expected_version=1)
    assert exc_info.value.detail.updated_by == "(key does not exist)"
    assert exc_info.value.detail.updated_by != ""


def test_expected_version_zero_succeeds_for_new_key(store: StateStore) -> None:
    """expected_version=0 is a safe 'create-only' pattern for new keys."""
    result = store.set("ns", "key", "value", updated_by="a", expected_version=0)
    assert result.version == 1


def test_expected_version_zero_fails_for_existing_key(store: StateStore) -> None:
    """expected_version=0 rejects writes to keys that already exist."""
    store.set("ns", "key", "v1", updated_by="a")
    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "v2", updated_by="b", expected_version=0)
    assert exc_info.value.detail.actual_version == 1


def test_get_after_delete_raises_not_found(store: StateStore) -> None:
    """A deleted key is truly gone from live state."""
    store.set("ns", "key", "value", updated_by="a")
    store.delete("ns", "key", deleted_by="operator")
    with pytest.raises(NotFoundError):
        store.get("ns", "key")


def test_history_preserved_after_delete(store: StateStore) -> None:
    """Deleting a key removes it from live state but history is preserved."""
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b")
    store.delete("ns", "key", deleted_by="operator")

    # History now has 3 entries: delete tombstone + 2 prior writes
    history = store.history("ns", "key")
    assert len(history) == 3
    assert history[0].event_type == "delete"
    assert history[1].version == 2
    assert history[2].version == 1


def test_multiple_namespaces_conflict_independently(store: StateStore) -> None:
    """Conflicts are scoped to namespace/key pairs, not global."""
    store.set("ns-a", "key", "v1", updated_by="a")
    store.set("ns-b", "key", "v1", updated_by="a")

    # Writing to ns-a should not affect ns-b's version tracking
    store.set("ns-a", "key", "v2", updated_by="b", expected_version=1)

    # ns-b should still be at version 1
    result = store.set("ns-b", "key", "v2", updated_by="b", expected_version=1)
    assert result.version == 2


def test_rapid_sequential_writes_maintain_version_integrity(store: StateStore) -> None:
    """Many rapid writes to the same key maintain correct version sequence."""
    for i in range(50):
        result = store.set("ns", "counter", i, updated_by=f"agent-{i % 3}")
        assert result.version == i + 1

    record = store.get("ns", "counter")
    assert record.version == 50
    assert record.value == 49

    history = store.history("ns", "counter", limit=50)
    assert len(history) == 50
    # Versions should be 50, 49, ..., 1
    assert [h.version for h in history] == list(range(50, 0, -1))
