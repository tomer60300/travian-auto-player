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
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# The offset the night window runs on before any page has stated one, in
# minutes east of UTC. Europe 2 -- the world this account plays -- measures
# UTC+1 (issue #76; the live page states `Travian.Game.timezoneOffsetToUTC =
# -3600`), and the operator ruled the DEFAULT should say so rather than fall
# back to the host's clock: the host has been on a third offset more than once,
# and a night window 1-2h out of phase wakes the account inside the server's
# night, nightly -- the pattern the window exists to remove. A page-stated
# offset still overrides this (see `set_server_utc_offset_minutes`) and is
# persisted with the scheduler state, so a world on another offset corrects
# itself on its first marketplace read and no later process spends a request
# re-learning it.
DEFAULT_SERVER_UTC_OFFSET_MINUTES = 60

# Reject a stored offset outside the range real timezones occupy (UTC-12 to
# UTC+14), the same bounds the page parser enforces.
_MAX_UTC_OFFSET_MINUTES = 14 * 60


class ActivityScheduler:
    """Enforces realistic play session boundaries.

    Tracks:
    - Rolling 24h activity via hourly buckets (no midnight reset)
    - Continuous session duration with auto-reset after idle

    Usage:
        scheduler = ActivityScheduler(max_daily_hours=16.0)

        while running:
            # `while`, not `if`. `next_break_duration()` answers for the moment
            # it is asked, and waking is a new moment: the window may still be
            # open (a short draw, a deadline-trimmed sleep, an interruption), or
            # the rolling cap may still be over. An `if` falls straight through
            # to the work below without asking again, so the one case the break
            # exists to prevent -- working inside the window -- is the one it
            # lets through. Bounded: every duration is positive while the
            # scheduler refuses, and it stops refusing once the window closes.
            while not scheduler.can_continue():
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
        # The game's clock, presumed Europe 2 (UTC+1) until a page states
        # otherwise; a value learned in an earlier process is restored by
        # `_load_state`. See `set_server_utc_offset_minutes`.
        self._server_utc_offset_minutes: int | None = DEFAULT_SERVER_UTC_OFFSET_MINUTES

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

    def set_server_utc_offset_minutes(self, minutes: int | None) -> None:
        """Tell the scheduler what time it is where the GAME is.

        The night window decides when this account sleeps, which is the one
        behaviour meant to be indistinguishable from a person's. It ran on
        ``datetime.now()`` -- the HOST's clock -- and the host need not share a
        timezone with the server: this codebase's own #76 work established that
        Europe 2 runs UTC+1 while stamping its page epochs in UTC, and the
        operator's machine has been on a third offset more than once.

        A host three hours out puts the account's "night" three hours out, so it
        goes quiet while the server's day is busiest and works through the hours
        its neighbours are asleep. That is worse than having no night window:
        an account awake at 04:00 server time every single night is a pattern,
        not an absence of one.

        The default is :data:`DEFAULT_SERVER_UTC_OFFSET_MINUTES` -- this
        account's world, measured -- so the window sits at (or within DST of)
        the right hours from the first minute of a process, rather than only
        after a marketplace read has happened to run. A learned value is
        persisted with the scheduler state and outlives the process; passing
        None is the explicit opt-out back to host-local time.
        """
        if minutes != self._server_utc_offset_minutes:
            self._server_utc_offset_minutes = minutes
            # Learned once, kept for every later process: this is what makes
            # the offset cost zero requests after the first page ever to state
            # it. Throttled like every other save; atexit force-writes anyway.
            self._save_state()

    def _server_now(self) -> datetime:
        """Now on the game's clock -- learned, else presumed UTC+1 -- and the
        host's only after an explicit ``set_server_utc_offset_minutes(None)``."""
        if self._server_utc_offset_minutes is None:
            return datetime.now()
        return datetime.now(UTC).replace(tzinfo=None) + timedelta(
            minutes=self._server_utc_offset_minutes
        )

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
        # Three further draws bounded `_night_break_band`, which nothing has
        # read since `next_break_duration` started delegating to
        # `seconds_until_rest_ends`. They were the last statements in this
        # method, so nothing downstream depended on where they left the RNG and
        # deleting them shifts no account's phase.

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

    @property
    def seconds_idle(self) -> float:
        """How long since this account last did anything.

        The same quantity :meth:`_auto_reset_session_if_idle` compares against
        ``min_break_minutes`` to decide a new logical session has begun, exposed
        so a caller can ask the question WITHOUT the side effect of answering it
        -- reading it must not reset the counter it is reading.

        A caller that has been away this long is arriving, not continuing, and
        should arrive the way a person does: on a landing page, not on the form
        it came for.
        """
        return time.monotonic() - self._last_activity_time

    @property
    def is_new_session(self) -> bool:
        """True when the idle gap is long enough to count as arriving afresh.

        Shares `min_break_minutes` with the auto-reset above rather than picking
        its own threshold, so "the scheduler thinks this is a new session" and
        "the navigator thinks this is an arrival" can never disagree.
        """
        if not self.enabled:
            return False
        return self.seconds_idle >= self.min_break_minutes * 60

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

            # The game clock's offset, learned from a page by some earlier
            # process. Restoring it is what makes the night window sit right
            # without this process ever reading a marketplace. Same posture as
            # `_coerce_cap`: a value that is not a plausible timezone keeps the
            # fresh default rather than moving the account's night to nonsense.
            offset = data.get("server_utc_offset_minutes")
            if (
                isinstance(offset, int)
                and not isinstance(offset, bool)
                and abs(offset) <= _MAX_UTC_OFFSET_MINUTES
            ):
                self._server_utc_offset_minutes = offset

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
            "server_utc_offset_minutes": self._server_utc_offset_minutes,
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

    def quota_allows(self) -> bool:
        """The CAPS only: rolling-24h and continuous session. Not the clock.

        Split out from :meth:`can_continue` because the two questions have
        different answers to one particular caller. "Have I worked too long"
        is a quota and nobody may override it. "Is it 4am" is a schedule, and
        an operator sitting at the keyboard at 4am is the one person entitled
        to say it does not apply to them -- which the execute endpoint's
        ``i_am_awake`` offers in so many words.

        With one predicate the override could not work: the endpoint waved the
        night check through at the door and then hit it again, wearing a
        quota's name, at the budget check two lines later. See
        :meth:`can_continue` for who should still ask the combined question.
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

    def can_continue(self) -> bool:
        """Quota AND schedule: what a loop running unattended should ask.

        Every loop in the app goes through here, which is why the night window
        is checked here. Before that, `next_break_duration` had a correct night
        branch that slept 6-9h and nothing could reach it: the trigger never
        fired at night, so the only way in was for the daily budget to run out
        during the window by coincidence. The machinery was written, tested and
        unreachable, and every loop ran straight through to morning.

        Which is the signal the rest of the stealth layer cannot cover for.
        Impersonated TLS, log-normal delays, a drifting session tempo and
        truthful Referers all describe how a single request looks; none of them
        says anything about an account that has never once been idle at 4am.

        The one caller that should ask :meth:`quota_allows` instead is a run
        whose operator has stated they are present -- and only that run, because
        this is per-account state shared with every concurrent loop. Choosing
        the narrower question at the call site keeps the override scoped to the
        operation that was granted it, rather than switching something off
        account-wide for as long as the run lasts.
        """
        if not self.quota_allows():
            return False

        # The account's night. This is a SCHEDULE, not a quota -- the caps ask
        # "have I worked too long", this asks "is it 4am".
        if self.is_rest_window():
            logger.info(
                "Night-rest window (%.1f -> %.1f): pausing until it closes",
                self._night_start_hour,
                self._night_end_hour,
            )
            return False

        return True

    def is_rest_window(self, now: datetime | None = None) -> bool:
        """True during this account's night-rest window (wrap-aware).

        The window can wrap past midnight (start ~23:00, end ~06:00). A human
        account is quiet then, so a long-running loop should pause and resume in
        the morning rather than run straight through — activity that respects no
        sleep window is the most reliable machine-vs-human signal there is. The
        boundaries are per-account (seed_circadian), so a fleet on one host does
        not share a synchronized night phase.
        """
        if not self.enabled:
            return False
        now = now or self._server_now()
        # Second precision, matching seconds_until_rest_ends, so the two share
        # one boundary and can't disagree about the window edge.
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        start = self._night_start_hour % 24.0  # seed can draw up to 24.0
        end = self._night_end_hour
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def seconds_until_rest_ends(self, now: datetime | None = None) -> float:
        """Seconds to sleep so the loop wakes just after the rest window.

        Returns 0 outside the window. Inside it, ONE pause spans the whole
        remaining window (time from now to ``night_end_hour``, wrap-aware) plus
        a small random buffer. That is deliberately NOT ``next_break_duration``'s
        fixed 6–9h draw, which can oversleep hours past morning (enter at 05:30,
        draw 8h) or undersleep and wake back inside the window (enter at 23:00,
        draw 6h) — the latter would let the loop fire a full activity burst
        mid-window on wake. Aligning to the window end guarantees a single quiet
        stretch and a wake time that is near morning but jittered, not a sharp
        daily constant.
        """
        # Resolve the clock ONCE and share it with the window check. Sampling
        # now separately for is_rest_window and the remaining-time math let the
        # two straddle the window-end boundary: is_rest_window(now1) True, then
        # hour(now2) just past the end, so (end - hour) % 24 wraps to ~24h and
        # the account slept for a day. One instant closes that.
        now = now or self._server_now()
        if not self.is_rest_window(now):
            return 0.0
        hour = now.hour + now.minute / 60.0 + now.second / 3600.0
        remaining_h = (self._night_end_hour - hour) % 24.0
        if remaining_h <= 0.0:
            # Defensive net for the exact-boundary instant: resume, never a
            # full-day sleep.
            return 0.0
        # Up to ~45 min so a wake is not a constant -- log-normal rather than
        # uniform, for the reason `stealth/timing.py` opens with. A flat band
        # here means every account in a fleet wakes with the same shaped delay,
        # and a wake time is one of the few events an observer gets cleanly.
        buffer_s = min(random.lognormvariate(math.log(600.0), 0.9), 2700.0)
        return remaining_h * 3600.0 + buffer_s

    def next_break_duration(self, now: datetime | None = None) -> float:
        """How long to break (in seconds).

        Night break: to the end of the rest window (see seconds_until_rest_ends)
        Long break (rolling limit near): 1-3 hours
        Short break (mid-session): min_break_minutes + random jitter

        *now* is injectable for the same reason :meth:`is_rest_window` and
        :meth:`seconds_until_rest_ends` take it: all three have to agree about
        where the window is, and a method that reads the clock itself can only
        be tested by monkeypatching time -- which is how two of them would come
        to disagree about the edge without anything failing.
        """
        if not self.enabled:
            return 0.0

        # The GAME's clock, like `is_rest_window` and `seconds_until_rest_ends`
        # -- and this method resolves `now` for both of them, so getting it from
        # `datetime.now()` here overrode their own correct defaults rather than
        # merely differing from them. With the host three hours ahead of the
        # server, `can_continue` read 04:00 and refused to work while this read
        # 07:00, found no rest window, and handed back a ten-minute daytime
        # break with two hours of night still to run. The caller slept it and
        # carried on. The docstring above already required all three to agree
        # about where the window is; this is the line that did not.
        now = now or self._server_now()
        rolling_hours = self._rolling_24h_seconds() / 3600.0

        # Triangular (not uniform) so the duration histogram tapers to zero at
        # the band edges instead of showing the flat support a KS test flags —
        # the same anti-uniform reasoning the continuous-session caps use
        # (see _sample_continuous_cap).

        # Night break: if it's late, take a long rest. Reuse is_rest_window as
        # the single source of truth for "is it night" (it normalizes a
        # start hour of 24.0) so this duration path can never disagree with the
        # detection path about where the window is.
        if self.is_rest_window(now):
            # Delegated, not re-derived. `seconds_until_rest_ends` already sleeps
            # to the END of the window plus a one-sided buffer, and already
            # carries the reasoning for it: a 6-9h draw measured from wherever
            # the loop happens to be standing could land short -- waking it back
            # INSIDE its own night to fire a burst, which is worse than not
            # sleeping, because now there is activity at 4am AND a gap that looks
            # deliberate -- or land hours past morning and waste the day.
            #
            # This branch used to make that 6-9h draw itself. Adding a second
            # jitter here would not have been belt and braces, it would have been
            # two independent samples of the same decision, which is how a pause
            # comes to disagree with the window it was computed from.
            duration_s = self.seconds_until_rest_ends(now)
            logger.info("Night break: sleeping %.1fh to the end of the window", duration_s / 3600.0)
            return duration_s

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
