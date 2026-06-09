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
        burst_max_requests: int = 20,
        burst_cooldown_s: float = 15.0,
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
        self.enabled = enabled

        self._last_request_time: float = 0
        self._request_times: deque = deque()
        self._lock = asyncio.Lock()
        self._penalty_until: float = 0  # extra penalty from errors
        self._captcha_guard = None  # set via set_captcha_guard()

        # Per-session gap-shape parameters. Drawn once per instance instead of
        # using one global constant so two accounts on the same config don't
        # emit an identical *normalized* gap shape — a cross-account
        # likelihood-ratio/KS test could otherwise fingerprint the shared
        # generator even though each marginal passes a uniform-rejection test.
        self._gap_median_frac = random.uniform(0.30, 0.48)
        self._gap_sigma = random.uniform(0.45, 0.85)

    def set_captcha_guard(self, guard) -> None:
        """Attach a CaptchaGuard so requests block when captcha is active."""
        self._captcha_guard = guard

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

    async def wait(self, context: str = "") -> float:
        """Wait until it's safe to make the next request.

        Args:
            context: Optional description for logging (e.g., "upgrade building")

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
            if now < self._penalty_until:
                penalty_wait = self._penalty_until - now
                logger.debug(f"Throttle penalty: waiting {penalty_wait:.1f}s")
                await asyncio.sleep(penalty_wait)
                waited += penalty_wait
                now = time.monotonic()

            # Check burst limit
            self._cleanup_burst_window(now)
            if len(self._request_times) >= self.burst_max_requests:
                burst_wait = self.burst_cooldown_s + random.uniform(2.0, 8.0)
                if context:
                    logger.info(
                        f"Burst limit reached ({len(self._request_times)} reqs in {self.burst_window_s}s). "
                        f"Cooling down {burst_wait:.1f}s before: {context}"
                    )
                else:
                    logger.info(f"Burst limit reached. Cooling down {burst_wait:.1f}s")
                await asyncio.sleep(burst_wait)
                waited += burst_wait
                now = time.monotonic()
                self._cleanup_burst_window(now)

            # Enforce minimum gap with heavy-tailed jitter
            if self._last_request_time > 0:
                elapsed = now - self._last_request_time
                target_gap = self._sample_gap()
                if elapsed < target_gap:
                    gap_wait = target_gap - elapsed
                    await asyncio.sleep(gap_wait)
                    waited += gap_wait
                    now = time.monotonic()

            # Record this request
            self._last_request_time = now
            self._request_times.append(now)

            if waited > 0.5 and context:
                logger.debug(f"Throttled {waited:.1f}s before: {context}")

            return waited

    def _sample_gap(self) -> float:
        """Sample an inter-request gap from a right-skewed distribution.

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
        span = self.max_gap_s - self.min_gap_s
        if span <= 0:
            return self.min_gap_s
        increment = random.lognormvariate(math.log(span * self._gap_median_frac), self._gap_sigma)
        return min(self.min_gap_s + increment, self.max_gap_s * 3.0)

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
