"""The hardened farm cadence must preserve average throughput.

The metronome was replaced with a heavy-tailed, tempo-scaled wait to kill the
periodogram peak — but the wait's EXPECTED value must still be ~= the operator's
configured interval. A code-review caught that variance_factor=0.5 secretly
halved it (doubling request volume, the opposite of the stealth goal). This
pins the expected value near the interval so that regression can't recur.
"""

from types import SimpleNamespace

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
    # ~0.97x by construction; a value near 0.5x (the vf=0.5 bug) must fail.
    assert 0.85 * interval <= mean <= 1.15 * interval, mean


def test_cadence_is_not_a_constant_metronome():
    ctx = _ctx()
    samples = [_next_cycle_wait(ctx, 300.0) for _ in range(2000)]
    assert len(set(round(s, 1) for s in samples)) > 100, "cadence must be spread, not fixed"


def test_stealth_off_is_deterministic():
    client = _StubClient()
    client.stealth_enabled = False
    ctx = SimpleNamespace(session=SimpleNamespace(http_client=client))
    assert _next_cycle_wait(ctx, 300.0) == 300.0
