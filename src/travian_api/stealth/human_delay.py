"""Human-like delay simulation.

Real humans don't click instantly. They read pages, move their mouse,
hesitate, get distracted. This module adds realistic random delays
that mimic human interaction timing.

Delay profiles:
- page_read: 1-4s (scanning a page before acting)
- click: 0.3-1.2s (time to find and click a button)
- form_fill: 0.5-2.0s (filling in a form field)
- between_actions: 2-8s (gap between major actions like upgrades)
- between_raids: 1-5s (gap between sending raids in a farm list)
- thinking: 5-20s (longer pause, simulating decision-making)
- idle_browse: 30-120s (checking the game casually)
"""

import asyncio
import logging
import random
from enum import Enum
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class DelayProfile(Enum):
    """Named delay profiles with (min, max) ranges in seconds."""
    PAGE_READ = (1.0, 4.0)
    CLICK = (0.3, 1.2)
    FORM_FILL = (0.5, 2.0)
    BETWEEN_ACTIONS = (2.0, 8.0)
    BETWEEN_RAIDS = (1.0, 5.0)
    BETWEEN_SCOUTS = (1.5, 4.5)
    THINKING = (5.0, 20.0)
    IDLE_BROWSE = (30.0, 120.0)
    VIDEO_TICK_JITTER = (-0.5, 0.5)  # jitter added to video tick timing
    
    # Navigation chain delays (simulating browsing between pages)
    NAV_STEP = (0.8, 2.5)  # between pages in a navigation chain


class HumanDelay:
    """Generates human-like delays for various interaction types.
    
    Supports a global speed multiplier to make everything faster/slower.
    multiplier=0.5 → half the delays (faster but riskier)
    multiplier=2.0 → double the delays (slower but safer)
    
    Also occasionally adds "distraction" delays — longer pauses that
    simulate a human alt-tabbing, reading chat, etc.
    """
    
    def __init__(
        self,
        multiplier: float = 1.0,
        distraction_chance: float = 0.05,
        distraction_range: Tuple[float, float] = (10.0, 45.0),
        enabled: bool = True,
    ):
        """
        Args:
            multiplier: Speed multiplier for all delays
            distraction_chance: Probability of a random long pause (0.0-1.0)
            distraction_range: (min, max) seconds for distraction pauses
            enabled: If False, all delays are skipped
        """
        self.multiplier = max(0.1, multiplier)
        self.distraction_chance = distraction_chance
        self.distraction_range = distraction_range
        self.enabled = enabled
    
    async def wait(self, profile: DelayProfile, context: str = "") -> float:
        """Wait for a human-like duration based on the profile.
        
        Args:
            profile: Which delay profile to use
            context: Optional description for logging
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        min_s, max_s = profile.value
        base_delay = random.uniform(min_s, max_s) * self.multiplier
        
        # Occasional distraction (random long pause)
        distraction = 0.0
        if random.random() < self.distraction_chance:
            distraction = random.uniform(*self.distraction_range) * self.multiplier
            logger.debug(f"Simulating distraction: +{distraction:.1f}s")
        
        total = base_delay + distraction
        
        if total > 1.0 and context:
            logger.debug(f"Human delay ({profile.name}): {total:.1f}s — {context}")
        
        await asyncio.sleep(total)
        return total
    
    async def wait_range(self, min_s: float, max_s: float, context: str = "") -> float:
        """Wait for a custom random duration (respects multiplier).
        
        Args:
            min_s: Minimum seconds
            max_s: Maximum seconds
            context: Optional description for logging
            
        Returns:
            Actual seconds waited
        """
        if not self.enabled:
            return 0.0
        
        delay = random.uniform(min_s, max_s) * self.multiplier
        
        if delay > 1.0 and context:
            logger.debug(f"Human delay (custom): {delay:.1f}s — {context}")
        
        await asyncio.sleep(delay)
        return delay
    
    def jitter(self, base_ms: int, profile: DelayProfile = DelayProfile.VIDEO_TICK_JITTER) -> int:
        """Add jitter to a millisecond value.
        
        Args:
            base_ms: Base value in milliseconds
            profile: Delay profile for jitter range
            
        Returns:
            base_ms with random jitter added
        """
        if not self.enabled:
            return base_ms
        
        min_j, max_j = profile.value
        jitter_ms = int(random.uniform(min_j, max_j) * 1000)
        return max(100, base_ms + jitter_ms)  # never go below 100ms
