"""Activity scheduling to prevent 24/7 patterns.

Travian Multihunters flag accounts active >10h/day from single IP.
Commercial bots typically limit to 10h/day, 5h continuous max,
with 2h minimum gap between sessions.

This scheduler tracks cumulative daily activity and enforces breaks
to prevent the most common automated-play detection heuristics.
"""

import logging
import random
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ActivityScheduler:
    """Enforces realistic play session boundaries.

    Tracks:
    - Total daily active hours
    - Continuous session duration
    - Break timing and duration

    Usage:
        scheduler = ActivityScheduler(max_daily_hours=10.0)

        while running:
            if not scheduler.can_continue():
                break_s = scheduler.next_break_duration()
                await asyncio.sleep(break_s)
                scheduler.start_session()

            # ... do work ...
            scheduler.log_activity(elapsed_seconds)
    """

    def __init__(
        self,
        max_daily_hours: float = 10.0,
        max_continuous_hours: float = 4.0,
        min_break_minutes: float = 30.0,
        enabled: bool = True,
    ):
        """
        Args:
            max_daily_hours: Maximum active hours per calendar day
            max_continuous_hours: Maximum hours before forced break
            min_break_minutes: Minimum break duration in minutes
            enabled: If False, no limits are enforced
        """
        self.max_daily_hours = max_daily_hours
        self.max_continuous_hours = max_continuous_hours
        self.min_break_minutes = min_break_minutes
        self.enabled = enabled

        self._daily_seconds: float = 0.0
        self._day_start: str = self._today_key()
        self._session_start: float = time.monotonic()
        self._session_seconds: float = 0.0

    @staticmethod
    def _today_key() -> str:
        """Calendar day key for tracking daily limits."""
        return datetime.now().strftime("%Y-%m-%d")

    def _reset_daily_if_new_day(self) -> None:
        """Reset daily counter if we've crossed midnight."""
        today = self._today_key()
        if today != self._day_start:
            logger.info(
                f"New day ({today}): resetting daily activity counter "
                f"(yesterday: {self._daily_seconds / 3600:.1f}h)"
            )
            self._daily_seconds = 0.0
            self._day_start = today

    def can_continue(self) -> bool:
        """Check if we're within daily/continuous limits.

        Returns:
            True if we can keep working, False if break needed.
        """
        if not self.enabled:
            return True

        self._reset_daily_if_new_day()

        # Check daily limit
        daily_hours = self._daily_seconds / 3600.0
        if daily_hours >= self.max_daily_hours:
            logger.info(
                f"Daily limit reached: {daily_hours:.1f}h / {self.max_daily_hours}h"
            )
            return False

        # Check continuous session limit
        session_hours = self._session_seconds / 3600.0
        if session_hours >= self.max_continuous_hours:
            logger.info(
                f"Continuous limit reached: {session_hours:.1f}h / "
                f"{self.max_continuous_hours}h"
            )
            return False

        return True

    def next_break_duration(self) -> float:
        """How long to break (in seconds).

        Short break (mid-session): min_break_minutes + random 0-15 min
        Long break (daily limit approaching): 2-6 hours
        Night break (if past 11pm local): 6-9 hours

        Returns:
            Break duration in seconds
        """
        if not self.enabled:
            return 0.0

        hour = datetime.now().hour
        daily_hours = self._daily_seconds / 3600.0

        # Night break: if it's late, take a long rest
        if hour >= 23 or hour < 6:
            duration_h = random.uniform(6.0, 9.0)
            logger.info(f"Night break: sleeping {duration_h:.1f}h")
            return duration_h * 3600.0

        # Daily limit approaching (>80% used): long break
        if daily_hours >= self.max_daily_hours * 0.8:
            duration_h = random.uniform(2.0, 6.0)
            logger.info(f"Long break (daily limit near): {duration_h:.1f}h")
            return duration_h * 3600.0

        # Standard mid-session break
        base_minutes = self.min_break_minutes
        extra_minutes = random.uniform(0.0, 15.0)
        duration_s = (base_minutes + extra_minutes) * 60.0
        logger.info(f"Short break: {duration_s / 60:.0f} minutes")
        return duration_s

    def remaining_daily_budget(self) -> float:
        """Hours remaining in today's activity budget."""
        self._reset_daily_if_new_day()
        remaining = self.max_daily_hours - (self._daily_seconds / 3600.0)
        return max(0.0, remaining)

    def log_activity(self, seconds: float) -> None:
        """Record that we were active for N seconds.

        Args:
            seconds: Duration of activity to log
        """
        self._daily_seconds += seconds
        self._session_seconds += seconds

    def start_session(self) -> None:
        """Start a new session (call after a break)."""
        self._session_start = time.monotonic()
        self._session_seconds = 0.0
        logger.debug("New session started")

    @property
    def daily_hours_used(self) -> float:
        """Hours active today."""
        self._reset_daily_if_new_day()
        return self._daily_seconds / 3600.0

    @property
    def session_hours(self) -> float:
        """Hours in current continuous session."""
        return self._session_seconds / 3600.0
