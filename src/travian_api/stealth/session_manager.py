"""Session lifetime management with breaks.

Real players don't stay logged in 24/7 making requests every 30 seconds.
They have sessions of 10-60 minutes, take breaks, come back later.

This module tracks session duration and suggests/enforces breaks to
make the usage pattern look natural.
"""

import logging
import random
import time
from typing import Optional

logger = logging.getLogger(__name__)


class SessionManager:
    """Manages session lifetime to mimic human play patterns.
    
    Tracks how long the current session has been active and
    how many actions have been performed. Signals when it's
    time to take a break.
    
    Usage:
        sm = SessionManager()
        sm.start_session()
        
        while doing_stuff:
            if sm.should_break():
                break_duration = sm.get_break_duration()
                await asyncio.sleep(break_duration)
                sm.start_session()  # new session
            
            sm.record_action()
    """
    
    def __init__(
        self,
        min_session_minutes: float = 15.0,
        max_session_minutes: float = 90.0,
        min_break_minutes: float = 1.0,
        max_break_minutes: float = 8.0,
        max_actions_per_session: int = 150,
        enabled: bool = True,
    ):
        self.min_session_minutes = min_session_minutes
        self.max_session_minutes = max_session_minutes
        self.min_break_minutes = min_break_minutes
        self.max_break_minutes = max_break_minutes
        self.max_actions_per_session = max_actions_per_session
        self.enabled = enabled
        
        self._session_start: float = 0
        self._session_target_duration: float = 0  # seconds
        self._action_count: int = 0
        self._total_sessions: int = 0
        self._total_break_time: float = 0
    
    def start_session(self) -> None:
        """Start a new session (call at login or after a break)."""
        self._session_start = time.monotonic()
        self._action_count = 0
        self._total_sessions += 1
        
        # Random session duration target
        self._session_target_duration = random.uniform(
            self.min_session_minutes * 60,
            self.max_session_minutes * 60,
        )
        
        logger.debug(
            f"Session #{self._total_sessions} started. "
            f"Target duration: {self._session_target_duration / 60:.0f}m"
        )
    
    def record_action(self) -> None:
        """Record that an action was performed."""
        self._action_count += 1
    
    def should_break(self) -> bool:
        """Check if it's time to take a break.
        
        Returns:
            True if a break is recommended
        """
        if not self.enabled:
            return False
        
        if self._session_start == 0:
            return False
        
        elapsed = time.monotonic() - self._session_start
        
        # Time-based: exceeded target session duration
        if elapsed > self._session_target_duration:
            logger.info(
                f"Session break recommended: {elapsed / 60:.0f}m elapsed "
                f"(target was {self._session_target_duration / 60:.0f}m)"
            )
            return True
        
        # Action-based: too many actions in one session
        if self._action_count >= self.max_actions_per_session:
            logger.info(
                f"Session break recommended: {self._action_count} actions "
                f"(max {self.max_actions_per_session})"
            )
            return True
        
        return False
    
    def get_break_duration(self) -> float:
        """Get recommended break duration in seconds."""
        duration = random.uniform(
            self.min_break_minutes * 60,
            self.max_break_minutes * 60,
        )
        self._total_break_time += duration
        return duration
    
    @property
    def session_elapsed_s(self) -> float:
        """Seconds since current session started."""
        if self._session_start == 0:
            return 0
        return time.monotonic() - self._session_start
    
    @property
    def session_actions(self) -> int:
        """Number of actions in current session."""
        return self._action_count
    
    @property
    def total_sessions(self) -> int:
        """Total number of sessions."""
        return self._total_sessions
