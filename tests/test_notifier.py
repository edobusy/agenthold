"""Unit tests for KeyNotifier (in-process wakeups for watch/wait)."""

from __future__ import annotations

import asyncio

from agenthold.notifier import KeyNotifier


async def test_notify_resolves_waiter() -> None:
    n = KeyNotifier()
    fut = n.subscribe("k")
    assert not fut.done()
    n.notify("k")
    assert fut.done()  # resolved synchronously on the loop
    await fut  # does not raise


async def test_notify_with_no_waiter_is_noop() -> None:
    KeyNotifier().notify("nobody")  # must not raise


async def test_notify_wakes_all_waiters_on_key() -> None:
    n = KeyNotifier()
    f1 = n.subscribe("k")
    f2 = n.subscribe("k")
    n.notify("k")
    assert f1.done() and f2.done()


async def test_notify_is_key_isolated() -> None:
    n = KeyNotifier()
    fa = n.subscribe("a")
    fb = n.subscribe("b")
    n.notify("a")
    assert fa.done() and not fb.done()


async def test_unsubscribe_prevents_resolution() -> None:
    n = KeyNotifier()
    fut = n.subscribe("k")
    n.unsubscribe("k", fut)
    n.notify("k")
    assert not fut.done()


async def test_unsubscribe_is_safe_to_repeat() -> None:
    n = KeyNotifier()
    fut = n.subscribe("k")
    n.unsubscribe("k", fut)
    n.unsubscribe("k", fut)  # already gone
    n.unsubscribe("other", fut)  # unknown key


async def test_second_notify_after_pop_is_noop() -> None:
    n = KeyNotifier()
    fut = n.subscribe("k")
    n.notify("k")
    n.notify("k")  # waiters already popped
    await asyncio.wait_for(fut, 1)
