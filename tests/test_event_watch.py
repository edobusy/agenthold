"""Behavioural tests: watch/wait wake on in-process notifications, with the
polling loop remaining as the correctness fallback.

Each event test raises ``_POLL_INTERVAL`` far above the test's own timeout, so a
fast return can only be the notification (a poll would take 30 s). Each fallback
test drops the interval low and does NOT notify, proving the poll still catches
an un-notified change (the cross-process guarantee).
"""

from __future__ import annotations

import asyncio

import pytest

from agenthold import server as srv
from agenthold.notifier import KeyNotifier
from agenthold.resources import Workspace, WorkspaceRegistry
from agenthold.store import StateStore


def _coord() -> tuple[StateStore, object]:
    store = StateStore(":memory:")
    from agenthold.coordinator import Coordinator

    registry = WorkspaceRegistry([Workspace(name="default", root="/work")])
    return store, Coordinator(store, registry)


# ---------------------------------------------------------------------------
# watch
# ---------------------------------------------------------------------------


async def _wait_until_parked(notifier: KeyNotifier, key: str) -> None:
    """Block until a waiter has subscribed on ``key`` (bounded)."""
    for _ in range(200):
        if key in notifier._waiters:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"waiter never parked on {key!r}")


async def test_watch_wakes_specifically_on_notify(
    monkeypatch: pytest.MonkeyPatch, store: StateStore
) -> None:
    # Airtight isolation of the notify as the wake cause: prove the watcher is
    # parked (already read v1), then that committing v2 alone does NOT wake it
    # (poll is 30 s away), then that the notify does.
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 30.0)
    store.set("ns", "k", "v0", updated_by="a")  # version 1
    notifier = KeyNotifier()
    notify_key = srv._watch_key("ns", "k")
    task = asyncio.create_task(
        srv._watch(
            store, "ns", "k", since_version=1, timeout_seconds=30.0, notifier=notifier
        )
    )
    await _wait_until_parked(notifier, notify_key)  # read v1, subscribed
    store.set(
        "ns", "k", "v2", updated_by="a", expected_version=1
    )  # committed, no notify
    await asyncio.sleep(0.05)
    assert not task.done()  # the write alone did not wake it (poll is 30 s off)
    notifier.notify(notify_key)  # the only thing that can wake it now
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["status"] == "ok" and result["version"] == 2


async def test_watch_wakes_via_call_tool_helper(
    monkeypatch: pytest.MonkeyPatch, store: StateStore
) -> None:
    # Exercises the exact call_tool path: _dispatch(set) then _notify_after_write.
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 30.0)
    notifier = KeyNotifier()
    task = asyncio.create_task(
        srv._watch(
            store, "ns", "k", since_version=0, timeout_seconds=30.0, notifier=notifier
        )
    )
    await asyncio.sleep(0.05)
    args = {
        "namespace": "ns",
        "key": "k",
        "value": "v",
        "updated_by": "x",
        "expected_version": 0,
    }
    result_write = srv._dispatch(store, "agenthold_set", args)
    srv._notify_after_write(notifier, "agenthold_set", args, result_write)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["version"] == 1


async def test_watch_fallback_catches_unnotified_write(
    monkeypatch: pytest.MonkeyPatch, store: StateStore
) -> None:
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 0.05)
    store.set("ns", "k", "v0", updated_by="a")  # version 1
    # Default (unshared) notifier -> no external notify -> only the poll can see it.
    task = asyncio.create_task(
        srv._watch(store, "ns", "k", since_version=1, timeout_seconds=5.0)
    )
    await asyncio.sleep(0.02)
    store.set(
        "ns", "k", "v2", updated_by="a", expected_version=1
    )  # version 2, no notify
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["version"] == 2


async def test_watch_conflict_does_not_notify(
    monkeypatch: pytest.MonkeyPatch, store: StateStore
) -> None:
    # A failed (conflicting) set must not fire a watch wake.
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 30.0)
    store.set("ns", "k", "v0", updated_by="a")  # version 1
    notifier = KeyNotifier()
    task = asyncio.create_task(
        srv._watch(
            store, "ns", "k", since_version=1, timeout_seconds=0.3, notifier=notifier
        )
    )
    await asyncio.sleep(0.05)
    args = {
        "namespace": "ns",
        "key": "k",
        "value": "v",
        "updated_by": "x",
        "expected_version": 0,  # stale -> conflict
    }
    result_write = srv._dispatch(store, "agenthold_set", args)
    assert result_write["status"] == "conflict"
    srv._notify_after_write(notifier, "agenthold_set", args, result_write)
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["status"] == "timeout"  # no version change, no wake


# ---------------------------------------------------------------------------
# wait
# ---------------------------------------------------------------------------


async def test_wait_wakes_specifically_on_release_notify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Airtight: parked -> release commits but does not wake -> notify wakes.
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 30.0)
    store, coord = _coord()
    agent = coord.register(name="a")["agent_id"]  # type: ignore[attr-defined]
    coord.claim("custom://r", agent)  # type: ignore[attr-defined]
    notifier = KeyNotifier()
    notify_key = srv._wait_key(coord.canonicalize("custom://r").to_uri())  # type: ignore[attr-defined]
    task = asyncio.create_task(
        srv._wait_standard(coord, "custom://r", timeout_seconds=30.0, notifier=notifier)
    )
    await _wait_until_parked(notifier, notify_key)
    coord.release("custom://r", agent, outcome="modified")  # type: ignore[attr-defined]
    await asyncio.sleep(0.05)
    assert not task.done()  # release alone did not wake it (poll is 30 s off)
    srv._notify_after_standard(
        notifier,
        coord,
        "agenthold_release",
        {"resource": "custom://r"},
        {"status": "released"},
    )
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["status"] == "available"


async def test_wait_fallback_catches_unnotified_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 0.05)
    store, coord = _coord()
    agent = coord.register(name="a")["agent_id"]  # type: ignore[attr-defined]
    coord.claim("custom://r", agent)  # type: ignore[attr-defined]
    task = asyncio.create_task(
        srv._wait_standard(coord, "custom://r", timeout_seconds=5.0)  # default notifier
    )
    await asyncio.sleep(0.02)
    coord.release("custom://r", agent, outcome="modified")  # type: ignore[attr-defined]  # no notify
    result = await asyncio.wait_for(task, timeout=2.0)
    assert result["status"] == "available"


async def test_wait_times_out_while_still_claimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(srv, "_POLL_INTERVAL", 0.05)
    store, coord = _coord()
    holder = coord.register(name="h")["agent_id"]  # type: ignore[attr-defined]
    coord.claim("custom://busy", holder)  # type: ignore[attr-defined]
    result = await srv._wait_standard(coord, "custom://busy", timeout_seconds=0.15)
    assert result["status"] == "timeout"
    assert result["held_by"] == holder


def test_notify_after_standard_invalid_resource_is_noop() -> None:
    # A resource that fails canonicalization must be swallowed, not raised.
    store, coord = _coord()
    srv._notify_after_standard(
        KeyNotifier(),
        coord,
        "agenthold_release",
        {"resource": "../escape"},
        {"status": "released"},
    )
