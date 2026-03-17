"""Tests for the agenthold_watch async polling tool."""

import asyncio
import json
from unittest.mock import patch

import pytest

from agenthold.exceptions import BusyError
from agenthold.server import _watch
from agenthold.store import StateStore

# ---------------------------------------------------------------------------
# Group 1 — Immediate return (no polling needed)
# ---------------------------------------------------------------------------


async def test_watch_returns_immediately_if_already_changed(
    store: StateStore,
) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)
    assert result["status"] == "ok"
    assert result["version"] == 1


async def test_watch_returns_immediately_if_version_far_ahead(
    store: StateStore,
) -> None:
    for i in range(5):
        store.set("ns", "k", f"v{i}", updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=2, timeout_seconds=5.0)
    assert result["status"] == "ok"
    assert result["version"] == 5


async def test_watch_timeout_zero_key_changed(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=0)
    assert result["status"] == "ok"


async def test_watch_timeout_zero_key_not_changed(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=1, timeout_seconds=0)
    assert result["status"] == "timeout"


async def test_watch_timeout_zero_key_nonexistent(store: StateStore) -> None:
    result = await _watch(store, "ns", "missing", since_version=0, timeout_seconds=0)
    assert result["status"] == "timeout"


# ---------------------------------------------------------------------------
# Group 2 — Polling (write happens after watch starts)
# ---------------------------------------------------------------------------


async def test_watch_fires_when_key_written_during_wait(store: StateStore) -> None:
    task = asyncio.create_task(
        _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)
    )
    await asyncio.sleep(0.3)
    store.set("ns", "k", {"result": 42}, updated_by="agent-a")
    result = await task
    assert result["status"] == "ok"
    assert result["version"] == 1
    assert result["value"] == {"result": 42}


async def test_watch_fires_on_subsequent_write(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    task = asyncio.create_task(
        _watch(store, "ns", "k", since_version=1, timeout_seconds=5.0)
    )
    await asyncio.sleep(0.3)
    store.set("ns", "k", "v2", updated_by="agent")
    result = await task
    assert result["status"] == "ok"
    assert result["version"] == 2


async def test_watch_fires_when_key_created(store: StateStore) -> None:
    task = asyncio.create_task(
        _watch(store, "ns", "new-key", since_version=0, timeout_seconds=5.0)
    )
    await asyncio.sleep(0.3)
    store.set("ns", "new-key", "created", updated_by="creator")
    result = await task
    assert result["status"] == "ok"
    assert result["version"] == 1


async def test_watch_fires_when_multiple_writes_happen_between_polls(
    store: StateStore,
) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    task = asyncio.create_task(
        _watch(store, "ns", "k", since_version=1, timeout_seconds=5.0)
    )
    await asyncio.sleep(0.3)
    # Two writes happen quickly — watcher may miss v2 but must fire on v3
    store.set("ns", "k", "v2", updated_by="agent")
    store.set("ns", "k", "v3", updated_by="agent")
    result = await task
    assert result["status"] == "ok"
    assert result["version"] == 3  # > not ==, so intermediate version is fine


# ---------------------------------------------------------------------------
# Group 3 — Timeout
# ---------------------------------------------------------------------------


async def test_watch_times_out_when_key_never_changes(store: StateStore) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=1, timeout_seconds=0.5)
    assert result["status"] == "timeout"


async def test_watch_times_out_for_nonexistent_key_with_nonzero_since(
    store: StateStore,
) -> None:
    result = await _watch(store, "ns", "absent", since_version=5, timeout_seconds=0.5)
    assert result["status"] == "timeout"


async def test_watch_elapsed_seconds_is_plausible(store: StateStore) -> None:
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=0.5)
    assert result["status"] == "timeout"
    assert result["elapsed_seconds"] > 0.4  # loose lower bound; no upper bound


# ---------------------------------------------------------------------------
# Group 4 — Input validation
# ---------------------------------------------------------------------------


async def test_watch_negative_timeout_returns_error(store: StateStore) -> None:
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=-1)
    assert result["status"] == "error"
    assert "timeout_seconds" in result["message"]


async def test_watch_negative_since_version_returns_error(store: StateStore) -> None:
    result = await _watch(store, "ns", "k", since_version=-1, timeout_seconds=5.0)
    assert result["status"] == "error"
    assert "since_version" in result["message"]


# ---------------------------------------------------------------------------
# Group 5 — Return format
# ---------------------------------------------------------------------------


async def test_watch_ok_response_has_all_required_fields(store: StateStore) -> None:
    store.set("ns", "k", "v", updated_by="a")
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)
    assert result["status"] == "ok"
    for field in ("namespace", "key", "value", "version", "updated_by", "updated_at"):
        assert field in result, f"Missing field: {field}"


async def test_watch_timeout_response_has_all_required_fields(
    store: StateStore,
) -> None:
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=0)
    assert result["status"] == "timeout"
    for field in ("namespace", "key", "since_version", "elapsed_seconds", "hint"):
        assert field in result, f"Missing field: {field}"


async def test_watch_timeout_response_hint_is_nonempty_string(
    store: StateStore,
) -> None:
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=0)
    assert result["status"] == "timeout"
    assert isinstance(result["hint"], str)
    assert len(result["hint"]) > 0


async def test_watch_value_types_preserved(store: StateStore) -> None:
    payload = {"score": 0.92, "tags": ["a", "b"], "nested": {"x": 1}}
    store.set("ns", "k", payload, updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)
    assert result["status"] == "ok"
    assert result["value"] == payload


async def test_watch_ok_response_is_json_serialisable(store: StateStore) -> None:
    store.set("ns", "k", {"score": 0.92}, updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)
    text = json.dumps(result, indent=2)  # must not raise
    parsed = json.loads(text)
    assert parsed["status"] == "ok"


# ---------------------------------------------------------------------------
# Group 6 — Namespace and key isolation
# ---------------------------------------------------------------------------


async def test_watch_does_not_fire_on_different_key(store: StateStore) -> None:
    task = asyncio.create_task(
        _watch(store, "ns", "a", since_version=0, timeout_seconds=0.5)
    )
    await asyncio.sleep(0.1)
    store.set("ns", "b", "unrelated", updated_by="agent")
    result = await task
    assert result["status"] == "timeout"


async def test_watch_does_not_fire_on_different_namespace(store: StateStore) -> None:
    task = asyncio.create_task(
        _watch(store, "ns1", "x", since_version=0, timeout_seconds=0.5)
    )
    await asyncio.sleep(0.1)
    store.set("ns2", "x", "unrelated", updated_by="agent")
    result = await task
    assert result["status"] == "timeout"


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


async def test_watch_since_version_zero_key_already_at_version_one(
    store: StateStore,
) -> None:
    store.set("ns", "k", "v1", updated_by="agent")
    result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)
    assert result["status"] == "ok"
    assert result["version"] == 1


@pytest.mark.parametrize("ns,key", [("", "k"), ("ns", ""), ("", "")])
async def test_watch_empty_namespace_or_key_rejected(
    store: StateStore, ns: str, key: str
) -> None:
    """Empty strings are rejected by input validation."""
    result = await _watch(store, ns, key, since_version=0, timeout_seconds=0)
    assert result["status"] == "error"
    assert "must not be empty" in result["message"]


async def test_watch_survives_transient_busy_error(store: StateStore) -> None:
    """BusyError during polling should not crash — watch keeps polling."""
    store.set("ns", "k", "v1", updated_by="agent")

    real_get = store.get
    call_count = 0

    def flaky_get(namespace: str, key: str) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise BusyError()
        return real_get(namespace, key)

    with patch.object(store, "get", side_effect=flaky_get):
        result = await _watch(store, "ns", "k", since_version=0, timeout_seconds=5.0)

    assert result["status"] == "ok"
    assert result["version"] == 1
    assert call_count >= 2
