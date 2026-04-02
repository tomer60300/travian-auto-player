"""Human-like delay patterns for different actions.

Real players have predictable delay distributions:
- Reading a page: 2-8 seconds
- Clicking a button after reading: 0.5-3 seconds  
- Between sequential actions: 3-12 seconds
- Thinking before a big decision: 5-20 seconds
- Between raids in farm list: 1-4 seconds

These aren't uniform random — humans tend toward the lower end with
occasional longer pauses (log-normal distribution).
"""

import asyncio
import logging
import random
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions with different delay profiles."""
    PAGE_READ = "page_read"          # viewing a page before acting
    BUTTON_CLICK = "button_click"    # clicking after reading
    BETWEEN_ACTIONS = "between_actions"  # gap between sequential actions
    THINKING = "thinking"            # before a significant decision
    FARM_SEND = "farm_send"          # between farm list sends
    SCOUT_SEND = "scout_send"        # between scout sends
    FORM_FILL = "form_fill"          # filling in form fields
    NAVIGATION = "navigation"        # navigating between pages
    VIDEO_TICK = "video_tick"        # ATG video progress ticks


# Delay profiles: (min_s, mode_s, max_s)
# Uses triangular distribution — mode is most likely value
_DELAY_PROFILES = {
    ActionType.PAGE_READ:       (1.5, 3.0, 8.0),
    ActionType.BUTTON_CLICK:    (0.3, 0.8, 2.5),
    ActionType.BETWEEN_ACTIONS: (2.0, 4.0, 12.0),
    ActionType.THINKING:        (3.0, 7.0, 20.0),
    ActionType.FARM_SEND:       (0.8, 1.5, 4.0),
    ActionType.SCOUT_SEND:      (1.0, 2.0, 5.0),
    ActionType.FORM_FILL:       (0.5, 1.0, 3.0),
    ActionType.NAVIGATION:      (0.8, 1.5, 4.0),
    ActionType.VIDEO_TICK:      (2.5, 3.0, 3.8),  # tighter for ATG timing
}


class HumanDelay:
    """Generates and applies human-like delays.
    
    Uses triangular distribution to model human behavior —
    most delays cluster around the mode with occasional outliers.
    
    Usage:
        delay = HumanDelay()
        await delay.wait(ActionType.PAGE_READ, "viewing dorf1")
        await delay.wait(ActionType.BUTTON_CLICK, "clicking upgrade")
    """
    
    def __init__(self, speed_factor: float = 1.0, enabled: bool = True):
        """
        Args:
            speed_factor: Multiplier for all delays. 
                         1.0 = normal human speed
                         0.5 = faster (risky)
                         2.0 = slower (safer)
            enabled: If False, no delays (for testing)
        """
        self.speed_factor = max(0.1, speed_factor)
        self.enabled = enabled
        self._total_waited = 0.0
    
    def get_delay(self, action: ActionType) -> float:
        """Calculate delay for an action type.
        
        Returns:
            Seconds to wait
        """
        if not self.enabled:
            return 0.0
        
        profile = _DELAY_PROFILES.get(action, (1.0, 2.0, 5.0))
        min_s, mode_s, max_s = profile
        
        # Triangular distribution — clusters around mode
        delay = random.triangular(min_s, max_s, mode_s)
        
        # Apply speed factor
        delay *= self.speed_factor
        
        # Occasionally add an extra "distraction" pause (5% chance)
        # Simulates human checking phone, Alt-Tab, etc.
        if random.random() < 0.05:
            delay += random.uniform(3.0, 15.0)
        
        return delay
    
    async def wait(self, action: ActionType, context: str = "") -> float:
        """Wait with a human-like delay.
        
        Args:
            action: Type of action determining delay profile
            context: Optional description for logging
            
        Returns:
            Actual seconds waited
        """
        delay = self.get_delay(action)
        
        if delay > 0:
            if delay > 5.0 and context:
                logger.debug(f"Human delay: {delay:.1f}s before {context}")
            await asyncio.sleep(delay)
            self._total_waited += delay
        
        return delay
    
    @property
    def total_waited(self) -> float:
        """Total seconds spent in human delays this session."""
        return self._total_waited
