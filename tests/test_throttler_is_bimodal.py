"""Request pacing has two modes, because real traffic does.

Measured against a recorded human session -- 1,631 requests, 2026-09-15, one
player setting up trade routes, raiding and sending resources:

    percentile      all requests    documents + API
    p25                   0.00s               0.10s
    p50                   0.00s               0.90s
    p75                   0.10s               1.90s
    p90                   1.10s               4.20s
    p99                   7.80s              19.80s
    max                 380.70s             380.70s

Two facts in that table sank the old model. **94% of those gaps are below the
1.5s floor this throttler applied to every request**, and the median for
documents-and-API is 0.90s -- our MINIMUM sat above their TYPICAL. And the
largest gap is 380 seconds, which nothing here could emit: `max_gap_s` is 3.0.

The shape is bimodal and only one mode was modelled:

    deliberate      a click, a form submit, choosing a target. A decision, so
                    it costs human time.
    consequential   subresources, the page's own API chatter, a read-back
                    fired off the back of a write. Not a decision at all.

`HttpClient` has always known which is which -- it picks headers from exactly
that -- and never passed it on. So a read-back the browser fires in 0.1s was
paced like a human choosing something, and the two modes collapsed into one
smeared band no person produces.
"""

import random
import statistics

from travian_api.stealth.throttler import RequestThrottler


def _throttler(**kw) -> RequestThrottler:
    return RequestThrottler(**kw)


def _draws(t: RequestThrottler, request_type: str, n: int = 4000) -> list[float]:
    return [t._effective_gap(request_type) for _ in range(n)]


class TestConsequentialRequestsAreNotDecisions:
    """A page firing its own read-back does not stop to think first."""

    def test_they_are_not_held_to_the_deliberate_floor(self):
        t = _throttler(min_gap_s=1.5)
        below = [g for g in _draws(t, "fetch") if g < 1.5]

        assert len(below) > 3900, "almost every one should be under the old floor"

    def test_their_median_matches_the_recording(self):
        """Recorded p50 for all traffic is 0.00s, p75 is 0.10s."""
        t = _throttler()
        median = statistics.median(_draws(t, "fetch"))

        assert 0.01 < median < 0.20, median

    def test_every_consequential_class_is_treated_alike(self):
        t = _throttler()
        for kind in ("fetch", "xhr", "json"):
            assert statistics.median(_draws(t, kind)) < 0.25, kind

    def test_they_are_never_exactly_zero(self):
        """Two requests sharing a timestamp to the microsecond is its own tell,
        and a hard zero would also defeat the ordering the caller relies on."""
        assert all(g > 0.0 for g in _draws(_throttler(), "fetch"))


class TestDeliberateRequestsStillCostHumanTime:
    """The narrowing must not turn into "everything is fast now"."""

    def test_a_page_navigation_still_respects_the_floor(self):
        t = _throttler(min_gap_s=1.5)

        assert all(g >= 1.5 for g in _draws(t, "page"))

    def test_a_form_submit_is_deliberate_too(self):
        t = _throttler(min_gap_s=1.5)

        assert all(g >= 1.5 for g in _draws(t, "form"))

    def test_an_unknown_class_is_treated_as_deliberate(self):
        """The safe default: pacing something too slowly is survivable, pacing
        a human decision at 0.06s is not."""
        t = _throttler(min_gap_s=1.5)

        assert all(g >= 1.5 for g in _draws(t, "something-new"))


class TestTheAccountIsSometimesDistracted:
    """380 seconds. Mid-session, tab open, nothing happening.

    The old distribution's whole support sat under `max_gap_s`, so our traffic
    was continuous purposeful activity from the first request to the last. A
    person stops to read something, answers the door, reads a message.
    """

    def test_a_long_pause_is_possible_at_all(self):
        t = _throttler(distraction_chance=1.0)
        draws = _draws(t, "page", n=400)

        assert statistics.median(draws) > 20.0
        assert max(draws) > 60.0, "the tail has to reach minutes, not just tens"

    def test_it_is_rare_enough_not_to_dominate_a_short_run(self):
        random.seed(20260915)
        t = _throttler()
        long_ones = [g for g in _draws(t, "page", n=4000) if g > 20.0]

        assert 10 < len(long_ones) < 200, len(long_ones)

    def test_it_can_be_switched_off_entirely(self):
        t = _throttler(distraction_chance=0.0, max_gap_s=3.0)

        assert all(g < 30.0 for g in _draws(t, "page"))

    def test_consequential_requests_are_never_distracted(self):
        """The page's own chatter does not wander off; only the person does."""
        t = _throttler(distraction_chance=1.0)

        assert all(g < 5.0 for g in _draws(t, "fetch"))


class TestTheBurstCapNoLongerFlagsAHuman:
    """Ours would have put the recorded session in a cooldown.

    A single page load is 13 requests -- the document plus twelve locale bundles
    fired in parallel -- and the largest observed burst was 35 requests under
    0.5s apart. Three ordinary page loads inside a minute is 39. The cap was 20.
    """

    def test_the_default_clears_three_page_loads_in_a_minute(self):
        assert RequestThrottler().burst_max_requests >= 39

    def test_it_clears_the_largest_observed_human_burst(self):
        assert RequestThrottler().burst_max_requests >= 35

    def test_the_jittered_trigger_still_never_lands_on_a_constant(self):
        t = _throttler()
        rolls = {t._roll_burst_threshold() for _ in range(200)}

        assert len(rolls) > 5, "a fixed trigger point is itself a signature"
