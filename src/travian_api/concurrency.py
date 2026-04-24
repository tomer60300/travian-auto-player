"""Reusable concurrency primitives.

KeyedLock serializes access to one resource at a time (e.g. one farm list,
one building slot, one map tile) while allowing disjoint resources to run
in parallel. Use it when the real invariant is "don't mutate the same
resource twice concurrently" — not "don't run the same operation twice."
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Hashable
from contextlib import asynccontextmanager


class KeyedLock:
    """Per-key asyncio mutex with opportunistic cleanup.

    asyncio is single-threaded, so ``dict.setdefault`` is atomic across await
    points; no meta-lock is needed. The lock entry is dropped after release
    when nobody is waiting, so the dict stays bounded by the live keyspace.
    """

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: dict[Hashable, asyncio.Lock] = {}

    @asynccontextmanager
    async def __call__(self, key: Hashable) -> AsyncIterator[None]:
        lock = self._locks.setdefault(key, asyncio.Lock())
        await lock.acquire()
        try:
            yield
        finally:
            lock.release()
            # Drop the entry if no coroutine is waiting, so the dict doesn't
            # grow with every distinct key ever seen.
            if not lock.locked() and not lock._waiters:  # type: ignore[attr-defined]
                self._locks.pop(key, None)
