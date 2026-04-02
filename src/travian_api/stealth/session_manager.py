"""Session lifetime management with breaks.

A real player doesn't stay logged in 24/7 with constant activity.
This module tracks session duration and suggests breaks to avoid
detection patterns like "active for 18 hours straight."

Also provides idle browsing during long waits (e.g., waiting for
a build to finish) so the session looks like a player AFK-checking.
"""

import asyncio
import logging
import random
import time
from typing import Optional, TYPE_CHECKING

from .human_delay import HumanDelay, ActionType

if TYPE_CHECKING:
    from .navigator import PageNavigator
    from ..clients.http_client import HttpClient

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages session lifetime and activity patterns.
    
    Features:
    - Tracks session start time and total active duration
    - Suggests breaks after configurable active periods
    - Performs idle browsing during long waits
    - Randomizes activity patterns to avoid regularity
    """
    
    def __init__(
        self,
        max_active_minutes: int = 120,
        break_minutes: tuple = (5, 15),
        idle_browse_interval_s: float = 300.0,
        enabled: bool = True,
    ):
        """
        Args:
            max_active_minutes: Suggest a break after this many active minutes
            break_minutes: (min, max) break duration in minutes
            idle_browse_interval_s: Seconds between idle page visits during waits
            enabled: If False, session management is disabled
        """
        self.max_active_minutes = max_active_minutes
        self.break_minutes = break_minutes
        self.idle_browse_interval_s = idle_browse_interval_s
        self.enabled = enabled
        
        self._session_start = time.monotonic()
        self._last_idle_browse = time.monotonic()
        self._total_requests = 0
        self._break_count = 0
    
    @property
    def session_duration_minutes(self) -> float:
        """Minutes since session started."""
        return (time.monotonic() - self._session_start) / 60.0
    
    @property
    def should_take_break(self) -> bool:
        """Whether a break is recommended."""
        if not self.enabled:
            return False
        return self.session_duration_minutes >= self.max_active_minutes
    
    async def take_break_if_needed(self) -> float:
        """Take a break if session has been active too long.
        
        Returns:
            Seconds spent on break (0 if no break taken)
        """
        if not self.should_take_break:
            return 0.0
        
        min_break, max_break = self.break_minutes
        break_s = random.uniform(min_break * 60, max_break * 60)
        
        self._break_count += 1
        logger.info(f"Session break #{self._break_count}: pausing {break_s/60:.1f} minutes "
                    f"(active for {self.session_duration_minutes:.0f}min)")
        
        await asyncio.sleep(break_s)
        
        # Reset session timer after break
        self._session_start = time.monotonic()
        
        return break_s
    
    async def idle_browse_if_due(
        self,
        navigator: "PageNavigator",
        http_client: "HttpClient",
        village_id: Optional[int] = None,
    ) -> bool:
        """Perform an idle page visit if enough time has passed.
        
        Call this during long polling loops (e.g., waiting for build to finish).
        
        Returns:
            True if an idle browse was performed
        """
        if not self.enabled:
            return False
        
        now = time.monotonic()
        elapsed = now - self._last_idle_browse
        
        # Add some randomness to the interval (±30%)
        jittered_interval = self.idle_browse_interval_s * random.uniform(0.7, 1.3)
        
        if elapsed >= jittered_interval:
            await navigator.idle_browse(http_client, village_id)
            self._last_idle_browse = time.monotonic()
            return True
        
        return False
    
    def record_request(self) -> None:
        """Record that a request was made."""
        self._total_requests += 1
    
    def reset(self) -> None:
        """Reset session tracking (e.g., after re-login)."""
        self._session_start = time.monotonic()
        self._last_idle_browse = time.monotonic()
        self._total_requests = 0
