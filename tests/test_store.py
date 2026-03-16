"""Unit tests for the StateStore."""

from datetime import datetime

import pytest

from agenthold.exceptions import ConflictError, NotFoundError
from agenthold.models import SetResult, StateRecord, StateRecordHistory
from agenthold.store import StateStore

# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_existing_key(populated_store: StateStore) -> None:
    record = populated_store.get("ns-a", "key1")
    assert isinstance(record, StateRecord)
    assert record.namespace == "ns-a"
    assert record.key == "key1"
    assert record.value == "value1"
    assert record.version == 1
    assert record.updated_by == "agent-a"
    assert isinstance(record.updated_at, datetime)


def test_get_missing_key_raises_not_found(store: StateStore) -> None:
    with pytest.raises(NotFoundError):
        store.get("ns", "nonexistent")


def test_get_returns_correct_version(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="a")
    record = store.get("ns", "key")
    assert isinstance(record, StateRecord)
    assert record.version == 2
    assert record.updated_by == "a"


def test_get_returns_correct_value_types(store: StateStore) -> None:
    """Verify round-trip for string, int, list, dict, bool, and None."""
    cases = [
        ("string", "hello"),
        ("int", 42),
        ("list", [1, 2, 3]),
        ("dict", {"nested": True}),
        ("bool", True),
        ("none", None),
    ]
    for key, value in cases:
        store.set("ns", key, value, updated_by="a")
        record = store.get("ns", key)
        assert record.value == value, f"Failed for key={key}"


# ---------------------------------------------------------------------------
# set : first write
# ---------------------------------------------------------------------------


def test_set_new_key_returns_version_1(store: StateStore) -> None:
    result = store.set("ns", "key", "value", updated_by="a")
    assert isinstance(result, SetResult)
    assert result.version == 1
    assert result.namespace == "ns"
    assert result.key == "key"


def test_set_new_key_no_expected_version_succeeds(store: StateStore) -> None:
    result = store.set("ns", "key", "value", updated_by="a")
    assert result.version == 1


def test_set_new_key_previous_version_is_none(store: StateStore) -> None:
    result = store.set("ns", "key", "value", updated_by="a")
    assert result.previous_version is None


# ---------------------------------------------------------------------------
# set : subsequent writes
# ---------------------------------------------------------------------------


def test_set_existing_key_increments_version(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    result = store.set("ns", "key", "v2", updated_by="a")
    assert result.version == 2
    assert result.previous_version == 1


def test_set_with_correct_expected_version_succeeds(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    result = store.set("ns", "key", "v2", updated_by="a", expected_version=1)
    assert result.version == 2


def test_set_with_wrong_expected_version_raises_conflict(store: StateStore) -> None:
    store.set("ns", "key", "first", updated_by="agent-a")
    store.set("ns", "key", "second", updated_by="agent-b")
    # agent-a still thinks version is 1, but it is now 2
    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "third", updated_by="agent-a", expected_version=1)
    assert exc_info.value.detail.expected_version == 1
    assert exc_info.value.detail.actual_version == 2
    assert exc_info.value.detail.updated_by == "agent-b"


def test_set_without_expected_version_always_succeeds(store: StateStore) -> None:
    """Unconditional overwrite : no expected_version means no conflict check."""
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b")
    # Writing without expected_version should always succeed
    result = store.set("ns", "key", "v3", updated_by="c")
    assert result.version == 3


def test_set_conflict_error_contains_actual_version(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b")
    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "v3", updated_by="a", expected_version=1)
    assert exc_info.value.detail.actual_version == 2


def test_set_conflict_error_contains_who_wrote_it(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="agent-b")
    with pytest.raises(ConflictError) as exc_info:
        store.set("ns", "key", "v3", updated_by="a", expected_version=1)
    assert exc_info.value.detail.updated_by == "agent-b"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_returns_all_keys_in_namespace(populated_store: StateStore) -> None:
    records = populated_store.list_keys("ns-a")
    assert all(isinstance(r, StateRecord) for r in records)
    keys = [r.key for r in records]
    assert sorted(keys) == ["key1", "key2"]
    # Verify all fields are populated on listed records
    for r in records:
        assert isinstance(r.updated_at, datetime)
        assert r.updated_by == "agent-a"


def test_list_empty_namespace_returns_empty_list(store: StateStore) -> None:
    records = store.list_keys("nonexistent")
    assert records == []


def test_list_does_not_return_other_namespaces(populated_store: StateStore) -> None:
    records = populated_store.list_keys("ns-a")
    for r in records:
        assert r.namespace == "ns-a"


def test_list_returns_current_values_not_history(store: StateStore) -> None:
    store.set("ns", "key", "old", updated_by="a")
    store.set("ns", "key", "new", updated_by="a")
    records = store.list_keys("ns")
    assert len(records) == 1
    assert records[0].value == "new"
    assert records[0].version == 2


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_returns_newest_first(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="a")
    store.set("ns", "key", "v3", updated_by="a")
    history = store.history("ns", "key")
    assert all(isinstance(h, StateRecordHistory) for h in history)
    versions = [h.version for h in history]
    assert versions == [3, 2, 1]
    # Verify all fields are populated on history entries
    for h in history:
        assert h.namespace == "ns"
        assert h.key == "key"
        assert isinstance(h.updated_at, datetime)
        assert h.updated_by == "a"


def test_history_limit_parameter_respected(store: StateStore) -> None:
    for i in range(5):
        store.set("ns", "key", f"v{i + 1}", updated_by="a")
    history = store.history("ns", "key", limit=2)
    assert len(history) == 2
    assert history[0].version == 5
    assert history[1].version == 4


def test_history_records_every_write(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b")
    store.set("ns", "key", "v3", updated_by="a")
    history = store.history("ns", "key")
    assert len(history) == 3
    assert [h.updated_by for h in history] == ["a", "b", "a"]


def test_history_empty_for_missing_key(store: StateStore) -> None:
    """Returns empty list, does not raise."""
    history = store.history("ns", "nonexistent")
    assert history == []


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_existing_key_returns_deleted_version(store: StateStore) -> None:
    store.set("ns", "key", "value", updated_by="a")
    result = store.delete("ns", "key", deleted_by="b")
    assert result == 1


def test_delete_missing_key_returns_none(store: StateStore) -> None:
    assert store.delete("ns", "nonexistent", deleted_by="b") is None


def test_delete_removes_from_list(store: StateStore) -> None:
    store.set("ns", "key1", "v1", updated_by="a")
    store.set("ns", "key2", "v2", updated_by="a")
    store.delete("ns", "key1", deleted_by="b")
    records = store.list_keys("ns")
    keys = [r.key for r in records]
    assert keys == ["key2"]


def test_delete_writes_tombstone_to_history(store: StateStore) -> None:
    store.set("ns", "key", "value", updated_by="a")
    store.delete("ns", "key", deleted_by="b")
    history = store.history("ns", "key")
    assert len(history) == 2
    assert history[0].event_type == "delete"
    assert history[0].value is None
    assert history[1].event_type == "write"


def test_delete_tombstone_preserves_prior_history(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="b")
    store.delete("ns", "key", deleted_by="c")
    history = store.history("ns", "key", limit=10)
    event_types = [h.event_type for h in history]
    assert event_types == ["delete", "write", "write"]


def test_delete_missing_key_writes_no_tombstone(store: StateStore) -> None:
    result = store.delete("ns", "nonexistent", deleted_by="b")
    assert result is None
    history = store.history("ns", "nonexistent")
    assert history == []


def test_history_includes_event_type_field(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    history = store.history("ns", "key")
    assert history[0].event_type == "write"


def test_delete_tombstone_records_deleted_by(store: StateStore) -> None:
    store.set("ns", "key", "value", updated_by="writer")
    store.delete("ns", "key", deleted_by="remover")
    history = store.history("ns", "key")
    assert history[0].event_type == "delete"
    assert history[0].updated_by == "remover"
    assert history[0].updated_by != "writer"


def test_delete_with_correct_expected_version_succeeds(store: StateStore) -> None:
    store.set("ns", "key", "value", updated_by="a")
    result = store.delete("ns", "key", deleted_by="b", expected_version=1)
    assert result == 1
    with pytest.raises(NotFoundError):
        store.get("ns", "key")


def test_delete_with_wrong_expected_version_raises_conflict(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="a")
    with pytest.raises(ConflictError) as exc_info:
        store.delete("ns", "key", deleted_by="b", expected_version=1)
    assert exc_info.value.detail.expected_version == 1
    assert exc_info.value.detail.actual_version == 2
    # Key must still exist — conflict aborted the delete
    assert store.get("ns", "key").version == 2


def test_delete_without_expected_version_is_unconditional(store: StateStore) -> None:
    store.set("ns", "key", "v1", updated_by="a")
    store.set("ns", "key", "v2", updated_by="a")
    result = store.delete("ns", "key", deleted_by="b")
    assert result == 2  # deleted the version that was live


# ---------------------------------------------------------------------------
# value types : explicit round-trip tests
# ---------------------------------------------------------------------------


def test_set_and_get_string_value(store: StateStore) -> None:
    store.set("ns", "k", "hello world", updated_by="a")
    assert store.get("ns", "k").value == "hello world"


def test_set_and_get_integer_value(store: StateStore) -> None:
    store.set("ns", "k", 42, updated_by="a")
    assert store.get("ns", "k").value == 42


def test_set_and_get_dict_value(store: StateStore) -> None:
    data = {"status": "active", "count": 3}
    store.set("ns", "k", data, updated_by="a")
    assert store.get("ns", "k").value == data


def test_set_and_get_list_value(store: StateStore) -> None:
    data = [1, "two", 3.0]
    store.set("ns", "k", data, updated_by="a")
    assert store.get("ns", "k").value == data


def test_set_and_get_none_value(store: StateStore) -> None:
    store.set("ns", "k", None, updated_by="a")
    assert store.get("ns", "k").value is None


def test_set_and_get_boolean_value(store: StateStore) -> None:
    store.set("ns", "k", True, updated_by="a")
    assert store.get("ns", "k").value is True


def test_close_does_not_raise() -> None:
    store = StateStore(":memory:")
    store.close()  # should complete without error


# ---------------------------------------------------------------------------
# clear_namespace
# ---------------------------------------------------------------------------


def test_clear_namespace_deletes_all_keys(store: StateStore) -> None:
    store.set("ns", "k1", "v1", updated_by="a")
    store.set("ns", "k2", "v2", updated_by="a")
    store.clear_namespace("ns", deleted_by="cleanup")
    assert store.list_keys("ns") == []
    for key in ("k1", "k2"):
        with pytest.raises(NotFoundError):
            store.get("ns", key)


def test_clear_namespace_returns_deleted_keys(store: StateStore) -> None:
    # Insert in non-alphabetical order to verify ORDER BY key
    store.set("ns", "gamma", "v1", updated_by="a")
    store.set("ns", "alpha", "v2", updated_by="a")
    store.set("ns", "beta", "v3", updated_by="a")
    result = store.clear_namespace("ns", deleted_by="cleanup")
    assert result == ["alpha", "beta", "gamma"]


def test_clear_namespace_empty_namespace(store: StateStore) -> None:
    result = store.clear_namespace("nonexistent", deleted_by="cleanup")
    assert result == []


def test_clear_namespace_tombstones_in_history(store: StateStore) -> None:
    # 3 keys; k2 written twice so its version is 2, proving tombstones
    # capture the live version rather than a hardcoded value
    store.set("ns", "k1", "v1", updated_by="a")
    store.set("ns", "k2", "v1", updated_by="a")
    store.set("ns", "k2", "v2", updated_by="a")  # k2 now at version 2
    store.set("ns", "k3", "v1", updated_by="a")
    store.clear_namespace("ns", deleted_by="cleaner")
    for key in ("k1", "k2", "k3"):
        tombstone = store.history("ns", key)[0]
        assert tombstone.event_type == "delete"
        assert tombstone.updated_by == "cleaner"
        assert tombstone.value is None
    assert store.history("ns", "k1")[0].version == 1
    assert store.history("ns", "k2")[0].version == 2
    assert store.history("ns", "k3")[0].version == 1


def test_clear_namespace_does_not_affect_other_namespaces(
    store: StateStore,
) -> None:
    store.set("ns-a", "key", "value", updated_by="a")
    store.set("ns-b", "key", "value", updated_by="a")
    store.clear_namespace("ns-a", deleted_by="cleanup")
    # Live record in ns-b is unaffected
    assert store.get("ns-b", "key").value == "value"
    # No spurious tombstones in ns-b history
    history = store.history("ns-b", "key")
    assert all(h.event_type == "write" for h in history)


def test_clear_namespace_is_idempotent(store: StateStore) -> None:
    store.set("ns", "key", "value", updated_by="a")
    store.clear_namespace("ns", deleted_by="cleanup")
    result = store.clear_namespace("ns", deleted_by="cleanup")
    assert result == []
