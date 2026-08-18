"""The hardened farm cadence must preserve average throughput AND honor the floor.

The metronome was replaced with a heavy-tailed, tempo-scaled wait to kill the
periodogram peak — but the wait's EXPECTED value must still be ~= the operator's
configured interval, and it must never dip below the loop's absolute stealth
floor (the farm's 60s). Two review findings are pinned here:
  * a vf=0.5 regression that secretly halved the interval (doubling volume);
  * the sampler's 0.1x lower bound bypassing the 60s floor (#55) and its 4x tail
    cut pulling the mean to ~0.84x (~19% more requests than configured).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from travian_api.stealth.timing import HumanTiming
from travian_api.web.ws import farm_ws
from travian_api.web.ws._loop_stealth import recurring_wait as _next_cycle_wait


class _StubClient:
    stealth_enabled = True

    def tempo_scale(self, seconds: float) -> float:
        return seconds  # identity: isolate the delay distribution


def _ctx():
    return SimpleNamespace(session=SimpleNamespace(http_client=_StubClient()))


def test_expected_cadence_is_near_the_interval_not_halved():
    ctx = _ctx()
    interval = 300.0
    samples = [_next_cycle_wait(ctx, interval) for _ in range(20000)]
    mean = sum(samples) / len(samples)
    # ~0.84x with the 4x tail cap; a value near 0.5x (the vf=0.5 halving bug)
    # must still fail. The cap trims the heavy tail's contribution to the mean.
    assert 0.78 * interval <= mean <= 1.15 * interval, mean


def test_cadence_is_not_a_constant_metronome():
    ctx = _ctx()
    samples = [_next_cycle_wait(ctx, 300.0) for _ in range(2000)]
    assert len(set(round(s, 1) for s in samples)) > 100, "cadence must be spread, not fixed"


def test_large_interval_wait_is_capped_not_a_multi_hour_stall():
    # HumanTiming.delay clamps at 15x; recurring_wait caps at 4x so an hourly
    # interval can't draw a 15h stall that reads as a hang.
    ctx = _ctx()
    samples = [_next_cycle_wait(ctx, 3600.0) for _ in range(5000)]
    assert max(samples) <= 3600.0 * 4.0 + 1e-6, max(samples)


def test_stealth_off_is_deterministic():
    client = _StubClient()
    client.stealth_enabled = False
    ctx = SimpleNamespace(session=SimpleNamespace(http_client=client))
    assert _next_cycle_wait(ctx, 300.0) == 300.0


# ── #55: the 60s stealth floor must be honored, mean not below interval ──


def test_no_sample_ever_dips_below_the_floor():
    ctx = _ctx()
    samples = [_next_cycle_wait(ctx, 300.0, floor=60.0) for _ in range(20000)]
    # Strictly above: the variable part is ADDED to the floor, not clamped to it,
    # so there is no detectable point mass at exactly 60.
    assert min(samples) > 60.0, min(samples)


def test_floor_mean_is_not_below_the_configured_interval():
    ctx = _ctx()
    interval = 300.0
    samples = [_next_cycle_wait(ctx, interval, floor=60.0) for _ in range(20000)]
    mean = sum(samples) / len(samples)
    # Cadence may only run SLOWER than configured, never faster (the old 0.84x).
    assert mean >= interval, mean


def test_shortest_possible_draw_still_clears_the_floor():
    # Force HumanTiming.delay to its own lower bound (0.1x mean) — the exact draw
    # that used to yield a ~6s wait at a 60s interval, bypassing the floor.
    ctx = _ctx()
    with patch.object(
        HumanTiming, "delay", staticmethod(lambda mean, variance_factor=1.0: mean * 0.1)
    ):
        wait = _next_cycle_wait(ctx, 60.0, floor=60.0)
    assert wait >= 60.0, wait


# ── #56: a bounded run must not sleep a heavy-tail draw past its deadline ──


class _DeadlineCtx:
    def __init__(self):
        self.pushed = []
        self.exec_session = SimpleNamespace(label="")
        result = SimpleNamespace(success_count=0, fail_count=0, targets=[])

        async def _send(_list_id):
            return result

        async def _get(_list_id):
            return SimpleNamespace(id=1, name="L", active_slots=[])

        self.session = SimpleNamespace(
            http_client=SimpleNamespace(
                stealth_enabled=True,
                check_activity_budget=lambda: None,
                rest_pause_seconds=lambda: 0,  # never the night-rest window
            ),
            farm_service=SimpleNamespace(get_farm_list=_get, send_farm_list=_send),
        )

    def push(self, msg):
        self.pushed.append(msg)

    def should_stop(self):
        return False


def test_bounded_run_sleep_is_capped_to_the_deadline():
    """A one-minute bounded run whose cadence draws 240s must sleep at most the
    ~60s remaining, not the full 240s — otherwise it stays 'running' for minutes
    past the operator's requested duration."""
    ctx = _DeadlineCtx()
    slept = []

    async def _fake_sleep(_ctx, seconds, chunk=2.0):
        slept.append(seconds)
        return True  # stop after the first cycle so the coro ends

    coro = farm_ws._build_farm_run_coro(list_id=1, interval=60, duration=1, verbose=False, floor=60)
    with (
        patch.object(farm_ws, "recurring_wait", lambda *a, **k: 240.0),
        patch.object(farm_ws, "interruptible_sleep", _fake_sleep),
    ):
        asyncio.run(coro(ctx))

    assert slept, "the loop never reached its inter-cycle sleep"
    assert slept[0] <= 60.0 + 1.0, f"slept {slept[0]}s, past the 60s deadline"


def test_unbounded_run_sleep_is_not_capped():
    """With no duration, the full cadence draw is slept (nothing to cap against)."""
    ctx = _DeadlineCtx()
    slept = []

    async def _fake_sleep(_ctx, seconds, chunk=2.0):
        slept.append(seconds)
        return True

    coro = farm_ws._build_farm_run_coro(list_id=1, interval=60, duration=0, verbose=False, floor=60)
    with (
        patch.object(farm_ws, "recurring_wait", lambda *a, **k: 240.0),
        patch.object(farm_ws, "interruptible_sleep", _fake_sleep),
    ):
        asyncio.run(coro(ctx))

    assert slept and slept[0] == 240.0, slept
