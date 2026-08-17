"""Distributional invariants a statistical bot-detector would test.

Pins the Cluster-2 hardening: session-tempo scaling must not re-introduce a
point-mass at the min-gap floor, and the burst trigger must not be a fixed
round count.
"""

from travian_api.stealth.throttler import RequestThrottler


class _Tempo:
    def __init__(self, value):
        self._v = value

    def current(self):
        return self._v


class TestNoFloorPointMass:
    def test_slow_tempo_does_not_pile_gaps_at_the_floor(self):
        """A tempo < 1 used to clamp every low draw onto exactly min_gap_s.
        The increment-only scaling must keep the density at the floor ~zero."""
        t = RequestThrottler(min_gap_s=1.5, max_gap_s=3.0)
        t.set_tempo(_Tempo(0.7))

        gaps = [t._effective_gap() for _ in range(5000)]

        assert min(gaps) >= 1.5, "no gap may fall below the hard floor"
        at_floor = sum(1 for g in gaps if abs(g - 1.5) < 1e-9)
        assert at_floor == 0, f"{at_floor}/5000 gaps sat exactly on the floor (point mass)"
        # And the distribution still has real spread above the floor.
        assert max(gaps) > 1.8

    def test_fast_tempo_still_respects_the_floor(self):
        t = RequestThrottler(min_gap_s=1.5, max_gap_s=3.0)
        t.set_tempo(_Tempo(1.3))
        assert all(t._effective_gap() >= 1.5 for _ in range(2000))

    def test_a_degenerate_nonpositive_tempo_never_goes_below_floor(self):
        t = RequestThrottler(min_gap_s=1.5, max_gap_s=3.0)
        t.set_tempo(_Tempo(0.0))
        assert all(t._effective_gap() >= 1.5 for _ in range(500))


class TestBurstThresholdJitter:
    def test_threshold_varies_around_the_configured_max(self):
        t = RequestThrottler(burst_max_requests=30)
        rolls = {t._roll_burst_threshold() for _ in range(200)}
        assert len(rolls) > 1, "a fixed threshold is the sawtooth tell"
        assert all(24 <= r <= 36 for r in rolls), rolls

    def test_threshold_stays_at_least_one(self):
        t = RequestThrottler(burst_max_requests=1)
        assert all(t._roll_burst_threshold() >= 1 for _ in range(50))
