"""Session lifetime management.

Real players don't stay logged in 24/7 with constant activity. They:
- Play for 30-90 minutes, then go AFK
- Have irregular activity patterns (busy mornings, idle afternoons)
- Sometimes leave the game tab open without doing anything
- Come back after hours and need to refresh

This module tracks session age and activity patterns to:
- Suggest breaks (caller decides when to actually pause)
- Detect if the session has been "too active" for too long
- Provide human-like session envelope metadata
"""

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SessionStats:
    """Tracks session activity for pattern analysis."""
    started_at: float = field(default_factory=time.monotonic)
    last_action_at: float = 0
    total_actions: int = 0
    total_requests: int = 0
    
    @property
    def session_age_s(self) -> float:
        return time.monotonic() - self.started_at
    
    @property
    def session_age_min(self) -> float:
        return self.session_age_s / 60
    
    @property
    def actions_per_minute(self) -> float:
        age = self.session_age_min
        return self.total_actions / age if age > 0 else 0
    
    def record_action(self) -> None:
        self.total_actions += 1
        self.last_action_at = time.monotonic()
    
    def record_request(self) -> None:
        self.total_requests += 1


class SessionManager:
    """Manages session lifetime and activity patterns.
    
    Usage:
        sm = SessionManager()
        sm.record_action()
        
        if sm.should_take_break():
            # pause for a while
            await asyncio.sleep(sm.suggested_break_duration())
            sm.reset_session()
    """
    
    def __init__(
        self,
        max_session_min: float = 90.0,
        min_session_min: float = 30.0,
        max_actions_per_min: float = 3.0,
        break_min_s: float = 300.0,   # 5 min minimum break
        break_max_s: float = 1800.0,  # 30 min maximum break
        enabled: bool = True,
    ):
        """
        Args:
            max_session_min: Max session length before suggesting a break
            min_session_min: Minimum session length before break is possible
            max_actions_per_min: Actions/min threshold that triggers break suggestion
            break_min_s: Minimum break duration in seconds
            break_max_s: Maximum break duration in seconds
            enabled: If False, never suggests breaks
        """
        self.max_session_min = max_session_min
        self.min_session_min = min_session_min
        self.max_actions_per_min = max_actions_per_min
        self.break_min_s = break_min_s
        self.break_max_s = break_max_s
        self.enabled = enabled
        
        self._stats = SessionStats()
        # Randomize the actual max session time for this session (±20%)
        self._session_limit = max_session_min * random.uniform(0.8, 1.2)
    
    @property
    def stats(self) -> SessionStats:
        return self._stats
    
    def record_action(self) -> None:
        """Record that a user-initiated action was performed."""
        self._stats.record_action()
    
    def record_request(self) -> None:
        """Record an HTTP request."""
        self._stats.record_request()
    
    def should_take_break(self) -> bool:
        """Check if the session should take a break.
        
        Returns True if:
        - Session has been active longer than the randomized limit
        - OR action rate is suspiciously high for too long
        """
        if not self.enabled:
            return False
        
        # Too early for a break
        if self._stats.session_age_min < self.min_session_min:
            return False
        
        # Session too long
        if self._stats.session_age_min >= self._session_limit:
            logger.info(f"Session break suggested: {self._stats.session_age_min:.0f} min "
                       f"(limit: {self._session_limit:.0f} min)")
            return True
        
        # Too many actions per minute (sustained)
        if (self._stats.session_age_min > 10 and 
                self._stats.actions_per_minute > self.max_actions_per_min):
            logger.info(f"Session break suggested: {self._stats.actions_per_minute:.1f} "
                       f"actions/min (limit: {self.max_actions_per_min})")
            return True
        
        return False
    
    def suggested_break_duration(self) -> float:
        """Suggest a break duration in seconds."""
        return random.uniform(self.break_min_s, self.break_max_s)
    
    def reset_session(self) -> None:
        """Reset session stats (call after a break)."""
        self._stats = SessionStats()
        self._session_limit = self.max_session_min * random.uniform(0.8, 1.2)
        logger.info("Session reset — new activity window started")
