"""Global request rate limiter.

Ensures a minimum time gap between any two requests to the game server.
Prevents machine-gun request patterns that are trivially detectable.

Also tracks request counts per time window for burst detection.
"""

import asyncio
import logging
import math
import random
import time
from collections import deque

logger = logging.getLogger(__name__)


# The request classes a browser actually produces, and the reason this module
# needed to learn about them at all.
#
# Measured against a recorded human session, 1,631 requests, 2026-09-15: the gap
# distribution is BIMODAL and this throttler only modelled one of the modes.
#
#   deliberate    a click, a form submit, choosing a target. A decision, so it
#                 costs human time: p50 ~0.9s, tail to 20s and beyond.
#   consequential subresources, the page's own API chatter, read-backs. NOT a
#                 decision -- the browser makes them because the first one
#                 happened -- so they arrive at p50 0.00s, p75 0.10s.
#
# A single floor applied to both smears the two modes into one band no human
# emits. 94% of that session's gaps fell BELOW this module's 1.5s minimum, and
# its median for documents-and-API was 0.90s: our floor sat above their typical.
#
# `HttpClient` has always known which class a request is -- it picks the headers
# from it -- and never told us.
_CONSEQUENTIAL = frozenset({"fetch", "xhr", "json"})

# Ceiling on a distraction pause. Above the 380.7s the recording's longest gap
# measured, so the shape is not clipped where the evidence lives, and far below
# "this loop has hung".
_MAX_DISTRACTION_S = 600.0

# Above this, a pause is reported to the operator rather than only to the debug
# log. Chosen so ordinary pacing stays quiet -- the body of the deliberate
# distribution is a few seconds -- while a distraction always announces itself.
_VISIBLE_PAUSE_S = 8.0


def _human_duration(seconds: float) -> str:
    """ "4m12s", "45s". Written for a person reading a log line, not a chart."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds) // 60}m{int(seconds) % 60:02d}s"


class RequestThrottler:
    """Global rate limiter for all HTTP requests to Travian.

    Features:
    - Minimum gap between requests (with jitter)
    - Burst detection: if too many requests in a short window, add extra delay
    - Adaptive: slows down if server returns errors/captchas

    Usage:
        throttler = RequestThrottler()
        await throttler.wait()  # call before every request
    """

    def __init__(
        self,
        min_gap_s: float = 1.5,
        max_gap_s: float = 3.0,
        burst_window_s: float = 60.0,
        # 60, not 20. Measured against a recorded human session 2026-09-15: a
        # SINGLE page load is 13 requests (the document plus twelve locale
        # bundles the page fires in parallel), and the largest observed burst
        # was 35 requests under 0.5s apart. Three ordinary page loads inside a
        # minute is 39. The old cap of 20 would have flagged that session as a
        # burst violation and sat it in a cooldown -- a stealth guard calibrated
        # so that genuine human traffic trips it is calibrated wrong, and the
        # cooldown it imposes is itself the unnatural shape.
        burst_max_requests: int = 60,
        burst_cooldown_s: float = 15.0,
        # How often a deliberate gap becomes a real pause instead. 1.5% puts
        # roughly one distraction in a seventy-request session, which is the
        # order the recording showed (one 380s gap in 1,631 requests, plus
        # several tens-of-seconds ones).
        distraction_chance: float = 0.015,
        enabled: bool = True,
    ):
        """
        Args:
            min_gap_s: Minimum seconds between requests (hard floor)
            max_gap_s: Nominal upper bound of the gap body. Gaps are drawn
                from a right-skewed distribution whose body sits in
                [min_gap_s, max_gap_s]; the tail can exceed it, soft-capped
                at 3x max_gap_s.
            burst_window_s: Time window for burst detection
            burst_max_requests: Max requests in burst window before cooldown
            burst_cooldown_s: Extra delay when burst limit hit
            enabled: If False, throttling is disabled (for testing)
        """
        self.min_gap_s = min_gap_s
        self.max_gap_s = max_gap_s
        self.burst_window_s = burst_window_s
        self.burst_max_requests = burst_max_requests
        self.burst_cooldown_s = burst_cooldown_s
        self.distraction_chance = distraction_chance
        self.enabled = enabled

        self._last_request_time: float = 0
        self._request_times: deque = deque()
        self._lock = asyncio.Lock()
        self._penalty_until: float = 0  # extra penalty from errors
        self._captcha_guard = None  # set via set_captcha_guard()
        self._tempo = None  # optional shared SessionTempo, set via set_tempo()

        # Per-session gap-shape parameters. Drawn once per instance instead of
        # using one global constant so two accounts on the same config don't
        # emit an identical *normalized* gap shape — a cross-account
        # likelihood-ratio/KS test could otherwise fingerprint the shared
        # generator even though each marginal passes a uniform-rejection test.
        self._gap_median_frac = random.uniform(0.30, 0.48)
        self._gap_sigma = random.uniform(0.45, 0.85)

        # Jittered per-cycle burst threshold (see _roll_burst_threshold): a
        # fixed count trigger produces a recognizable "N fast then a fixed
        # cooldown" sawtooth that burst heuristics key on directly.
        self._effective_burst_max = self._roll_burst_threshold()

    def _roll_burst_threshold(self) -> int:
        """A jittered burst-trigger count so the cap never lands on a round
        constant twice running (±20% around the configured max)."""
        lo = max(1, int(self.burst_max_requests * 0.8))
        hi = max(lo, int(self.burst_max_requests * 1.2))
        return random.randint(lo, hi)

    def set_captcha_guard(self, guard) -> None:
        """Attach a CaptchaGuard so requests block when captcha is active."""
        self._captcha_guard = guard

    def set_tempo(self, tempo) -> None:
        """Attach a shared SessionTempo so gaps drift with action delays."""
        self._tempo = tempo

    def seed_gap_shape(self, identity: str) -> None:
        """Bind the gap-shape params to a stable identity (e.g. the persona).

        By default the params are drawn fresh per instance, so the same account
        drifts to a new gap shape on every process restart — a two-sample
        KS / Cramer-von Mises test across sessions of one account could flag the
        change. Deriving them deterministically from a persona-stable identity
        keeps one account's gap shape constant across restarts while still
        differing between accounts. The persona already rotates only on cookie
        expiry or server change, so the shape inherits that lifetime.
        """
        rng = random.Random(identity)
        self._gap_median_frac = rng.uniform(0.30, 0.48)
        self._gap_sigma = rng.uniform(0.45, 0.85)

    async def wait(self, context: str = "", request_type: str = "page") -> float:
        """Wait until it's safe to make the next request.

        Args:
            context: Optional description for logging (e.g., "upgrade building")
            request_type: "page", "form", "json", "xhr" or "fetch" -- the class
                the caller already resolved to choose its headers. Consequential
                classes (see `_CONSEQUENTIAL`) are paced on a near-zero floor,
                because the browser fires them off the back of something else
                rather than deciding to.

        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0

        # Captcha gate: block indefinitely if captcha is active.
        # Placed BEFORE the lock so all coroutines suspend on the Event
        # rather than queueing on the lock.
        if self._captcha_guard is not None:
            await self._captcha_guard.wait_if_blocked()

        async with self._lock:
            now = time.monotonic()
            waited = 0.0

            # Check penalty (from server errors / captcha detection)
            penalty_wait = await self.wait_for_penalty()
            if penalty_wait > 0:
                waited += penalty_wait
                now = time.monotonic()

            # Check burst limit against a JITTERED threshold, with a
            # heavy-tailed cooldown — a hard count plus a flat cooldown is a
            # machine-shaped sawtooth. The threshold is re-rolled after each
            # trigger so successive cycles don't share a trigger point.
            self._cleanup_burst_window(now)
            if len(self._request_times) >= self._effective_burst_max:
                burst_wait = self.burst_cooldown_s + random.lognormvariate(math.log(3.0), 0.6)
                logger.info(
                    "Slowing down for %s — %d requests in the last %.0fs is more "
                    "than a person clicks.%s",
                    _human_duration(burst_wait),
                    len(self._request_times),
                    self.burst_window_s,
                    f" (next: {context})" if context else "",
                )
                await asyncio.sleep(burst_wait)
                waited += burst_wait
                now = time.monotonic()
                self._cleanup_burst_window(now)
                self._effective_burst_max = self._roll_burst_threshold()

            # Enforce minimum gap with heavy-tailed jitter, scaled by the
            # shared session tempo (so consecutive gaps are positively
            # correlated, not iid) but never below the hard floor.
            if self._last_request_time > 0:
                elapsed = now - self._last_request_time
                target_gap = self._effective_gap(request_type)
                if elapsed < target_gap:
                    gap_wait = target_gap - elapsed
                    # A pause long enough for the operator to notice is a pause
                    # the operator should be TOLD about. These reach the UI:
                    # `LogBroadcastHandler` streams this logger to /ws/logs at
                    # level=info, and a run that sits silent for four minutes
                    # otherwise looks hung rather than deliberate -- which
                    # invites exactly the response that undoes it, restarting
                    # the run.
                    if gap_wait >= _VISIBLE_PAUSE_S:
                        logger.info(
                            "Pausing %s before continuing — stepping away from the "
                            "keyboard, as a player does. Nothing is stuck.%s",
                            _human_duration(gap_wait),
                            f" (next: {context})" if context else "",
                        )
                    await asyncio.sleep(gap_wait)
                    waited += gap_wait
                    now = time.monotonic()

            # Record this request
            self._last_request_time = now
            self._request_times.append(now)

            if waited > 0.5 and context:
                logger.debug(f"Throttled {waited:.1f}s before: {context}")

            return waited

    def _effective_gap(self, request_type: str = "page") -> float:
        """The target inter-request gap after session-tempo scaling.

        Tempo scales only the INCREMENT above the floor, never the whole gap:
        multiplying the whole gap by a <1 tempo and clamping with
        ``max(min_gap_s, …)`` pushed every low draw onto exactly ``min_gap_s``,
        resurrecting the floor point-mass ``_sample_gap`` exists to avoid. The
        increment is non-negative and tempo is positive, so the result is never
        below the floor and the density there stays zero.
        """
        if request_type in _CONSEQUENTIAL:
            # The page's own chatter. Recorded p50 0.00s, p75 0.10s, so this is
            # a small positive draw rather than a floor: enough that two
            # requests never share a timestamp exactly, not enough to read as a
            # decision. Tempo is not applied -- a browser firing a read-back
            # does not get tired.
            return random.lognormvariate(math.log(0.06), 0.9)
        target_gap = self._sample_gap()
        if self._tempo is not None:
            increment = max(0.0, (target_gap - self.min_gap_s) * self._tempo.current())
            target_gap = self.min_gap_s + increment
        return target_gap

    def _sample_gap(self) -> float:
        """Sample an inter-request gap from a right-skewed distribution.

        Occasionally very long. The same recorded session's largest gap was
        380.7 SECONDS -- six minutes of nothing, mid-session, with the tab still
        open. Nothing in this module could emit that: `max_gap_s` is 3.0 and the
        body of the distribution sits under it, so our traffic was continuous
        purposeful activity from the first request to the last. A person stops
        to read something, answers the door, reads a message.

        So a small fraction of DELIBERATE gaps are drawn from a second,
        much longer mode instead. Deliberate only -- a page's own subresources
        do not pause to think -- and rare enough that a short run usually has
        none, which is also what the recording shows.

        A uniform draw over ``[min_gap_s, max_gap_s]`` yields a flat gap
        histogram — the exact "uniform-random timing" pattern that statistical
        bot detectors flag with a KS test against real human traffic. Human
        inter-action gaps are heavy-tailed: most are short, a few are much
        longer. This uses a *shifted* log-normal so the gap is never below the
        configured floor (the increment is always non-negative, so no spike
        piles up at ``min_gap_s``), its body stays inside the configured band,
        and an occasional draw lands in a longer tail. The tail is soft-capped
        so a single draw can't stall a loop for minutes. The median fraction
        and sigma are per-session (see ``__init__``) so the shape is not a
        cross-account constant.
        """
        if random.random() < self.distraction_chance:
            # Log-normal so the tail is smooth rather than a uniform block: most
            # distractions are half a minute, a few are several. Returned before
            # the band below, because the whole point is a draw the band cannot
            # produce -- its support stops at `max_gap_s * 3`.
            # ON TOP of the floor, not instead of it. A bare log-normal around
            # 45s still puts a few draws at a couple of seconds -- a
            # "distraction" shorter than an ordinary gap, which is not a
            # distraction at all and quietly breaks this sampler's one hard
            # guarantee, that a deliberate gap is never under the floor.
            # Capped. The old soft cap (`max_gap_s * 3`) is what made a long
            # idle impossible, so this draw has to escape it -- but "a single
            # draw cannot stall a loop" was a fair rule and still is. 600s sits
            # above the longest gap the recording contains (380.7s) and bounds
            # the worst case at ten minutes rather than at nothing.
            return self.min_gap_s + min(
                random.lognormvariate(math.log(45.0), 1.0), _MAX_DISTRACTION_S
            )
        span = self.max_gap_s - self.min_gap_s
        if span <= 0:
            return self.min_gap_s
        increment = random.lognormvariate(math.log(span * self._gap_median_frac), self._gap_sigma)
        return min(self.min_gap_s + increment, self.max_gap_s * 3.0)

    async def wait_for_penalty(self) -> float:
        """Sleep out any outstanding penalty. Returns the seconds waited.
        Deliberately independent of ``enabled``. That flag is the operator's
        pacing preference — how fast to go when the server has said nothing —
        but a 429 is the server saying stop, and honouring it is not a
        preference. Turning pacing off used to remove the backoff with it,
        leaving a rate-limited loop with no brake at all.

        :meth:`wait` folds this into the paced gate, so a penalty is served
        once and only once; the unpaced path calls it directly.
        """
        now = time.monotonic()
        if now >= self._penalty_until:
            return 0.0
        penalty_wait = self._penalty_until - now
        logger.debug(f"Throttle penalty: waiting {penalty_wait:.1f}s")
        await asyncio.sleep(penalty_wait)
        return penalty_wait

    def add_penalty(self, seconds: float) -> None:
        """Add a temporary penalty (e.g., after receiving a suspicious response).

        Args:
            seconds: Extra delay to add before next request
        """
        now = time.monotonic()
        self._penalty_until = max(self._penalty_until, now + seconds)
        logger.warning(f"Throttle penalty added: {seconds:.0f}s cooldown")

    def _cleanup_burst_window(self, now: float) -> None:
        """Remove request timestamps outside the burst window."""
        cutoff = now - self.burst_window_s
        while self._request_times and self._request_times[0] < cutoff:
            self._request_times.popleft()

    @property
    def requests_in_window(self) -> int:
        """Number of requests in the current burst window."""
        self._cleanup_burst_window(time.monotonic())
        return len(self._request_times)
