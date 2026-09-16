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


def _draws(t: RequestThrottler, consequential: bool, n: int = 4000) -> list[float]:
    return [t._effective_gap(consequential) for _ in range(n)]


class TestConsequentialRequestsAreNotDecisions:
    """A page firing its own read-back does not stop to think first.

    Which requests those ARE is the caller's to say. It used to be inferred
    from the header shape -- json/xhr/fetch -- and `post_json` defaults to
    "json", so every `/api/v1/*` call in this app was paced as page chatter: a
    map pan, a tile click, a farm-list send, a trade-route create. See
    `TestPacingFollowsIntentNotTransport`.
    """

    def test_they_are_not_held_to_the_deliberate_floor(self):
        t = _throttler(min_gap_s=1.5)
        below = [g for g in _draws(t, True) if g < 1.5]

        assert len(below) > 3900, "almost every one should be under the old floor"

    def test_their_median_matches_the_recording(self):
        """0.0-0.1s is where the capture puts a read-back after its write."""
        t = _throttler()
        median = statistics.median(_draws(t, True))

        assert 0.01 < median < 0.20, median

    def test_they_are_never_exactly_zero(self):
        """Two requests sharing a timestamp to the microsecond is its own tell,
        and a hard zero would also defeat the ordering the caller relies on."""
        assert all(g > 0.0 for g in _draws(_throttler(), True))


class TestDeliberateRequestsStillCostHumanTime:
    """The narrowing must not turn into "everything is fast now"."""

    def test_a_navigation_still_respects_the_floor(self):
        t = _throttler(min_gap_s=1.5)

        assert all(g >= 1.5 for g in _draws(t, False))

    def test_the_default_is_deliberate(self):
        """The safe default: pacing something too slowly costs time, pacing a
        human decision at 0.06s costs the account."""
        t = _throttler(min_gap_s=1.5)

        assert all(t._effective_gap() >= 1.5 for _ in range(2000))


class TestTheAccountIsSometimesDistracted:
    """380 seconds. Mid-session, tab open, nothing happening.

    The old distribution's whole support sat under `max_gap_s`, so our traffic
    was continuous purposeful activity from the first request to the last. A
    person stops to read something, answers the door, reads a message.
    """

    def test_a_long_pause_is_possible_at_all(self):
        """Shape checked against the recording's own tail, not against a guess.

        The 364 deliberate requests had fifteen gaps over eight seconds::

            380.7  59.2  19.8  18.0  16.5  15.7  14.6  12.5  12.2  12.2  10.1
              9.9   9.5   9.3   8.1

        A cluster in the teens, one pause of a minute, one of six. So the BODY
        of a distraction belongs in the teens and the TAIL has to reach minutes
        -- which is the correction: this used to assert a median above twenty,
        written when the sampler used a 45-second median, and a 45-second median
        puts the body of our distractions where the real distribution keeps its
        tail.
        """
        t = _throttler(distraction_chance=1.0)
        draws = _draws(t, False, n=400)

        assert 10.0 < statistics.median(draws) < 30.0
        assert max(draws) > 60.0, "the tail has to reach minutes, not just tens"

    def test_it_is_rare_enough_not_to_dominate_a_short_run(self):
        random.seed(20260915)
        t = _throttler()
        long_ones = [g for g in _draws(t, False, n=4000) if g > 20.0]

        assert 10 < len(long_ones) < 200, len(long_ones)

    def test_it_can_be_switched_off_entirely(self):
        t = _throttler(distraction_chance=0.0, max_gap_s=3.0)

        assert all(g < 30.0 for g in _draws(t, False))

    def test_consequential_requests_are_never_distracted(self):
        """The page's own chatter does not wander off; only the person does."""
        t = _throttler(distraction_chance=1.0)

        assert all(g < 5.0 for g in _draws(t, True))


class TestTheBurstCapNoLongerFlagsAHuman:
    """Ours would have put the recorded session in a cooldown.

    A single page load is 13 requests -- the document plus twelve locale bundles
    fired in parallel -- and the largest observed burst was 35 requests under
    0.5s apart. Three ordinary page loads inside a minute is 39. The cap was 20.
    """

    def test_a_real_page_loads_worth_of_requests_does_not_trip_it(self):
        """Behaviour, not the constant.

        These asserted `burst_max_requests >= 39`, which is the number itself
        restated. What matters is whether a burst the size of real traffic
        actually trips the cooldown: a single page load is 13 requests (the
        document plus twelve locale bundles), and three inside a minute is 39.
        """
        t = _throttler()
        for _ in range(39):
            t._request_times.append(0.0)

        assert len(t._request_times) < t._effective_burst_max, (
            "three ordinary page loads in a minute must not read as a burst"
        )

    def test_the_largest_observed_human_burst_does_not_trip_it_either(self):
        t = _throttler()
        for _ in range(35):
            t._request_times.append(0.0)

        assert len(t._request_times) < t._effective_burst_max

    def test_the_jittered_trigger_still_never_lands_on_a_constant(self):
        t = _throttler()
        rolls = {t._roll_burst_threshold() for _ in range(200)}

        assert len(rolls) > 5, "a fixed trigger point is itself a signature"


class TestALongPauseTellsTheOperator:
    """A run that sits silent for four minutes looks hung, not deliberate.

    Which matters more than it sounds: an operator who thinks a run has hung
    restarts it, and a restart is the one response that undoes the pause. The
    pause has to announce itself, and at a level that reaches the screen --
    `LogBroadcastHandler` streams this logger to /ws/logs at level=info, so
    INFO is the difference between visible and not.
    """

    def test_a_distraction_is_reported_at_info(self, caplog):
        import asyncio
        import logging
        import time

        t = _throttler(min_gap_s=0.0, max_gap_s=0.01, distraction_chance=1.0)
        # Pin the distraction's own shape. It is drawn per account now, and its
        # body starts in the teens -- close enough to the 8s "worth announcing"
        # threshold that an unpinned draw makes this test a coin flip.
        t._distraction_median_s = 120.0
        t._distraction_sigma = 0.1
        # Must be NOW, not 1.0: elapsed is measured against time.monotonic(), so
        # an ancient timestamp means the gap has already passed and nothing waits.
        t._last_request_time = time.monotonic()

        async def _instant(_s):
            return None

        with caplog.at_level(logging.INFO, logger="travian_api.stealth.throttler"):
            import travian_api.stealth.throttler as mod

            real_sleep = mod.asyncio.sleep
            mod.asyncio.sleep = _instant
            try:
                asyncio.run(t.wait(context="creating trade route"))
            finally:
                mod.asyncio.sleep = real_sleep

        said = " ".join(r.getMessage() for r in caplog.records)
        assert "Pausing" in said
        assert "Nothing is stuck" in said, "the operator must be told it is not hung"
        assert "creating trade route" in said, "and what it is pausing before"

    def test_ordinary_pacing_stays_quiet(self, caplog):
        """Announcing every 2s gap would bury the one that matters."""
        import asyncio
        import logging
        import time

        t = _throttler(min_gap_s=0.0, max_gap_s=0.01, distraction_chance=0.0)
        t._last_request_time = time.monotonic()

        with caplog.at_level(logging.INFO, logger="travian_api.stealth.throttler"):
            asyncio.run(t.wait(context="a short one"))

        assert not [r for r in caplog.records if "Pausing" in r.getMessage()]


class TestTheDurationReadsLikeAPerson:
    def test_seconds_under_a_minute(self):
        from travian_api.stealth.throttler import _human_duration

        assert _human_duration(45.4) == "45s"

    def test_minutes_and_seconds_above_it(self):
        from travian_api.stealth.throttler import _human_duration

        assert _human_duration(252.0) == "4m12s"

    def test_a_round_minute_keeps_its_seconds(self):
        from travian_api.stealth.throttler import _human_duration

        assert _human_duration(120.0) == "2m00s"


class TestTheDistractionTailIsPerAccountToo:
    """The gap BODY was already account-distinct. Its TAIL was not.

    Every account we ran distracted at exactly 1.5% of gaps, drawn from exactly
    log-normal(log 45s, 1.0). Two numbers -- what fraction of an account's gaps
    exceed twenty seconds, and how long those run -- came out identical across
    the whole fleet, which is the same cross-account clustering the median
    fraction and sigma exist to defeat. Defeating it in the body while leaving
    it in the tail defeats it nowhere: the long gaps are the conspicuous ones,
    so the tail is the cheaper statistic to measure.
    """

    @staticmethod
    def _shape(identity: str) -> tuple[float, float, float]:
        t = RequestThrottler()
        t.seed_gap_shape(identity)
        return (t.distraction_chance, t._distraction_median_s, t._distraction_sigma)

    def test_it_is_stable_across_restarts(self):
        assert self._shape("account-a") == self._shape("account-a")

    def test_two_accounts_do_not_share_it(self):
        assert self._shape("account-a") != self._shape("account-b")

    def test_the_population_spreads_rather_than_clustering(self):
        rates = {self._shape(f"account-{i}")[0] for i in range(40)}

        assert len(rates) == 40, "a shared rate is the whole finding"
        assert max(rates) > min(rates) * 2, "the spread has to be worth having"

    def test_an_explicit_chance_is_left_exactly_alone(self):
        """A caller that names a number means it -- otherwise every test that
        pins this value would be quietly re-rolled underneath."""
        t = _throttler(distraction_chance=0.5)
        t.seed_gap_shape("account-a")

        assert t.distraction_chance == 0.5

    def test_the_rate_brackets_what_the_recording_showed(self):
        """8 of 364 deliberate gaps sat past what the body can reach: ~2.2%."""
        rates = [self._shape(f"account-{i}")[0] for i in range(200)]

        assert min(rates) < 0.022 < max(rates)


class TestPacingFollowsIntentNotTransport:
    """The regression this class exists for, and it was a bad one.

    `_CONSEQUENTIAL` used to be `{"fetch", "xhr", "json"}` and the mode was read
    off the header shape. `HttpClient.post_json` defaults to
    `request_type="json"`, so EVERY `/api/v1/*` call in this app was paced as
    the page's own chatter -- `map/position`, `map/tile-details`,
    `farm-list/send`, `trade-routes`. A map pan and a tile click are decisions;
    they are the class where a 0.06-second median is least defensible.

    The evidence was misread in a specific and checkable way. The recording has
    two columns: `all requests` (p50 0.00s) and `documents + API` (p50 0.90s).
    The consequential branch cited the first. But `all requests` is carried by
    1,265 locale bundles -- 84% of the capture, and requests this app
    deliberately makes none of. Strip them: 1,631 - 1,265 = 366, which is the
    documents-and-API column, p50 0.90s. Fifteen times what was being applied.

    The same file's distraction analysis had it right the whole time, calling
    the 364 documents-and-API requests "DELIBERATE".
    """

    def test_an_api_call_is_deliberate_unless_the_caller_says_otherwise(self):
        """post_json's default request_type is "json"; that must not fast-path it."""
        t = _throttler(min_gap_s=1.5)

        assert all(t._effective_gap() >= 1.5 for _ in range(2000))

    def test_the_gap_no_longer_depends_on_the_header_shape(self):
        """Header shape and pacing answer different questions. Conflating them
        is what let a default argument decide how fast we hit the game."""
        import inspect

        source = inspect.getsource(RequestThrottler._effective_gap)

        assert "request_type" not in source

    def test_a_scan_loop_cannot_outrun_a_human(self):
        """The aggregate nobody was measuring.

        `auto_scout_service` and `scout_ws` loop map/position with NO other
        pacing layer -- no human_delay, no think delay. The throttler was the
        only thing between requests, so its classification WAS the scan's
        emitted rate. Under the old behaviour a 25-region scan fell from ~40s
        to ~2s.

        This asserts the sustained rate the way an observer would measure it:
        total time for a run of requests, divided by the count.
        """
        t = _throttler(min_gap_s=1.5, max_gap_s=3.0, distraction_chance=0.0)
        total = sum(t._effective_gap() for _ in range(200))

        assert total / 200 >= 1.5, "a sweep must not sustain more than ~0.66 req/s"


def test_the_calibrated_burst_cap_is_the_one_production_runs():
    """The 20 -> 60 recalibration originally moved only this class's constructor
    default -- which production never reads. `HttpClient` passes
    `settings.stealth_burst_max`, and that stayed at 30, so the measured
    justification sat on a dead parameter while the live cap never changed.

    Pinned as an equality between the two seams, so whichever one the next
    calibration edits, this fails until the other follows and the number that
    was measured is the number that ships.
    """
    import inspect

    from travian_api.config import Settings

    configured = Settings.model_fields["stealth_burst_max"].default
    constructor = (
        inspect.signature(RequestThrottler.__init__).parameters["burst_max_requests"].default
    )

    assert configured == constructor == 60
