"""Human-like random delays between actions.

Models realistic human interaction timing:
- Reading a page: 2-8 seconds
- Clicking a button: 0.5-2 seconds
- Thinking/deciding: 3-12 seconds
- Between rapid actions: 1-3 seconds
- After login: 2-5 seconds

All delays use a skewed distribution (most delays are on the shorter end
with occasional longer pauses, matching real human behavior).
"""

import asyncio
import logging
import random
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions with different timing profiles."""
    PAGE_LOAD = "page_load"          # Loading a new page (reading it)
    CLICK = "click"                   # Clicking a button/link
    FORM_FILL = "form_fill"          # Filling form fields
    DECISION = "decision"             # Thinking before an action
    RAPID = "rapid"                   # Quick sequential actions
    POST_LOGIN = "post_login"        # After logging in
    BETWEEN_RAIDS = "between_raids"  # Between sending farm list raids
    BETWEEN_SCOUTS = "between_scouts" # Between sending scouts
    PRE_UPGRADE = "pre_upgrade"      # Before starting a building upgrade
    VIDEO_TICK = "video_tick"        # Between video reward ticks


# Timing profiles: (min_s, mode_s, max_s) — uses triangular distribution
# mode_s is the most likely value
_TIMING_PROFILES = {
    ActionType.PAGE_LOAD:      (0.8, 1.5, 4.0),
    ActionType.CLICK:          (0.2, 0.5, 1.5),
    ActionType.FORM_FILL:      (0.5, 1.2, 3.0),
    ActionType.DECISION:       (1.0, 2.5, 7.0),
    ActionType.RAPID:          (0.4, 0.8, 2.0),
    ActionType.POST_LOGIN:     (1.5, 2.5, 4.0),
    ActionType.BETWEEN_RAIDS:  (0.8, 1.5, 3.5),
    ActionType.BETWEEN_SCOUTS: (1.0, 2.0, 4.0),
    ActionType.PRE_UPGRADE:    (1.0, 2.0, 5.0),
    ActionType.VIDEO_TICK:     (2.5, 3.0, 3.8),  # Tight range — ATG expects ~3s
}


class HumanDelay:
    """Generates human-like delays for different action types.
    
    Uses triangular distribution for natural timing variation.
    Occasionally adds "micro-pauses" (someone got distracted) and
    "burst mode" (experienced player clicking fast).
    
    Usage:
        delay = HumanDelay()
        await delay.wait(ActionType.PAGE_LOAD, "checking dorf1")
        await delay.wait(ActionType.PRE_UPGRADE, "upgrading cranny")
    """
    
    def __init__(self, speed_factor: float = 1.0, enabled: bool = True):
        """
        Args:
            speed_factor: Multiplier for all delays. 0.5 = twice as fast, 2.0 = twice as slow.
                         Values below 0.3 are risky. Default 1.0.
            enabled: If False, no delays are added (for testing).
        """
        self.speed_factor = max(0.1, speed_factor)
        self.enabled = enabled
        self._action_count = 0
    
    async def wait(self, action: ActionType, context: str = "") -> float:
        """Wait for a human-like delay appropriate to the action type.
        
        Args:
            action: Type of action being performed
            context: Optional description for logging
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        self._action_count += 1
        
        # Get base timing
        min_s, mode_s, max_s = _TIMING_PROFILES[action]
        delay = random.triangular(min_s, max_s, mode_s) * self.speed_factor
        
        # Occasional micro-pause (5% chance): someone looked away, phone rang, etc.
        if random.random() < 0.05 and action not in (ActionType.VIDEO_TICK, ActionType.RAPID):
            pause = random.uniform(5.0, 20.0)
            delay += pause
            logger.debug(f"Micro-pause: +{pause:.1f}s (distraction)")
        
        # Every 15-25 actions, add a longer think pause (simulates checking map, 
        # reading messages, or just being idle)
        if self._action_count % random.randint(15, 25) == 0:
            think = random.uniform(10.0, 30.0)
            delay += think
            logger.debug(f"Think pause: +{think:.1f}s (periodic idle)")
        
        if delay > 0:
            if context:
                logger.debug(f"Human delay {delay:.1f}s before: {context}")
            await asyncio.sleep(delay)
        
        return delay
    
    async def wait_range(self, min_s: float, max_s: float, context: str = "") -> float:
        """Wait for a custom random duration.
        
        Args:
            min_s: Minimum seconds
            max_s: Maximum seconds
            context: Optional description
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        delay = random.uniform(min_s, max_s) * self.speed_factor
        if delay > 0:
            if context:
                logger.debug(f"Custom delay {delay:.1f}s: {context}")
            await asyncio.sleep(delay)
        return delay
    
    async def jitter(self, base_s: float, variance: float = 0.3) -> float:
        """Add jitter to a fixed timing value.
        
        Args:
            base_s: Base delay in seconds
            variance: Fraction of variance (0.3 = ±30%)
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        jitter_range = base_s * variance
        delay = base_s + random.uniform(-jitter_range, jitter_range)
        delay = max(0.1, delay * self.speed_factor)
        await asyncio.sleep(delay)
        return delay
