"""Heavy-tailed timing engine for human-like delay generation.

Research shows humans follow power-law inter-event timing:
bursts of rapid actions separated by long pauses.
Uniform random (e.g., random.uniform(3, 7)) is trivially detectable
because it produces flat distributions that never occur in human data.

This module provides the HumanTiming class with static methods that
generate delays using exponential/ex-Gaussian distributions to match
real human behavioral patterns.

Complements the existing HumanDelay class (triangular distributions)
with more sophisticated heavy-tailed models for longer automation loops.
"""

import random


class HumanTiming:
    """Generates human-like delays using heavy-tailed distributions.

    Methods are pure functions with no state or side effects.
    All return values are in seconds.
    """

    @staticmethod
    def delay(mean: float, variance_factor: float = 0.4) -> float:
        """Heavy-tailed delay. Returns seconds to sleep.

        70% chance: short delay (exponential around mean*0.5)
        25% chance: medium delay (exponential around mean*1.5)
        5% chance:  long pause  (exponential around mean*5.0)

        This produces the "bursty" pattern that matches human behavior.

        Args:
            mean: Target mean delay in seconds
            variance_factor: Controls spread (0=tight, 1=very spread)

        Returns:
            Delay in seconds, bounded to [mean*0.1, mean*15.0]
        """
        roll = random.random()

        if roll < 0.70:
            # Short burst — most common
            lambd = 1.0 / (mean * 0.5 * max(0.1, variance_factor))
            raw = random.expovariate(lambd)
        elif roll < 0.95:
            # Medium delay
            lambd = 1.0 / (mean * 1.5 * max(0.1, variance_factor))
            raw = random.expovariate(lambd)
        else:
            # Long pause — rare but human-like
            lambd = 1.0 / (mean * 5.0 * max(0.1, variance_factor))
            raw = random.expovariate(lambd)

        # Clamp to sane bounds: never instant, never absurdly long
        lower = mean * 0.1
        upper = mean * 15.0
        return max(lower, min(raw, upper))

    @staticmethod
    def reaction_time(base_ms: float = 300) -> float:
        """Human reaction time in seconds (ex-Gaussian distribution).

        The ex-Gaussian is the standard model for human reaction times:
        a Gaussian component (motor response) convolved with an
        exponential component (cognitive processing).

        Returns between ~0.15s and ~2.0s typically.

        Args:
            base_ms: Base reaction time in milliseconds (default 300ms)

        Returns:
            Reaction time in seconds
        """
        # Gaussian component: motor response variability
        mu = base_ms / 1000.0  # convert to seconds
        sigma = mu * 0.15  # 15% CV for motor noise

        gaussian = random.gauss(mu, sigma)

        # Exponential component: cognitive processing tail
        tau = mu * 0.3  # exponential rate parameter
        exponential = random.expovariate(1.0 / tau) if tau > 0 else 0

        result = gaussian + exponential
        return max(0.15, min(result, 3.0))

    @staticmethod
    def micro_jitter(seconds: float, jitter_pct: float = 0.15) -> float:
        """Add +/-jitter_pct random variance to any fixed interval.

        Use for intervals that must stay roughly fixed (like ATG ticks)
        but shouldn't be EXACTLY fixed.

        Args:
            seconds: Base interval in seconds
            jitter_pct: Fraction of variance (0.15 = +/-15%)

        Returns:
            Jittered interval in seconds (always positive)
        """
        jitter = seconds * jitter_pct * random.uniform(-1.0, 1.0)
        return max(0.1, seconds + jitter)

    @staticmethod
    def typing_delay(text_length: int) -> float:
        """Simulates time to type N characters at ~60 WPM with variance.

        Models per-character typing with random speed variation.

        Args:
            text_length: Number of characters to "type"

        Returns:
            Typing duration in seconds
        """
        if text_length <= 0:
            return 0.0

        # ~60 WPM = ~5 chars/sec = ~200ms per char
        base_per_char = 0.2
        total = 0.0

        for _ in range(text_length):
            # Each keystroke varies: fast typist sometimes pauses
            char_time = random.gauss(base_per_char, base_per_char * 0.3)
            total += max(0.05, char_time)

        return total

    @staticmethod
    def page_load_time() -> float:
        """Simulates perceived page load: 0.5-3.0 seconds.

        Accounts for network latency + rendering + human processing time
        before the user starts reading/acting on the page.

        Returns:
            Simulated page load time in seconds
        """
        # Network + render: exponential with mean ~1s
        network = random.expovariate(1.0)
        # Human processing: small gaussian
        human = random.gauss(0.5, 0.15)
        result = network + human
        return max(0.5, min(result, 4.0))
