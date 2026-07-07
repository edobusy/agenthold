"""
In-process wakeups for agenthold_watch / agenthold_wait.

The watch/wait loops keep a bounded polling fallback for correctness (a write in
another process cannot be observed except by re-reading the shared SQLite file).
On top of that, ``KeyNotifier`` lets a change made *in this process* wake any
waiters immediately, instead of them sleeping out the poll interval.

Notifications are therefore a best-effort latency optimisation: a missed
notification only means the waiter falls back to polling. That keeps the whole
mechanism low-risk — no notifier bug can cause a hang or a wrong result, only a
slower wake.

Loop affinity: every method must be called from the event-loop thread. Notify is
invoked from ``call_tool`` *after* the offloaded store dispatch has awaited back
onto the loop, so resolving a waiter's future needs no thread-safe scheduling.
"""

from __future__ import annotations

import asyncio


class KeyNotifier:
    """Wake in-process waiters keyed by an opaque notification string."""

    def __init__(self) -> None:
        self._waiters: dict[str, set[asyncio.Future[None]]] = {}

    def subscribe(self, key: str) -> asyncio.Future[None]:
        """Register interest in ``key`` and return a future to await.

        Callers must subscribe *before* reading current state, so a notify that
        lands between the read and the await is not lost, and must always pass
        the returned future to ``unsubscribe`` (in a ``finally``).
        """
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(key, set()).add(fut)
        return fut

    def unsubscribe(self, key: str, fut: asyncio.Future[None]) -> None:
        """Drop a waiter's future. Safe to call more than once."""
        waiters = self._waiters.get(key)
        if waiters is None:
            return
        waiters.discard(fut)
        if not waiters:
            self._waiters.pop(key, None)

    def notify(self, key: str) -> None:
        """Wake every current waiter on ``key``. No-op if there are none."""
        waiters = self._waiters.pop(key, None)
        if not waiters:
            return
        for fut in waiters:
            if not fut.done():
                fut.set_result(None)
