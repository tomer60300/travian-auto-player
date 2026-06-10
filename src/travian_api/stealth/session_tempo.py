"""Shared, slowly-drifting session tempo.

``HumanDelay`` and ``RequestThrottler`` otherwise emit ~iid samples within an
action class / per request. A real player's pace drifts over a session
(warming up, tiring, getting distracted), so consecutive action delays and
request gaps are positively autocorrelated. A detector separates iid synthetic
timing from a human session tempo with a Ljung-Box / runs / lag-1
autocorrelation test (or an HMM likelihood-ratio).

``SessionTempo`` is a bounded AR(1) random walk in log-space, shared by both
timing engines: each multiplies its sampled delay by the *same* current tempo,
so consecutive delays and gaps share a slowly-drifting factor — positive
short-lag correlation, without flattening either marginal (the walk
mean-reverts to a multiplier of 1.0, so over many samples the average scaling
is ~1).

Two facets are persona-stable (seeded from the behavioral identity): the
persistence ``phi`` and the volatility ``noise_sigma`` — so each account has a
distinct "tempo personality" (some steady, some erratic). The walk realization
itself uses the global RNG, so it varies per session.
"""

from __future__ import annotations

import math
import random
import time


class SessionTempo:
    """Bounded AR(1) log-space tempo multiplier shared across timing engines."""

    def __init__(
        self,
        identity: str | None = None,
        *,
        low: float = 0.7,
        high: float = 1.5,
        step_interval_s: float = 30.0,
    ):
        """
        Args:
            identity: persona-stable seed for phi/noise. None -> global RNG.
            low/high: hard bounds on the tempo multiplier.
            step_interval_s: minimum wall-clock between AR(1) steps. Gating by
                wall-clock means a HumanDelay.wait() and a throttler.wait()
                within the same logical action (a few seconds apart) read the
                same tempo instead of double-stepping the walk.
        """
        rng = random.Random(identity) if identity is not None else random
        # Persona-stable persistence + volatility. High phi => slow drift.
        self._phi = rng.uniform(0.90, 0.985)
        self._noise_sigma = rng.uniform(0.05, 0.15)
        # Map an UNBOUNDED latent AR(1) to the [low, high] multiplier with a
        # smooth tanh squash. A hard clamp would pile probability mass at the
        # bounds (the AR(1)'s stationary spread exceeds this narrow window), a
        # sticky-regime artifact an HMM / KS test on normalized gaps can detect.
        # tanh has no boundary mass — density tapers smoothly and the
        # multiplier is strictly interior.
        # Asymmetric reach so the multiplier is centered at exactly 1.0 (z=0):
        # positive latent stretches toward `high`, negative toward `low`, each
        # side scaled by its own log-distance from 1.0.
        self._reach_up = math.log(high)  # > 0
        self._reach_down = -math.log(low)  # > 0 (low < 1)
        self._scale = 1.5  # latent-to-tanh scale: typical drift ~±20%, no clamp
        self._step_interval_s = step_interval_s
        self._z = 0.0  # latent state; multiplier is exactly 1.0 at z == 0
        self._last_step: float | None = None

    def _advance(self) -> None:
        """One AR(1) step of the unbounded latent, mean-reverting to 0."""
        self._z = self._phi * self._z + random.gauss(0.0, self._noise_sigma)

    def _multiplier(self) -> float:
        th = math.tanh(self._z / self._scale)
        reach = self._reach_up if th >= 0 else self._reach_down
        return math.exp(th * reach)

    def current(self, now: float | None = None) -> float:
        """Return the current tempo multiplier, advancing the walk if due.

        The walk advances at most once per ``step_interval_s`` of wall-clock
        (catching up bounded steps if a long gap elapsed), so repeated reads
        within one action return the same value.

        Not thread-safe: assumes single-event-loop ownership (the method has no
        ``await``, so asyncio coroutines never interleave mid-update). Don't
        share one instance across OS threads / multiple event loops.
        """
        now = time.monotonic() if now is None else now
        if self._last_step is None:
            self._last_step = now
        else:
            elapsed = now - self._last_step
            if elapsed >= self._step_interval_s:
                # Catch up, but cap so a long idle gap can't walk unboundedly.
                steps = min(int(elapsed / self._step_interval_s), 10)
                for _ in range(steps):
                    self._advance()
                self._last_step = now
        return self._multiplier()
