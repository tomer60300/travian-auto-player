"""Behavioral noise injection for automation loops.

Research shows bots are detected by ABSENCE of normal browsing.
Real players check rankings, read messages, view profiles randomly.
This module adds 10-20% noise actions to any automation loop,
making the bot's traffic pattern indistinguishable from a human
who occasionally gets distracted.
"""

import asyncio
import logging
import random
from typing import Optional, TYPE_CHECKING

from .human_delay import HumanDelay, ActionType
from .timing import HumanTiming

if TYPE_CHECKING:
    from .navigator import PageNavigator

logger = logging.getLogger(__name__)


class NoiseInjector:
    """Injects non-functional browsing actions between bot operations.

    Usage:
        noise = NoiseInjector(navigator, human_delay)

        # In automation loop:
        for item in work_items:
            await do_work(item)
            await noise.maybe_inject_noise()
    """

    def __init__(
        self,
        navigator: "PageNavigator",
        human_delay: HumanDelay,
        noise_rate: float = 0.15,
        enabled: bool = True,
    ):
        """
        Args:
            navigator: PageNavigator for loading noise pages
            human_delay: HumanDelay for timing between noise actions
            noise_rate: Probability of noise per action cycle (0.0-1.0)
            enabled: Master switch for noise injection
        """
        self._navigator = navigator
        self._delay = human_delay
        self._noise_rate = max(0.0, min(1.0, noise_rate))
        self.enabled = enabled
        self._actions_since_noise = 0
        self._actions_since_break = 0

    async def maybe_inject_noise(self, village_id: Optional[int] = None) -> bool:
        """Called between automation actions. Returns True if noise was injected.

        With probability self._noise_rate:
          1. Pick a random noise action from navigator.idle_browse()
          2. Execute it
          3. Return True
        Otherwise return False (no noise this cycle).

        Args:
            village_id: Current village ID for context-aware browsing
        """
        if not self.enabled:
            return False

        self._actions_since_noise += 1
        self._actions_since_break += 1

        if random.random() < self._noise_rate:
            logger.debug(
                f"Noise injection triggered (after {self._actions_since_noise} actions)"
            )
            self._actions_since_noise = 0

            try:
                await self._navigator.idle_browse(village_id=village_id)
            except Exception as e:
                logger.debug(f"Noise action failed (non-critical): {e}")

            return True

        return False

    async def session_break(self, duration_minutes: Optional[float] = None) -> float:
        """Simulate a break (player went AFK, got coffee, etc.)

        During break: no requests at all.
        After break: do a mini warm-up (load dorf1.php).

        Args:
            duration_minutes: Break duration. If None, random 2-10 minutes.

        Returns:
            Actual break duration in seconds.
        """
        if not self.enabled:
            return 0.0

        if duration_minutes is None:
            duration_minutes = random.uniform(2.0, 10.0)

        duration_s = duration_minutes * 60.0
        logger.info(f"Taking a break for {duration_minutes:.1f} minutes...")
        await asyncio.sleep(duration_s)

        # Mini warm-up after break: load dorf1 to "resume" session
        try:
            await self._navigator._visit("/dorf1.php", "resuming after break")
        except Exception as e:
            logger.debug(f"Post-break warm-up failed: {e}")

        self._actions_since_break = 0
        logger.info("Break over, resuming.")
        return duration_s

    def should_take_break(self, actions_threshold: int = 30) -> bool:
        """Check if it's time for a short break based on action count.

        Args:
            actions_threshold: Number of actions before suggesting a break

        Returns:
            True if a break is recommended
        """
        if not self.enabled:
            return False
        return self._actions_since_break >= actions_threshold

    @property
    def actions_since_break(self) -> int:
        """Number of actions performed since last break."""
        return self._actions_since_break
