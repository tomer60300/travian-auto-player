"""Human-like delay patterns for different action types.

Real players don't act at machine speed. This module provides
context-aware delays that mimic human behavior:
- Quick actions (clicking a link): 0.5-2s
- Reading a page before acting: 2-6s
- Thinking before a decision (attack, upgrade): 3-10s
- Between steps in a multi-step flow: 1-4s
"""

import asyncio
import logging
import random
from enum import Enum

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions for delay selection."""
    PAGE_LOAD = "page_load"
    BEFORE_ACTION = "before_action"
    BETWEEN_STEPS = "between_steps"
    QUICK_CLICK = "quick_click"
    THINK = "think"
    BETWEEN_RAIDS = "between_raids"
    BETWEEN_SCOUTS = "between_scouts"
    SESSION_START = "session_start"
    BETWEEN_TASKS = "between_tasks"


# Delay ranges per action type: (min_s, max_s)
_DELAY_RANGES = {
    ActionType.PAGE_LOAD: (1.0, 4.0),
    ActionType.BEFORE_ACTION: (2.0, 6.0),
    ActionType.BETWEEN_STEPS: (1.5, 4.0),
    ActionType.QUICK_CLICK: (0.5, 2.0),
    ActionType.THINK: (3.0, 10.0),
    ActionType.BETWEEN_RAIDS: (0.8, 2.5),
    ActionType.BETWEEN_SCOUTS: (1.5, 4.0),
    ActionType.SESSION_START: (3.0, 8.0),
    ActionType.BETWEEN_TASKS: (2.0, 5.0),
}


class HumanDelay:
    """Provides human-like delays for different game actions.
    
    Each delay is randomized within a range. The ranges are tuned to
    match observed human behavior in browser games.
    
    Multiplier allows global speed adjustment:
    - 1.0 = normal human speed
    - 0.5 = faster (still looks human, just efficient)
    - 2.0 = slower (very cautious)
    """
    
    def __init__(self, multiplier: float = 1.0, speed_multiplier: float = None, enabled: bool = True):
        """
        Args:
            multiplier: Global speed multiplier (1.0 = normal)
            speed_multiplier: Alias for multiplier (for backward compat)
            enabled: If False, all delays return immediately
        """
        self.multiplier = max(0.1, speed_multiplier if speed_multiplier is not None else multiplier)
        self.enabled = enabled
    
    async def page_load(self) -> float:
        """Delay after loading a page (simulates reading/scanning).
        
        A real player glances at the page for 1-4 seconds before clicking.
        """
        return await self._delay(1.0, 4.0, "page_load")
    
    async def before_action(self) -> float:
        """Delay before performing an action (upgrade, send troops, etc).
        
        Player reads the page, checks costs, then clicks the button.
        """
        return await self._delay(2.0, 6.0, "before_action")
    
    async def between_steps(self) -> float:
        """Delay between steps in a multi-step flow.
        
        E.g., between step 1 (troop selection) and step 2 (confirmation).
        """
        return await self._delay(1.5, 4.0, "between_steps")
    
    async def quick_click(self) -> float:
        """Delay for a quick, routine click (e.g., confirming a dialog).
        
        Player knows what to click, just needs to move mouse and click.
        """
        return await self._delay(0.5, 2.0, "quick_click")
    
    async def think(self) -> float:
        """Delay for a decision-making pause.
        
        Player is deciding what to build, who to attack, etc.
        """
        return await self._delay(3.0, 10.0, "think")
    
    async def between_raids(self) -> float:
        """Delay between sending individual raids from a farm list.
        
        Player clicks "send" for each target, waits for UI update.
        """
        return await self._delay(0.8, 2.5, "between_raids")
    
    async def between_scouts(self) -> float:
        """Delay between sending scouts to different targets."""
        return await self._delay(1.5, 4.0, "between_scouts")
    
    async def session_start(self) -> float:
        """Delay at the start of a session (login → first action).
        
        Player logs in, looks at overview, checks messages, etc.
        """
        return await self._delay(3.0, 8.0, "session_start")
    
    async def wait(self, action: ActionType, context: str = "") -> float:
        """Wait with a delay appropriate for the given action type.
        
        Args:
            action: The type of action being performed
            context: Optional description for logging
            
        Returns:
            Actual seconds waited
        """
        min_s, max_s = _DELAY_RANGES.get(action, (1.0, 3.0))
        label = f"{action.value}" + (f" ({context})" if context else "")
        return await self._delay(min_s, max_s, label)
    
    async def custom(self, min_s: float, max_s: float, label: str = "custom") -> float:
        """Custom delay with specified range."""
        return await self._delay(min_s, max_s, label)
    
    async def _delay(self, min_s: float, max_s: float, label: str) -> float:
        """Internal: apply a random delay within range, scaled by multiplier."""
        if not self.enabled:
            return 0.0
        
        actual_min = min_s * self.multiplier
        actual_max = max_s * self.multiplier
        
        # Use a slightly skewed distribution — most delays cluster toward
        # the lower end (humans are usually quick) with occasional longer pauses
        delay = self._skewed_random(actual_min, actual_max)
        
        logger.debug(f"Human delay ({label}): {delay:.2f}s")
        await asyncio.sleep(delay)
        return delay
    
    @staticmethod
    def _skewed_random(min_val: float, max_val: float) -> float:
        """Generate a random value skewed toward the lower end.
        
        Uses a beta distribution (alpha=2, beta=5) mapped to [min, max].
        This gives ~70% of values in the lower third of the range.
        """
        # Beta(2, 5) has mean ~0.286, so most values are in lower range
        skew = random.betavariate(2, 5)
        return min_val + skew * (max_val - min_val)
