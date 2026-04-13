"""Global request rate limiter.

Ensures a minimum time gap between any two requests to the game server.
Prevents machine-gun request patterns that are trivially detectable.

Also tracks request counts per time window for burst detection.
"""

import asyncio
import logging
import random
import time
from collections import deque
from typing import Optional

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
            min_gap_s: Minimum seconds between requests
            max_gap_s: Maximum seconds between requests (random within range)
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
    
    def set_captcha_guard(self, guard) -> None:
        """Attach a CaptchaGuard so requests block when captcha is active."""
        self._captcha_guard = guard

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
                    logger.info(f"Burst limit reached ({len(self._request_times)} reqs in {self.burst_window_s}s). "
                               f"Cooling down {burst_wait:.1f}s before: {context}")
                else:
                    logger.info(f"Burst limit reached. Cooling down {burst_wait:.1f}s")
                await asyncio.sleep(burst_wait)
                waited += burst_wait
                now = time.monotonic()
                self._cleanup_burst_window(now)
            
            # Enforce minimum gap with jitter
            if self._last_request_time > 0:
                elapsed = now - self._last_request_time
                target_gap = random.uniform(self.min_gap_s, self.max_gap_s)
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
