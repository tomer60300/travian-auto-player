"""The rate limiter must not grow its key map without bound.

On a remotely exposed instance, login attempts arrive from many source IPs.
The old cleanup only pruned the current key (which was then immediately given
a fresh timestamp), so the empty-key sweep never fired and expired keys
accumulated forever. These tests pin that stale keys are actually evicted.
"""

import asyncio
from types import SimpleNamespace

from travian_api.web.rate_limit import RateLimiter


def _request(host: str):
    return SimpleNamespace(state=SimpleNamespace(), client=SimpleNamespace(host=host))


def test_expired_keys_are_evicted_once_over_the_threshold():
    limiter = RateLimiter(max_calls=5, window_seconds=10)
    # Seed 150 keys whose only timestamp is ancient (well outside the window).
    for i in range(150):
        limiter._calls[f"ip:stale-{i}"] = [0.0]

    # One live call from a fresh IP crosses the >100 threshold and sweeps.
    asyncio.run(limiter(_request("203.0.113.1")))

    remaining = set(limiter._calls)
    assert remaining == {"ip:203.0.113.1"}, (
        f"stale keys were not evicted: {len(remaining)} keys remain"
    )


def test_a_key_with_in_window_activity_survives_the_sweep():
    limiter = RateLimiter(max_calls=5, window_seconds=10)
    for i in range(150):
        limiter._calls[f"ip:stale-{i}"] = [0.0]
    # A recently-active other key must be kept (pruned to its fresh timestamps).
    import time

    limiter._calls["ip:busy"] = [time.monotonic()]

    asyncio.run(limiter(_request("203.0.113.2")))

    assert "ip:busy" in limiter._calls
    assert "ip:203.0.113.2" in limiter._calls
    assert not any(k.startswith("ip:stale-") for k in limiter._calls)
