"""Activity scheduling to prevent 24/7 patterns.

Travian Multihunters flag accounts active >10h/day from single IP.
Commercial bots typically limit to 10h/day, 5h continuous max,
with 2h minimum gap between sessions.

This scheduler tracks cumulative daily activity and enforces breaks
to prevent the most common automated-play detection heuristics.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import random
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

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
        state_file: Path | None = None,
    ):
        """
        Args:
            max_daily_hours: Maximum active hours per calendar day
            max_continuous_hours: Maximum hours before forced break
            min_break_minutes: Minimum break duration in minutes
            enabled: If False, no limits are enforced
            state_file: Optional path for persisting budget across restarts
        """
        self.max_daily_hours = max_daily_hours
        self.max_continuous_hours = max_continuous_hours
        self.min_break_minutes = min_break_minutes
        self.enabled = enabled

        self._daily_seconds: float = 0.0
        self._day_start: str = self._today_key()
        self._session_start: float = time.monotonic()
        self._session_seconds: float = 0.0
        self._last_activity_time: float = time.monotonic()

        self._state_file = state_file
        self._last_save_time: float = 0.0
        self._save_throttle_s: float = 30.0

        self._load_state()
        if self._state_file is not None:
            atexit.register(self._save_state_force)

    @staticmethod
    def _today_key() -> str:
        """Calendar day key for tracking daily limits."""
        return datetime.now().strftime("%Y-%m-%d")

    # ── State persistence ───────────────────────────────────────────

    def _load_state(self) -> None:
        """Load persisted budget state from disk."""
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
            saved_day = data.get("day_start", "")
            if saved_day == self._today_key():
                self._daily_seconds = float(data.get("daily_seconds", 0.0))
                self._day_start = saved_day

                # Check if enough real time elapsed since last save to
                # count as a break.  If so, reset session counter.
                last_saved = data.get("last_saved", 0)
                idle_since_save = time.time() - last_saved if last_saved else 0
                if idle_since_save >= self.min_break_minutes * 60:
                    self._session_seconds = 0.0
                    logger.info(
                        "Restored scheduler state: daily=%.1fh, "
                        "session reset (idle %.0fs since last save)",
                        self._daily_seconds / 3600.0,
                        idle_since_save,
                    )
                else:
                    self._session_seconds = float(data.get("session_seconds", 0.0))
                    logger.info(
                        "Restored scheduler state: daily=%.1fh, session=%.1fh",
                        self._daily_seconds / 3600.0,
                        self._session_seconds / 3600.0,
                    )
            else:
                logger.info(
                    "Scheduler state from %s ignored (new day %s)",
                    saved_day,
                    self._today_key(),
                )
        except Exception as e:
            logger.warning("Failed to load scheduler state from %s: %s", self._state_file, e)

    def _save_state(self) -> None:
        """Persist budget state to disk (throttled to once per 30 s)."""
        if self._state_file is None:
            return
        now = time.monotonic()
        if now - self._last_save_time < self._save_throttle_s:
            return
        self._save_state_force()

    def _save_state_force(self) -> None:
        """Write state to disk immediately (atomic via tempfile + replace)."""
        if self._state_file is None:
            return
        data = {
            "daily_seconds": self._daily_seconds,
            "day_start": self._day_start,
            "session_seconds": self._session_seconds,
            "last_saved": time.time(),
        }
        try:
            parent = self._state_file.parent
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp_path, str(self._state_file))
            except BaseException:
                # Clean up temp file on failure
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._last_save_time = time.monotonic()
            logger.debug("Saved scheduler state to %s", self._state_file)
        except Exception as e:
            logger.warning("Failed to save scheduler state: %s", e)

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

    def _auto_reset_session_if_idle(self) -> None:
        """Auto-reset session counter if enough idle time has passed.

        If no activity has been logged for at least ``min_break_minutes``,
        the user effectively took a break — reset the session counter so
        they aren't permanently locked out by a stale session_seconds
        value from the state file.
        """
        idle_seconds = time.monotonic() - self._last_activity_time
        if idle_seconds >= self.min_break_minutes * 60:
            if self._session_seconds > 0:
                logger.info(
                    "Session auto-reset: idle %.0fs >= break threshold %.0fs "
                    "(was %.1fh session)",
                    idle_seconds,
                    self.min_break_minutes * 60,
                    self._session_seconds / 3600.0,
                )
                self._session_seconds = 0.0
                self._session_start = time.monotonic()

    def can_continue(self) -> bool:
        """Check if we're within daily/continuous limits.

        Returns:
            True if we can keep working, False if break needed.
        """
        if not self.enabled:
            return True

        self._reset_daily_if_new_day()
        self._auto_reset_session_if_idle()

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
        self._last_activity_time = time.monotonic()
        self._save_state()

    def start_session(self) -> None:
        """Start a new session (call after a break)."""
        self._session_start = time.monotonic()
        self._session_seconds = 0.0
        logger.debug("New session started")
        self._save_state()

    @property
    def daily_hours_used(self) -> float:
        """Hours active today."""
        self._reset_daily_if_new_day()
        return self._daily_seconds / 3600.0

    @property
    def session_hours(self) -> float:
        """Hours in current continuous session."""
        return self._session_seconds / 3600.0
