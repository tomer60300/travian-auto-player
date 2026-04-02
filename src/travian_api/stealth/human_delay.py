"""Human-like random delays between actions.

Models the timing patterns of a real person playing Travian:
- Reading a page (2-6s)
- Deciding what to build (3-10s)
- Clicking through menus (1-3s)
- Pausing between raids (2-5s)
- Occasional longer pauses (thinking, distracted)

Uses a weighted random distribution that favors the middle of the range
(normal distribution clamped to bounds), because humans don't uniformly
distribute their action timing.
"""

import asyncio
import logging
import random
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions with different timing profiles."""
    
    # Quick actions — clicking a known button
    CLICK = "click"
    
    # Page navigation — loading and scanning a page
    PAGE_LOAD = "page_load"
    
    # Decision making — choosing what to build/train
    DECISION = "decision"
    
    # Between sequential operations (e.g., multiple raids)
    SEQUENCE_STEP = "sequence_step"
    
    # Between major operations (e.g., build then raid)
    BETWEEN_TASKS = "between_tasks"
    
    # Reading reports/messages
    READING = "reading"
    
    # Form filling (entering coordinates, troop counts)
    FORM_FILL = "form_fill"


# (min_s, typical_s, max_s) — typical is the center of the distribution
_TIMING_PROFILES = {
    ActionType.CLICK:          (0.3, 0.8, 2.0),
    ActionType.PAGE_LOAD:      (1.5, 3.0, 6.0),
    ActionType.DECISION:       (2.0, 5.0, 12.0),
    ActionType.SEQUENCE_STEP:  (1.0, 2.5, 5.0),
    ActionType.BETWEEN_TASKS:  (3.0, 8.0, 20.0),
    ActionType.READING:        (2.0, 4.0, 8.0),
    ActionType.FORM_FILL:      (1.0, 2.0, 4.0),
}

# Probability of an extra "distraction" pause (checking phone, thinking, etc.)
_DISTRACTION_CHANCE = 0.08  # 8% of actions get an extra 5-30s pause


class HumanDelay:
    """Generates human-like delays between actions.
    
    Usage:
        delay = HumanDelay()
        await delay.wait(ActionType.PAGE_LOAD, "loading dorf1")
        await delay.wait(ActionType.CLICK, "clicking upgrade")
    """
    
    def __init__(self, speed_multiplier: float = 1.0, enabled: bool = True):
        """
        Args:
            speed_multiplier: 0.5 = twice as fast, 2.0 = twice as slow.
                             Use 0.7-0.8 for experienced players, 1.2-1.5 for cautious.
            enabled: If False, delays are skipped (for testing).
        """
        self.speed_multiplier = max(0.1, speed_multiplier)
        self.enabled = enabled
    
    async def wait(self, action: ActionType, context: str = "") -> float:
        """Wait a human-like duration for the given action type.
        
        Args:
            action: Type of action being performed
            context: Optional description for logging
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        delay = self._generate_delay(action)
        
        # Apply speed multiplier
        delay *= self.speed_multiplier
        
        # Occasional distraction pause
        if random.random() < _DISTRACTION_CHANCE:
            extra = random.uniform(5.0, 30.0)
            logger.debug(f"Distraction pause: +{extra:.1f}s")
            delay += extra
        
        if delay > 0.1:
            if context:
                logger.debug(f"Human delay {delay:.1f}s ({action.value}): {context}")
            await asyncio.sleep(delay)
        
        return delay
    
    async def wait_custom(self, min_s: float, max_s: float, context: str = "") -> float:
        """Wait a random duration within a custom range.
        
        Args:
            min_s: Minimum seconds
            max_s: Maximum seconds
            context: Optional description
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        delay = self._gaussian_range(min_s, (min_s + max_s) / 2, max_s)
        delay *= self.speed_multiplier
        
        if delay > 0.1:
            if context:
                logger.debug(f"Custom delay {delay:.1f}s: {context}")
            await asyncio.sleep(delay)
        
        return delay
    
    def _generate_delay(self, action: ActionType) -> float:
        """Generate a delay from the action's timing profile."""
        min_s, typical_s, max_s = _TIMING_PROFILES[action]
        return self._gaussian_range(min_s, typical_s, max_s)
    
    @staticmethod
    def _gaussian_range(min_s: float, typical_s: float, max_s: float) -> float:
        """Generate a value from a truncated normal distribution.
        
        Centers around `typical_s` with the range [min_s, max_s].
        More realistic than uniform random — humans cluster around typical timing.
        """
        # Standard deviation = 1/3 of the range from typical to boundary
        range_below = typical_s - min_s
        range_above = max_s - typical_s
        sigma = max(range_below, range_above) / 3.0
        
        # Generate and clamp
        value = random.gauss(typical_s, sigma)
        return max(min_s, min(max_s, value))
