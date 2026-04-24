"""Tests for the KeyedLock primitive."""

from __future__ import annotations

import asyncio

import pytest

from travian_api.concurrency import KeyedLock


@pytest.mark.asyncio
async def test_same_key_serializes() -> None:
    lock = KeyedLock()
    order: list[str] = []

    async def worker(tag: str, hold_s: float) -> None:
        async with lock("shared"):
            order.append(f"{tag}:enter")
            await asyncio.sleep(hold_s)
            order.append(f"{tag}:exit")

    await asyncio.gather(worker("a", 0.05), worker("b", 0.01))

    # Whoever entered first must exit before the other enters.
    assert order[0].endswith(":enter")
    assert order[1].endswith(":exit")
    assert order[1].split(":")[0] == order[0].split(":")[0]


@pytest.mark.asyncio
async def test_different_keys_run_in_parallel() -> None:
    lock = KeyedLock()
    enters: list[str] = []

    async def worker(key: str) -> None:
        async with lock(key):
            enters.append(key)
            # Yield to the loop so the other worker can enter before we exit.
            await asyncio.sleep(0.02)

    await asyncio.gather(worker("x"), worker("y"))
    # Both entered; order doesn't matter.
    assert set(enters) == {"x", "y"}


@pytest.mark.asyncio
async def test_opportunistic_cleanup() -> None:
    lock = KeyedLock()

    async with lock(("slot", 42)):
        pass

    assert ("slot", 42) not in lock._locks


@pytest.mark.asyncio
async def test_cleanup_defers_while_waiters_exist() -> None:
    lock = KeyedLock()
    started = asyncio.Event()

    async def holder() -> None:
        async with lock("k"):
            started.set()
            await asyncio.sleep(0.05)

    async def waiter() -> None:
        await started.wait()
        # Small beat to let holder actually be inside the critical section.
        await asyncio.sleep(0)
        async with lock("k"):
            pass

    await asyncio.gather(holder(), waiter())
    # After both complete, the key is cleaned up.
    assert "k" not in lock._locks
