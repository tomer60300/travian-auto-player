"""Human-like random delays between actions.

Models realistic human interaction timing:
- Reading a page: 2-8 seconds
- Clicking a button: 0.5-2 seconds
- Thinking/deciding: 3-12 seconds
- Between rapid actions: 1-3 seconds
- After login: 2-5 seconds

All delays use a right-skewed (shifted log-normal) distribution: most delays
are on the shorter end with a soft floor and an occasional longer tail,
matching real human action-time data. A bounded triangular was used before,
but its hard min/max cutoffs and linear ramps are rejectable by a KS /
Anderson-Darling test against human timing.
"""

import asyncio
import logging
import math
import random
from enum import Enum

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions with different timing profiles."""

    PAGE_LOAD = "page_load"  # Loading a new page (reading it)
    CLICK = "click"  # Clicking a button/link
    FORM_FILL = "form_fill"  # Filling form fields
    DECISION = "decision"  # Thinking before an action
    RAPID = "rapid"  # Quick sequential actions
    POST_LOGIN = "post_login"  # After logging in
    BETWEEN_RAIDS = "between_raids"  # Between sending farm list raids
    BETWEEN_SCOUTS = "between_scouts"  # Between sending scouts
    BETWEEN_ROUTES = "between_routes"  # Between creating/disabling trade routes
    PRE_UPGRADE = "pre_upgrade"  # Before starting a building upgrade
    VIDEO_TICK = "video_tick"  # Between video reward ticks


# Timing profiles: (min_s, mode_s, max_s).
# mode_s is the most likely value; min_s is a soft floor; max_s is the ~95th
# percentile (NOT a hard cap — the distribution has a longer, soft-capped tail).
_TIMING_PROFILES = {
    ActionType.PAGE_LOAD: (0.8, 1.5, 4.0),
    ActionType.CLICK: (0.2, 0.5, 1.5),
    ActionType.FORM_FILL: (0.5, 1.2, 3.0),
    ActionType.DECISION: (1.0, 2.5, 7.0),
    ActionType.RAPID: (0.4, 0.8, 2.0),
    ActionType.POST_LOGIN: (1.5, 2.5, 4.0),
    ActionType.BETWEEN_RAIDS: (0.8, 1.5, 3.5),
    ActionType.BETWEEN_SCOUTS: (1.0, 2.0, 4.0),
    # Filling the trade-route dialog (destination, resources, interval) is a
    # slower, more deliberate action than a raid click — wider, longer profile.
    ActionType.BETWEEN_ROUTES: (3.0, 7.0, 20.0),
    ActionType.PRE_UPGRADE: (1.0, 2.0, 5.0),
    ActionType.VIDEO_TICK: (2.5, 3.0, 3.8),  # Tight range — ATG expects ~3s
}


class HumanDelay:
    """Generates human-like delays for different action types.

    Uses a shifted log-normal per action class for natural, KS/AD-robust
    timing variation. Occasionally adds "micro-pauses" (someone got
    distracted) and periodic longer "think pauses".

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
        # Per-account multiplier on the distribution spread (sigma). Default 1.0;
        # bound to the persona via seed_delays() so two accounts don't emit an
        # identical per-action-class gap distribution a cross-account
        # likelihood-ratio test could cluster. The mode is unaffected (see
        # _action_delay), so the tuned central tendency is preserved per account.
        self._delay_sigma_mult = 1.0
        self._tempo = None  # optional shared SessionTempo, set via set_tempo()

    def set_tempo(self, tempo) -> None:
        """Attach a shared SessionTempo so action delays drift with the gap."""
        self._tempo = tempo

    def seed_delays(self, identity: str) -> None:
        """Bind the per-account delay-spread multiplier to a stable identity.

        Kept to a narrow band: the multiplier scales sigma, which shifts where
        the old ``max_s`` falls on the curve (it is the ~95th percentile only
        at mult 1.0). A wide band would make the over-``max_s`` tail mass vary
        too much across accounts — a per-action-class likelihood-ratio signal.
        ``[0.92, 1.12]`` keeps that tail mass within roughly [3%, 9%] while
        still giving each account a distinct spread.
        """
        self._delay_sigma_mult = random.Random(identity).uniform(0.92, 1.12)

    def _action_delay(self, min_s: float, mode_s: float, max_s: float) -> float:
        """Sample an action delay from a shifted log-normal.

        The shape is auto-derived from the existing (min, mode, max) profile —
        no manual re-tuning of the tuned central tendency:

        - floored at ``min_s`` (soft physiological reaction floor, no spike);
        - peak (mode) exactly at ``mode_s`` (lognormal mode = exp(mu - sigma^2),
          so mu = ln(mode-min) + sigma^2 pins it regardless of sigma);
        - sigma chosen so the ~95th percentile lands at the old ``max_s``, i.e.
          ~95% of draws stay inside the old envelope, the rest form a soft
          right tail (no hard max cutoff — that cutoff is the triangular tell);
        - tail soft-capped at 4x the span so a single draw can't stall a loop.

        Falls back to triangular for degenerate profiles (mode<=min or
        max<=mode).
        """
        peak_inc = mode_s - min_s
        span_inc = max_s - min_s
        if peak_inc <= 0 or span_inc <= peak_inc:
            return random.triangular(min_s, max_s, mode_s)
        # Solve sigma^2 + 1.645*sigma - ln(span/peak) = 0 so the 95th percentile
        # of the increment equals span_inc.
        r = math.log(span_inc / peak_inc)
        sigma = (-1.645 + math.sqrt(1.645 * 1.645 + 4.0 * r)) / 2.0
        sigma *= self._delay_sigma_mult
        mu = math.log(peak_inc) + sigma * sigma  # pins the mode at mode_s
        delay = min_s + random.lognormvariate(mu, sigma)
        return min(delay, min_s + span_inc * 4.0)

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

        # Get base timing. VIDEO_TICK keeps its tight triangular range — the
        # ~3s tick cadence is a functional ATG-signature requirement, not a
        # stealth knob, so it must not grow a tail.
        min_s, mode_s, max_s = _TIMING_PROFILES[action]
        if action == ActionType.VIDEO_TICK:
            base = random.triangular(min_s, max_s, mode_s)
            tempo_mult = 1.0  # functional ~3s cadence must not drift
        else:
            base = self._action_delay(min_s, mode_s, max_s)
            tempo_mult = self._tempo.current() if self._tempo is not None else 1.0
        delay = base * self.speed_factor * tempo_mult

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
