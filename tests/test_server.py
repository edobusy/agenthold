"""Tests for the MCP server dispatch layer."""

import json

from agenthold.server import _dispatch, make_server
from agenthold.store import StateStore

EXPECTED_TOOLS = {
    "agenthold_get",
    "agenthold_set",
    "agenthold_list",
    "agenthold_history",
    "agenthold_delete",
}


# ---------------------------------------------------------------------------
# make_server smoke test (covers decorator registration)
# ---------------------------------------------------------------------------


def test_make_server_returns_server() -> None:
    server = make_server(":memory:")
    assert server is not None
    assert server.name == "agenthold"


def test_dispatch_output_is_json_serialisable() -> None:
    """_dispatch results must serialise to JSON without default=str fallback."""
    store = StateStore(":memory:")
    result = _dispatch(store, "agenthold_get", {"namespace": "ns", "key": "missing"})
    text = json.dumps(result, indent=2)  # must not raise
    parsed = json.loads(text)
    assert parsed["status"] == "not_found"


def test_dispatch_history_output_is_json_serialisable(store: StateStore) -> None:
    """History result including event_type must serialise cleanly."""
    store.set("ns", "k", "v1", updated_by="a")
    store.delete("ns", "k", deleted_by="b")
    result = _dispatch(store, "agenthold_history", {"namespace": "ns", "key": "k"})
    text = json.dumps(result, indent=2)  # must not raise
    parsed = json.loads(text)
    assert parsed["status"] == "ok"
    event_types = [entry["event_type"] for entry in parsed["history"]]
    assert event_types == ["delete", "write"]


# ---------------------------------------------------------------------------
# agenthold_get
# ---------------------------------------------------------------------------


def test_dispatch_get_existing_key(store: StateStore) -> None:
    store.set("ns", "k", "hello", updated_by="agent")
    result = _dispatch(store, "agenthold_get", {"namespace": "ns", "key": "k"})
    assert result["status"] == "ok"
    assert result["value"] == "hello"
    assert result["version"] == 1
    assert result["namespace"] == "ns"
    assert result["key"] == "k"
    assert result["updated_by"] == "agent"
    assert "updated_at" in result


def test_dispatch_get_missing_key(store: StateStore) -> None:
    result = _dispatch(store, "agenthold_get", {"namespace": "ns", "key": "missing"})
    assert result["status"] == "not_found"
    assert result["namespace"] == "ns"
    assert result["key"] == "missing"


# ---------------------------------------------------------------------------
# agenthold_set
# ---------------------------------------------------------------------------


def test_dispatch_set_new_key(store: StateStore) -> None:
    result = _dispatch(
        store,
        "agenthold_set",
        {"namespace": "ns", "key": "k", "value": 42, "updated_by": "agent"},
    )
    assert result["status"] == "ok"
    assert result["version"] == 1
    assert result["previous_version"] is None
    assert result["namespace"] == "ns"
    assert result["key"] == "k"


def test_dispatch_set_with_correct_expected_version(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    result = _dispatch(
        store,
        "agenthold_set",
        {
            "namespace": "ns",
            "key": "k",
            "value": "v2",
            "updated_by": "agent",
            "expected_version": 1,
        },
    )
    assert result["status"] == "ok"
    assert result["version"] == 2
    assert result["previous_version"] == 1


def test_dispatch_set_conflict(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent-a")
    result = _dispatch(
        store,
        "agenthold_set",
        {
            "namespace": "ns",
            "key": "k",
            "value": "v2",
            "updated_by": "agent-b",
            "expected_version": 0,
        },
    )
    assert result["status"] == "conflict"
    assert result["expected_version"] == 0
    assert result["actual_version"] == 1
    assert result["actual_updated_by"] == "agent-a"
    assert "actual_updated_at" in result
    assert "hint" in result
    assert "message" in result


# ---------------------------------------------------------------------------
# agenthold_list
# ---------------------------------------------------------------------------


def test_dispatch_list_empty_namespace(store: StateStore) -> None:
    result = _dispatch(store, "agenthold_list", {"namespace": "empty"})
    assert result["status"] == "ok"
    assert result["count"] == 0
    assert result["records"] == []


def test_dispatch_list_with_records(store: StateStore) -> None:
    store.set("ns", "k1", "v1", updated_by="agent")
    store.set("ns", "k2", "v2", updated_by="agent")
    result = _dispatch(store, "agenthold_list", {"namespace": "ns"})
    assert result["status"] == "ok"
    assert result["count"] == 2
    keys = {r["key"] for r in result["records"]}
    assert keys == {"k1", "k2"}
    for r in result["records"]:
        assert "value" in r
        assert "version" in r
        assert "updated_by" in r
        assert "updated_at" in r


# ---------------------------------------------------------------------------
# agenthold_history
# ---------------------------------------------------------------------------


def test_dispatch_history_returns_versions(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    store.set("ns", "k", "v2", updated_by="agent")
    result = _dispatch(store, "agenthold_history", {"namespace": "ns", "key": "k"})
    assert result["status"] == "ok"
    assert result["namespace"] == "ns"
    assert result["key"] == "k"
    assert len(result["history"]) == 2
    assert result["history"][0]["version"] == 2  # newest first


def test_dispatch_history_with_limit(store: StateStore) -> None:
    for i in range(5):
        store.set("ns", "k", f"v{i}", updated_by="agent")
    result = _dispatch(
        store, "agenthold_history", {"namespace": "ns", "key": "k", "limit": 3}
    )
    assert result["status"] == "ok"
    assert len(result["history"]) == 3


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------


def test_dispatch_unknown_tool(store: StateStore) -> None:
    result = _dispatch(store, "unknown_tool", {})
    assert result["status"] == "error"
    assert "unknown_tool" in result["message"]


# ---------------------------------------------------------------------------
# Tool registration drift guard
# ---------------------------------------------------------------------------


def test_all_expected_tools_are_handled_by_dispatch() -> None:
    """Every name in EXPECTED_TOOLS must have a handler in _dispatch.

    Guards against adding a tool to list_tools() but forgetting _dispatch,
    or vice versa.
    """
    store = StateStore(":memory:")
    store.set("ns", "k", "v", updated_by="a")

    # Minimal valid args that should not return status='error' for each tool
    valid_calls: dict[str, dict[str, object]] = {
        "agenthold_get": {"namespace": "ns", "key": "k"},
        "agenthold_set": {
            "namespace": "ns",
            "key": "k2",
            "value": 1,
            "updated_by": "a",
        },
        "agenthold_list": {"namespace": "ns"},
        "agenthold_history": {"namespace": "ns", "key": "k"},
        "agenthold_delete": {"namespace": "ns", "key": "k", "deleted_by": "a"},
    }

    assert valid_calls.keys() == EXPECTED_TOOLS, (
        "valid_calls and EXPECTED_TOOLS are out of sync — update this test"
    )

    for tool_name, args in valid_calls.items():
        result = _dispatch(store, tool_name, args)
        assert result.get("status") != "error", (
            f"Tool '{tool_name}' returned status='error': {result}"
        )


# ---------------------------------------------------------------------------
# agenthold_delete
# ---------------------------------------------------------------------------


def test_dispatch_delete_existing_key(store: StateStore) -> None:
    store.set("ns", "k", "value", updated_by="a")
    result = _dispatch(
        store,
        "agenthold_delete",
        {"namespace": "ns", "key": "k", "deleted_by": "cleanup"},
    )
    assert result["status"] == "ok"
    assert result["namespace"] == "ns"
    assert result["key"] == "k"
    assert result["deleted_by"] == "cleanup"
    assert result["deleted_version"] == 1


def test_dispatch_delete_missing_key(store: StateStore) -> None:
    result = _dispatch(
        store,
        "agenthold_delete",
        {"namespace": "ns", "key": "missing", "deleted_by": "cleanup"},
    )
    assert result["status"] == "not_found"
    assert result["namespace"] == "ns"
    assert result["key"] == "missing"


def test_dispatch_delete_conflict(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="a")
    store.set("ns", "k", "v2", updated_by="a")
    result = _dispatch(
        store,
        "agenthold_delete",
        {"namespace": "ns", "key": "k", "deleted_by": "b", "expected_version": 1},
    )
    assert result["status"] == "conflict"
    assert result["expected_version"] == 1
    assert result["actual_version"] == 2
    assert "hint" in result
    assert "message" in result


def test_dispatch_delete_tombstone_visible_in_history(store: StateStore) -> None:
    store.set("ns", "k", "value", updated_by="a")
    _dispatch(
        store,
        "agenthold_delete",
        {"namespace": "ns", "key": "k", "deleted_by": "cleanup"},
    )
    result = _dispatch(store, "agenthold_history", {"namespace": "ns", "key": "k"})
    assert result["history"][0]["event_type"] == "delete"
    assert result["history"][0]["updated_by"] == "cleanup"
