"""Activity scheduling to prevent 24/7 patterns.

Travian Multihunters flag accounts with no daily downtime from a single IP.
This scheduler tracks cumulative activity in a **rolling 24-hour window**
(not calendar-day) and enforces continuous-session breaks to make usage
patterns look natural.

Rolling window means: at any point in time, only the last 24h of activity
counts.  Old activity naturally expires — no midnight reset exploit.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import random
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class ActivityScheduler:
    """Enforces realistic play session boundaries.

    Tracks:
    - Rolling 24h activity via hourly buckets (no midnight reset)
    - Continuous session duration with auto-reset after idle

    Usage:
        scheduler = ActivityScheduler(max_daily_hours=16.0)

        while running:
            if not scheduler.can_continue():
                break_s = scheduler.next_break_duration()
                await asyncio.sleep(break_s)
                scheduler.start_session()

            # ... do work ...
            scheduler.log_activity(elapsed_seconds)
    """

    def __init__(
        self,
        max_daily_hours: float = 16.0,
        max_continuous_hours: float = 6.0,
        min_break_minutes: float = 10.0,
        enabled: bool = True,
        state_file: Path | None = None,
    ):
        self.max_daily_hours = max_daily_hours
        self.max_continuous_hours = max_continuous_hours
        self.min_break_minutes = min_break_minutes
        self.enabled = enabled

        # Hourly buckets: {"2026-04-22T14": 305.2, ...}
        # Each key is an hour slot, value is seconds of activity in that hour.
        self._hourly_buckets: dict[str, float] = {}

        # Continuous session tracking
        self._session_start: float = time.monotonic()
        self._session_seconds: float = 0.0
        self._last_activity_time: float = time.monotonic()
        self._last_activity_wall: float = time.time()

        self._state_file = state_file
        self._last_save_time: float = 0.0
        self._save_throttle_s: float = 30.0

        # Per-account circadian phase + night-break duration. Defaults match the
        # legacy fixed behavior; seed_circadian() binds them to the persona.
        # This was the ONLY stealth component not seeded with behavioral_identity
        # — so a multi-account fleet on one host shared an identical night-rest
        # window (hard 23:00-06:00) and wake-duration distribution, a
        # cross-account circadian-phase + wake-CDF collision a detector clusters
        # (per-account 24h periodicity alone is human-like; the synchronized
        # phase across accounts is the tell).
        self._night_start_hour = 23.0
        self._night_end_hour = 6.0
        self._night_break_band = (6.0, 9.0, 7.0)

        # Effective caps: jittered at-or-below the configured hard ceilings so
        # the actual stop point varies instead of landing on the exact same
        # round number every time. Without this, every session that hits the
        # limit is exactly ``max_continuous_hours`` long and every capped day
        # exactly ``max_daily_hours`` — a sharp spike in the session-length /
        # daily-total histogram that a detector flags. Both stay <= the
        # configured maximum, so we never work *longer* than the safety cap.
        # The continuous cap re-jitters per session (after each break); the
        # daily cap re-jitters once per local day (resampling it every short
        # session would let it drift upward as an order statistic). Persisted
        # so a same-day restart stays consistent. Sampled here so
        # ``_load_state()`` can override from disk when state exists.
        self._daily_cap_day = self._day_key()
        self._effective_continuous_hours = self._sample_continuous_cap()
        self._effective_daily_hours = self._sample_daily_cap()

        self._load_state()
        if self._state_file is not None:
            atexit.register(self._save_state_force)

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _hour_key(wall_time: float | None = None) -> str:
        """Return hour-granularity key like '2026-04-22T14'."""
        t = datetime.fromtimestamp(wall_time) if wall_time else datetime.now()
        return t.strftime("%Y-%m-%dT%H")

    def _prune_old_buckets(self) -> None:
        """Remove buckets older than 24 hours.

        Uses ``<=`` so the boundary hour is always pruned.  With hour
        granularity the effective window is 23h01m–24h00m, which slightly
        favors the user (never over-restricts).
        """
        cutoff = time.time() - 24 * 3600
        cutoff_key = self._hour_key(cutoff)
        old_keys = [k for k in self._hourly_buckets if k <= cutoff_key]
        for k in old_keys:
            del self._hourly_buckets[k]

    def _rolling_24h_seconds(self) -> float:
        """Sum activity seconds within the last 24 hours."""
        self._prune_old_buckets()
        return sum(self._hourly_buckets.values())

    @staticmethod
    def _day_key(wall_time: float | None = None) -> str:
        """Return day-granularity key like '2026-04-22' (local time)."""
        t = datetime.fromtimestamp(wall_time) if wall_time else datetime.now()
        return t.strftime("%Y-%m-%d")

    # Lower edge of each cap's jitter band, as a fraction of the hard ceiling.
    _CONT_CAP_LO_FRAC = 0.80
    _DAILY_CAP_LO_FRAC = 0.85

    def _continuous_cap_band(self) -> tuple[float, float]:
        return (self.max_continuous_hours * self._CONT_CAP_LO_FRAC, self.max_continuous_hours)

    def _daily_cap_band(self) -> tuple[float, float]:
        return (self.max_daily_hours * self._DAILY_CAP_LO_FRAC, self.max_daily_hours)

    def seed_circadian(self, identity: str) -> None:
        """Bind night-rest phase + wake duration to a stable persona identity.

        Each account gets its own night window (start in [22,24), end in [5,8))
        and its own wake-duration triangular band, so accounts on one host no
        longer share a synchronized night phase or an identical wake-time CDF.
        Stable across restarts (derived from the persona), distinct per account.
        """
        rng = random.Random(identity)
        self._night_start_hour = rng.uniform(22.0, 24.0)
        self._night_end_hour = rng.uniform(5.0, 8.0)
        lo = rng.uniform(5.5, 6.5)
        hi = rng.uniform(8.5, 9.5)
        mode = rng.uniform(lo + 0.5, hi - 0.5)
        self._night_break_band = (lo, hi, mode)

    def _sample_continuous_cap(self) -> float:
        """Effective continuous-session cap, jittered below the hard ceiling.

        Triangular (mode at the band midpoint) so density tapers to zero at
        both edges — a uniform draw leaves sharp support edges at 0.80x and
        1.0x that a fleet-wide KDE/density edge check can still see.
        """
        lo, hi = self._continuous_cap_band()
        return random.triangular(lo, hi, (lo + hi) / 2.0)

    def _sample_daily_cap(self) -> float:
        """Effective rolling-24h cap, jittered below the hard ceiling."""
        lo, hi = self._daily_cap_band()
        return random.triangular(lo, hi, (lo + hi) / 2.0)

    def _maybe_resample_daily_cap(self) -> None:
        """Re-jitter the daily cap once per local day, not per session.

        Resampling every short session would make the capped daily total the
        max of several draws (an order statistic), drifting it toward the hard
        ceiling and re-concentrating the upper edge.
        """
        today = self._day_key()
        if today != self._daily_cap_day:
            self._daily_cap_day = today
            self._effective_daily_hours = self._sample_daily_cap()

    def _coerce_cap(self, raw: object, band: tuple[float, float]) -> float | None:
        """Validate a persisted cap against the current sampler band.

        Returns None (caller keeps the freshly sampled default) when the value
        is non-finite or falls outside ``band`` — which happens when a corrupt
        state file holds nan/inf, or when the configured max changed between
        runs so a stale cap now sits above the hard ceiling or below the jitter
        band. Rejecting (vs clamping to the edge) avoids re-piling a stale value
        onto the exact boundary, which would reintroduce the histogram spike.
        Accepting nan/inf would be worse than cosmetic: ``value >= nan`` is
        always false, silently disabling the safety gate.
        """
        low, high = band
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or not (low <= value <= high):
            return None
        return value

    def _auto_reset_session_if_idle(self) -> None:
        """Auto-reset session counter if enough idle time has passed."""
        idle_seconds = time.monotonic() - self._last_activity_time
        if idle_seconds >= self.min_break_minutes * 60:
            if self._session_seconds > 0:
                logger.info(
                    "Session auto-reset: idle %.0fs >= break threshold %.0fs (was %.1fh session)",
                    idle_seconds,
                    self.min_break_minutes * 60,
                    self._session_seconds / 3600.0,
                )
                self._session_seconds = 0.0
                self._session_start = time.monotonic()
                # New logical session after an idle break: re-jitter the
                # continuous cap so it doesn't reuse the prior stop point.
                self._effective_continuous_hours = self._sample_continuous_cap()
                self._maybe_resample_daily_cap()

    # ── State persistence ────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load persisted budget state from disk."""
        if self._state_file is None or not self._state_file.exists():
            return
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))

            # Load hourly buckets (prune old ones)
            buckets = data.get("hourly_buckets", {})
            if isinstance(buckets, dict):
                self._hourly_buckets = {k: float(v) for k, v in buckets.items()}
                self._prune_old_buckets()

            # Migrate from old format: discard stale daily_seconds.
            # Old calendar-day data doesn't map to the rolling window;
            # starting fresh is fairer than concentrating it in one bucket.
            if not self._hourly_buckets and "daily_seconds" in data:
                old_daily = float(data.get("daily_seconds", 0))
                if old_daily > 0:
                    logger.info(
                        "Discarded old scheduler format: %.1fh (rolling window starts fresh)",
                        old_daily / 3600.0,
                    )

            # Session: reset if idle since last save
            last_saved = data.get("last_saved", 0)
            idle_since_save = time.time() - last_saved if last_saved else 0
            session_was_reset = idle_since_save >= self.min_break_minutes * 60
            if session_was_reset:
                self._session_seconds = 0.0
            else:
                self._session_seconds = float(data.get("session_seconds", 0.0))

            # Restore the jittered caps for cross-restart consistency. Invalid
            # or out-of-band values are rejected and the freshly sampled default
            # is kept. When the idle gap reset the session this is a NEW logical
            # session (same boundary as _auto_reset_session_if_idle), so keep
            # the fresh continuous cap rather than restoring the prior one.
            if not session_was_reset:
                cont = self._coerce_cap(
                    data.get("effective_continuous_hours"), self._continuous_cap_band()
                )
                if cont is not None:
                    self._effective_continuous_hours = cont
            daily = self._coerce_cap(data.get("effective_daily_hours"), self._daily_cap_band())
            if daily is not None:
                self._effective_daily_hours = daily
            saved_day = data.get("daily_cap_day")
            if isinstance(saved_day, str) and saved_day:
                self._daily_cap_day = saved_day
            # A process resumed across a day boundary must pick up a fresh daily
            # cap before any can_continue() gate uses the stale one.
            self._maybe_resample_daily_cap()

            rolling = self._rolling_24h_seconds()
            logger.info(
                "Restored scheduler: rolling_24h=%.1fh, session=%.1fh (idle_since_save=%.0fs)",
                rolling / 3600.0,
                self._session_seconds / 3600.0,
                idle_since_save,
            )
        except Exception as e:
            logger.warning("Failed to load scheduler state from %s: %s", self._state_file, e)

    def _save_state(self) -> None:
        """Persist budget state to disk (throttled to once per 30 s)."""
        if self._state_file is None:
            return
        now = time.monotonic()
        if now - self._last_save_time < self._save_throttle_s:
            return
        self._save_state_force()

    def _save_state_force(self) -> None:
        """Write state to disk immediately (atomic via tempfile + replace)."""
        if self._state_file is None:
            return
        self._prune_old_buckets()
        data = {
            "hourly_buckets": self._hourly_buckets,
            "session_seconds": self._session_seconds,
            "effective_continuous_hours": self._effective_continuous_hours,
            "effective_daily_hours": self._effective_daily_hours,
            "daily_cap_day": self._daily_cap_day,
            "last_saved": time.time(),
        }
        try:
            parent = self._state_file.parent
            parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=str(parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f)
                os.replace(tmp_path, str(self._state_file))
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
            self._last_save_time = time.monotonic()
        except Exception as e:
            logger.warning("Failed to save scheduler state: %s", e)

    # ── Public API ───────────────────────────────────────────────────

    def can_continue(self) -> bool:
        """Check if we're within rolling-24h and continuous limits.

        Returns:
            True if we can keep working, False if break needed.
        """
        if not self.enabled:
            return True

        # A session running across local midnight gets a fresh daily cap.
        self._maybe_resample_daily_cap()
        self._auto_reset_session_if_idle()

        # Check rolling 24h limit (against the jittered effective cap)
        rolling_hours = self._rolling_24h_seconds() / 3600.0
        if rolling_hours >= self._effective_daily_hours:
            logger.info(
                "Rolling 24h limit reached: %.1fh / %.1fh (cap %.1fh)",
                rolling_hours,
                self._effective_daily_hours,
                self.max_daily_hours,
            )
            return False

        # Check continuous session limit (against the jittered effective cap)
        session_hours = self._session_seconds / 3600.0
        if session_hours >= self._effective_continuous_hours:
            logger.info(
                "Continuous limit reached: %.1fh / %.1fh (cap %.1fh)",
                session_hours,
                self._effective_continuous_hours,
                self.max_continuous_hours,
            )
            return False

        return True

    def next_break_duration(self) -> float:
        """How long to break (in seconds).

        Short break (mid-session): min_break_minutes + random jitter
        Long break (rolling limit near): 1-3 hours
        Night break (if past 11pm local): 6-9 hours
        """
        if not self.enabled:
            return 0.0

        now = datetime.now()
        hour = now.hour + now.minute / 60.0  # fractional, for per-account phase
        rolling_hours = self._rolling_24h_seconds() / 3600.0

        # Triangular (not uniform) so the duration histogram tapers to zero at
        # the band edges instead of showing the flat support a KS test flags —
        # the same anti-uniform reasoning the continuous-session caps use
        # (see _sample_continuous_cap).

        # Night break: if it's late, take a long rest. The window boundaries and
        # the wake-duration band are per-account (seed_circadian), so accounts
        # on one host don't share a synchronized night phase / wake-time CDF.
        if hour >= self._night_start_hour or hour < self._night_end_hour:
            lo, hi, mode = self._night_break_band
            duration_h = random.triangular(lo, hi, mode)
            logger.info("Night break: sleeping %.1fh", duration_h)
            return duration_h * 3600.0

        # Rolling limit approaching (>85% used): longer break
        if rolling_hours >= self.max_daily_hours * 0.85:
            duration_h = random.triangular(1.0, 3.0, 1.8)
            logger.info("Long break (rolling limit near): %.1fh", duration_h)
            return duration_h * 3600.0

        # Standard mid-session break (mode skewed low — most breaks are short)
        base_minutes = self.min_break_minutes
        extra_minutes = random.triangular(0.0, 10.0, 3.0)
        duration_s = (base_minutes + extra_minutes) * 60.0
        logger.info("Short break: %.0f minutes", duration_s / 60)
        return duration_s

    def remaining_daily_budget(self) -> float:
        """Hours remaining in the rolling 24h window.

        Measured against the jittered effective cap that ``can_continue()``
        actually gates on, so telemetry/UI agrees with when the bot stops.
        """
        remaining = self._effective_daily_hours - (self._rolling_24h_seconds() / 3600.0)
        return max(0.0, remaining)

    def log_activity(self, seconds: float) -> None:
        """Record that we were active for N seconds.

        Splits activity across hour boundaries so each bucket only
        contains seconds that actually fell within that hour.
        """
        now = time.time()
        start = now - seconds

        # Walk from activity start to now, splitting at hour boundaries
        remaining = seconds
        cursor = start
        while remaining > 0:
            key = self._hour_key(cursor)
            # Seconds until the next hour boundary
            dt = datetime.fromtimestamp(cursor)
            next_hour = dt.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            secs_to_boundary = next_hour.timestamp() - cursor
            chunk = min(remaining, secs_to_boundary)
            self._hourly_buckets[key] = self._hourly_buckets.get(key, 0.0) + chunk
            remaining -= chunk
            cursor += chunk

        self._session_seconds += seconds
        self._last_activity_time = time.monotonic()
        self._last_activity_wall = now
        self._save_state()

    def start_session(self) -> None:
        """Start a new session (call after a break)."""
        self._session_start = time.monotonic()
        self._session_seconds = 0.0
        # Re-jitter the continuous cap so each post-break session ends at a
        # different point; the daily cap only re-jitters across a day boundary.
        self._effective_continuous_hours = self._sample_continuous_cap()
        self._maybe_resample_daily_cap()
        logger.debug(
            "New session started (caps: continuous=%.1fh, daily=%.1fh)",
            self._effective_continuous_hours,
            self._effective_daily_hours,
        )
        self._save_state()

    @property
    def daily_hours_used(self) -> float:
        """Hours active in the rolling 24h window."""
        return self._rolling_24h_seconds() / 3600.0

    @property
    def session_hours(self) -> float:
        """Hours in current continuous session."""
        return self._session_seconds / 3600.0
