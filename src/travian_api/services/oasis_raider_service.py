"""Oasis Raider service — scan, filter, enrich, and raid unoccupied oases.

Includes six behavioral anti-detection mitigations that make the sweep
pattern indistinguishable from a human browsing the map and raiding oases
manually.  These operate ABOVE the request-level stealth layer (throttler,
human_delay, navigator) — they control the *sequence* and *timing* of game
actions, not the individual HTTP requests.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List

from ..constants import TROOP_MAPPINGS, TribeType

logger = logging.getLogger(__name__)

# ── Animal unit IDs → display names ──────────────────────────────────
ANIMAL_NAMES: Dict[str, str] = {
    "u31": "Rat",
    "u32": "Spider",
    "u33": "Serpent",
    "u34": "Bat",
    "u35": "Wild Boar",
    "u36": "Wolf",
    "u37": "Bear",
    "u38": "Crocodile",
    "u39": "Tiger",
    "u40": "Elephant",
}

BONUS_KEYWORDS: Dict[str, List[str]] = {
    "wood": ["wood", "lumber"],
    "clay": ["clay"],
    "iron": ["iron"],
    "crop": ["crop", "grain", "cereal"],
}

# ── Anti-detection behavioral constants (tunable) ────────────────────
# Mitigation 1 — Weighted shuffle
SHUFFLE_WEIGHT_FACTOR = 1.5  # Higher = stronger distance bias in shuffle

# Mitigation 2 — Map browsing between enrichments
BROWSE_FREQ_MIN = 2  # Browse every N enrichments (randomized)
BROWSE_FREQ_MAX = 4

# Mitigation 3 — Decision delay before raids
THINK_DELAY_MIN = 2.0  # Seconds: quick glance → immediate raid
THINK_DELAY_MODE = 5.0  # Seconds: read page, open rally point
THINK_DELAY_MAX = 12.0  # Seconds: careful read, check troops, hesitate

# Mitigation 5 — Random false skips
SKIP_PROBABILITY = 0.10  # 10% chance to skip a valid empty target
SKIP_MIN_REMAINING = 4  # Don't skip if fewer than this many targets left

# Mitigation 6 — Burst-and-break pacing (burst size via _sample_burst_size)
BREAK_DURATION_MIN = 30.0  # Micro-break length in seconds
BREAK_DURATION_MAX = 90.0

# Shared — Noisy sleep segments (Mitigations 4 & 6)
NOISY_SLEEP_SEGMENT_MIN = 15.0  # Seconds per sleep chunk
NOISY_SLEEP_SEGMENT_MAX = 25.0

# Pages a human might visit while idle / waiting for troops
_NOISE_PAGES = [
    "/dorf1.php",
    "/dorf2.php",
    "/report/all",
    "/statistiken.php",
    "/spieler.php",
]


def _sample_burst_size() -> int:
    """Raids before a micro-break — right-skewed, not uniform over {3,4,5}.

    A uniform draw over a 3-value set is a flat discrete histogram a chi-square
    can reject. Real focus-bursts cluster small with an occasional longer run:
    a quick 2-raid burst sometimes, mostly 3-4, occasionally a 5-7 streak.
    """
    r = random.random()
    if r < 0.25:
        return 2
    if r < 0.80:
        return random.randint(3, 4)
    return random.randint(5, 7)


@dataclass
class OasisRaiderConfig:
    """Configuration for a single oasis raider sweep."""

    radius: int = 15
    troops: Dict[str, int] = field(default_factory=dict)
    max_targets: int = 0
    bonus_filter: List[str] = field(default_factory=list)
    sleep_interval: int = 60
    dry_run: bool = False
    village_id: int | None = None
    # Recurring-run support: if > 0, the sweep re-runs every N seconds
    # after completion until the user stops. 0 = single-shot (default).
    repeat_interval_seconds: int = 0


class OasisRaiderService:
    """Orchestrates scan → filter → enrich → raid for unoccupied oases."""

    def __init__(self, session) -> None:
        self._session = session
        self._http = session.http_client
        self._military = session.military_service
        self._scout_svc = session.scout_service

    # ── Main pipeline ────────────────────────────────────────────────

    async def run_sweep(
        self,
        config: OasisRaiderConfig,
        send_log: Callable,
        check_stop: Callable,
    ) -> dict:
        t_start = time.monotonic()
        stats: dict = {
            "total": 0,
            "sent": 0,
            "skipped_animals": [],
            "skipped_troops": 0,
            "skipped_random": 0,
            "sleep_cycles": 0,
            "sleep_time": 0.0,
            "browse_pauses": 0,
            "breaks_taken": 0,
            "break_time": 0.0,
            "think_delays": [],
            "order_entropy": 0.0,
            "duration": 0.0,
        }

        village_id = config.village_id or self._session.active_village_id
        village = next(
            (v for v in self._session.auth_state.villages if v.id == village_id),
            None,
        )
        if not village:
            await send_log("TROOPS", "❌", "No active village found", "error")
            return stats
        cx, cy = village.x, village.y
        tribe_id = self._session.tribe_id or 2

        # ── STEP 1: Fetch idle troops ────────────────────────────────
        await send_log("TROOPS", "🏠", "Fetching idle troops from village...", "info")
        available = await self._fetch_idle_troops(village_id)
        await send_log(
            "TROOPS",
            "🏠",
            f"Available: {self._format_troops(available, tribe_id)}",
            "info",
        )

        if not self._has_enough_troops(available, config.troops):
            await send_log("TROOPS", "❌", "Not enough troops for even one raid", "error")
            return stats
        await send_log("TROOPS", "✅", "Sufficient troops for raiding", "info")

        # ── STEP 2: Scan map ─────────────────────────────────────────
        if await check_stop():
            return stats
        await send_log(
            "SCAN",
            "🗺️",
            f"Scanning radius {config.radius} around ({cx}, {cy})...",
            "info",
        )
        oases = await self._scan_for_oases(config.radius, cx, cy)
        await send_log("SCAN", "🗺️", f"Found {len(oases)} unoccupied oases", "info")

        if not oases:
            await send_log("SCAN", "ℹ️", "No unoccupied oases found — nothing to raid", "info")
            stats["duration"] = round(time.monotonic() - t_start, 1)
            return stats

        # ── STEP 3: Note bonus filter ────────────────────────────────
        if config.bonus_filter:
            await send_log(
                "FILTER",
                "🔽",
                f"Bonus filter active: {config.bonus_filter} — will apply per-oasis during enrichment",
                "info",
            )

        # ── STEP 4: Sort by distance (for logging) ──────────────────
        oases.sort(key=lambda o: o.distance)
        if oases:
            orig_order = ", ".join(f"({o.x}|{o.y}) {o.distance:.2f}" for o in oases[:6])
            suffix = ", ..." if len(oases) > 6 else ""
            await send_log(
                "SORT",
                "📏",
                f"Original distance order: {orig_order}{suffix}",
                "info",
            )

        if config.max_targets > 0:
            await send_log(
                "SORT", "📏", f"Will stop after {config.max_targets} successful raids", "info"
            )

        # ── STEP 4b: Humanize target order (Mitigation 1) ───────────
        original_sorted = list(oases)
        oases = self._humanize_target_order(oases)
        stats["order_entropy"] = self._compute_order_entropy(original_sorted, oases)

        humanized_order = ", ".join(f"({o.x}|{o.y}) {o.distance:.2f}" for o in oases[:6])
        suffix = ", ..." if len(oases) > 6 else ""
        await send_log(
            "HUMANIZE",
            "🎲",
            f"Weighted shuffle applied (factor: {SHUFFLE_WEIGHT_FACTOR})",
            "info",
        )
        await send_log(
            "HUMANIZE",
            "🎲",
            f"Raiding order: {humanized_order}{suffix}",
            "info",
        )

        stats["total"] = len(oases)

        # ── STEP 5: Process each target sequentially ─────────────────
        raids_in_burst = 0
        next_burst_size = _sample_burst_size()
        browse_counter = 0
        next_browse_at = random.randint(BROWSE_FREQ_MIN, BROWSE_FREQ_MAX)

        for i, oasis in enumerate(oases):
            if await check_stop():
                await send_log("STOP", "⛔", "Stopped by user", "warning")
                break

            # 5a — Troop gate
            if not self._has_enough_troops(available, config.troops):
                needed_str = self._format_troops(config.troops, tribe_id)
                have_str = self._format_troops(available, tribe_id)
                await send_log(
                    "TROOPS",
                    "⚠️",
                    f"Insufficient (have {have_str}, need {needed_str}) — entering sleep",
                    "warning",
                )
                await send_log(
                    "SLEEP",
                    "💤",
                    f"Progress: {i}/{stats['total']} processed, {stats['sent']} raids sent",
                    "info",
                )

                while not self._has_enough_troops(available, config.troops):
                    if await check_stop():
                        break
                    stats["sleep_cycles"] += 1
                    sleep_start = time.monotonic()
                    # Mitigation 4 — Noisy sleep instead of silent wait
                    await send_log(
                        "SLEEP",
                        "💤",
                        "Waiting for troops (browsing game meanwhile)...",
                        "info",
                    )
                    await self._noisy_sleep(
                        config.sleep_interval,
                        send_log,
                        check_stop,
                        village_id=village_id,
                    )
                    stats["sleep_time"] += time.monotonic() - sleep_start
                    available = await self._fetch_idle_troops(village_id)
                    avail_str = self._format_troops(available, tribe_id)
                    sufficient = self._has_enough_troops(available, config.troops)
                    await send_log(
                        "SLEEP",
                        "💤",
                        f"Checking troops... {avail_str} — "
                        f"{'SUFFICIENT! Resuming...' if sufficient else 'still insufficient'}",
                        "info",
                    )

                if await check_stop():
                    break
                if not self._has_enough_troops(available, config.troops):
                    stats["skipped_troops"] += len(oases) - i
                    break

            # 5a-ii — Micro-break check (Mitigation 6)
            if raids_in_burst >= next_burst_size:
                if await check_stop():
                    break
                brk = await self._take_micro_break(send_log, check_stop, village_id=village_id)
                stats["breaks_taken"] += 1
                stats["break_time"] += brk
                raids_in_burst = 0
                next_burst_size = _sample_burst_size()

            # 5a-iii — Random skip (Mitigation 5)
            remaining = len(oases) - i
            if self._should_randomly_skip(remaining):
                stats["skipped_random"] += 1
                await send_log(
                    "SKIP",
                    "🎲",
                    f"Randomly skipping ({oasis.x}|{oasis.y}) — simulating human selectivity",
                    "info",
                )
                if i < len(oases) - 1:
                    await send_log(
                        "NEXT",
                        "➡️",
                        f"Moving to target {i + 2}/{stats['total']}",
                        "info",
                    )
                continue

            # 5b-pre — Map browsing (Mitigation 2)
            browse_counter += 1
            if browse_counter >= next_browse_at:
                await self._simulate_map_browsing(send_log, village_id=village_id)
                stats["browse_pauses"] += 1
                browse_counter = 0
                next_browse_at = random.randint(BROWSE_FREQ_MIN, BROWSE_FREQ_MAX)

            # 5b — Enrich this single oasis
            await send_log(
                "ENRICH",
                "🔍",
                f"Fetching oasis details for ({oasis.x}|{oasis.y})...",
                "info",
            )
            try:
                detail = await self._fetch_oasis_detail(oasis.x, oasis.y)
            except Exception as e:
                await send_log(
                    "ENRICH",
                    "❌",
                    f"Failed to fetch ({oasis.x}|{oasis.y}): {e} — skipping",
                    "error",
                )
                continue

            bonus_str = detail.get("bonus", "Unknown")
            troops_dict = detail.get("troops", {})
            troops_str = detail.get("troops_str", "none")
            dist = detail.get("distance") or oasis.distance

            await send_log(
                "ENRICH",
                "🔍",
                f"({oasis.x}|{oasis.y}): Bonus {bonus_str} | Troops: {troops_str} | Distance: {dist:.2f}",
                "info",
            )

            # Bonus filter (applied now that we have detail)
            if config.bonus_filter:
                if not self._matches_bonus_filter(bonus_str, config.bonus_filter):
                    await send_log(
                        "FILTER",
                        "❌",
                        f"({oasis.x}|{oasis.y}) removed: {bonus_str} — no match",
                        "info",
                    )
                    continue
                await send_log(
                    "FILTER",
                    "✅",
                    f"({oasis.x}|{oasis.y}) kept: {bonus_str} — matches",
                    "info",
                )

            # 5c — Classify
            if troops_dict:
                await send_log(
                    "CLASSIFY",
                    "⛔",
                    f"({oasis.x}|{oasis.y}) has troops ({troops_str}) — SKIPPING",
                    "warning",
                )
                stats["skipped_animals"].append(f"({oasis.x}|{oasis.y})")
                if i < len(oases) - 1:
                    await send_log(
                        "NEXT",
                        "➡️",
                        f"Moving to target {i + 2}/{stats['total']}",
                        "info",
                    )
                continue

            await send_log(
                "CLASSIFY",
                "✅",
                f"({oasis.x}|{oasis.y}) is EMPTY — proceeding to raid",
                "info",
            )

            # 5d-pre — Decision delay (Mitigation 3)
            think_s = await self._human_think_delay(send_log, check_stop)
            stats["think_delays"].append(round(think_s, 1))
            if await check_stop():
                break

            # 5d-jit — JIT re-verification before dispatch (skip in dry-run)
            # Checks for ANY troops (animals or human player units)
            if not config.dry_run:
                try:
                    jit_detail = await self._fetch_oasis_detail(oasis.x, oasis.y)
                    if jit_detail.get("has_any_troops"):
                        jit_troops = jit_detail.get("troops", {})
                        jit_str = (
                            self._format_animal_troops(jit_troops)
                            if jit_troops
                            else "player troops"
                        )
                        await send_log(
                            "JIT",
                            "⚠️",
                            f"({oasis.x}|{oasis.y}) now occupied ({jit_str}) — skipping",
                            "warning",
                        )
                        stats["skipped_animals"].append(f"({oasis.x}|{oasis.y})")
                        continue
                except Exception as exc:
                    await send_log(
                        "JIT",
                        "⚠️",
                        f"JIT recheck failed for ({oasis.x}|{oasis.y}): {exc} — proceeding",
                        "warning",
                    )

            # 5d — Send raid (or dry-run). raid_succeeded gates the local
            # troop deduction and burst counter — without this, a soft
            # failure (no confirmation form, rate-limit hint) would still
            # decrement the local count and the sweep would press on as if
            # the send happened.
            raid_succeeded = config.dry_run
            soft_failure = False
            raid_troops_str = self._format_troops(config.troops, tribe_id)
            if config.dry_run:
                await send_log(
                    "DRY RUN",
                    "🔍",
                    f"Would raid ({oasis.x}|{oasis.y}): {raid_troops_str} | Oasis troops: {troops_str}",
                    "info",
                )
                stats["sent"] += 1
            else:
                await send_log(
                    "RAID",
                    "⚔️",
                    f"Sending to ({oasis.x}|{oasis.y}): {raid_troops_str} | Oasis troops: {troops_str}",
                    "info",
                )
                try:
                    result = await self._send_raid(oasis.x, oasis.y, config.troops, village_id)
                    if result.success:
                        await send_log(
                            "RAID",
                            "✅",
                            f"Successfully sent to ({oasis.x}|{oasis.y})",
                            "success",
                        )
                        stats["sent"] += 1
                        raid_succeeded = True
                    else:
                        raw = (result.raw_response or "")[:200]
                        await send_log(
                            "RAID",
                            "❌",
                            f"Failed: {raw}",
                            "error",
                        )
                        # No-confirmation-form / rate-limit / generic page is
                        # a soft block. Pause this sweep — a real player who
                        # saw the form stop appearing would close the tab,
                        # not fire 30 more raids in 60 seconds.
                        soft_failure = True
                except Exception as e:
                    await send_log("RAID", "❌", f"Failed: {e}", "error")
                    soft_failure = True

            if raid_succeeded:
                # 5e — Deduct troops only when the send actually went through.
                for key, amount in config.troops.items():
                    available[key] = available.get(key, 0) - amount
                remaining_str = self._format_troops(available, tribe_id)
                await send_log("TROOPS", "🏠", f"Remaining: {remaining_str}", "info")
                if self._has_enough_troops(available, config.troops):
                    await send_log("TROOPS", "✅", "Sufficient for next raid", "info")
                else:
                    await send_log(
                        "TROOPS", "⚠️", "Insufficient — will check on next loop", "warning"
                    )
                # 5e-post — Increment burst counter (Mitigation 6 tracking)
                raids_in_burst += 1
            elif soft_failure:
                # Apply throttler penalty and abort the rest of the sweep.
                # Continuing through "ghost sends" with deducted-but-unsent
                # troops is exactly the pattern Travian's anti-bot scopes for.
                try:
                    self._http.throttler.add_penalty(random.uniform(45.0, 75.0))
                except Exception:
                    pass
                await send_log(
                    "STOP",
                    "🛑",
                    "Soft failure detected — pausing sweep (throttle penalty applied)",
                    "warning",
                )
                break

            # Check max raids limit
            if config.max_targets > 0 and stats["sent"] >= config.max_targets:
                await send_log(
                    "DONE",
                    "🎯",
                    f"Reached target of {config.max_targets} successful raid(s) — stopping",
                    "info",
                )
                break

        # ── STEP 6: Summary ──────────────────────────────────────────
        duration = time.monotonic() - t_start
        dur_min = int(duration // 60)
        dur_sec = int(duration % 60)
        sleep_min = int(stats["sleep_time"] // 60)
        sleep_sec = int(stats["sleep_time"] % 60)
        stats["duration"] = round(duration, 1)
        stats["sleep_time"] = round(stats["sleep_time"], 1)
        stats["break_time"] = round(stats["break_time"], 1)

        await send_log("DONE", "🏁", "Oasis Raider sweep complete", "success")
        await send_log("SUMMARY", "📊", f"Total targets: {stats['total']}", "info")
        await send_log("SUMMARY", "📊", f"Raids sent: {stats['sent']}", "info")
        if stats["skipped_animals"]:
            await send_log(
                "SUMMARY",
                "📊",
                f"Skipped (animals): {len(stats['skipped_animals'])} — {stats['skipped_animals']}",
                "info",
            )
        if stats["skipped_troops"] > 0:
            await send_log(
                "SUMMARY",
                "📊",
                f"Skipped (troops depleted): {stats['skipped_troops']}",
                "info",
            )
        if stats["skipped_random"] > 0:
            await send_log(
                "SUMMARY",
                "📊",
                f"Skipped (random human-skip): {stats['skipped_random']}",
                "info",
            )
        if stats["browse_pauses"] > 0:
            await send_log(
                "SUMMARY",
                "📊",
                f"Map browsing pauses: {stats['browse_pauses']}",
                "info",
            )
        if stats["breaks_taken"] > 0:
            brk_min = int(stats["break_time"] // 60)
            brk_sec = int(stats["break_time"] % 60)
            await send_log(
                "SUMMARY",
                "📊",
                f"Micro-breaks taken: {stats['breaks_taken']} (total: {brk_min}m {brk_sec:02d}s)",
                "info",
            )
        if stats["think_delays"]:
            avg_think = sum(stats["think_delays"]) / len(stats["think_delays"])
            await send_log(
                "SUMMARY",
                "📊",
                f"Avg decision delay: {avg_think:.1f}s",
                "info",
            )
        if stats["sleep_cycles"] > 0:
            await send_log(
                "SUMMARY",
                "📊",
                f"Sleep cycles: {stats['sleep_cycles']} (total pause: {sleep_min}m {sleep_sec:02d}s)",
                "info",
            )
        await send_log(
            "SUMMARY",
            "📊",
            f"Target order entropy: {stats['order_entropy']:.2f}/1.00",
            "info",
        )
        await send_log("SUMMARY", "📊", f"Duration: {dur_min}m {dur_sec:02d}s", "info")

        return stats

    # ── Anti-detection mitigations ───────────────────────────────────

    @staticmethod
    def _humanize_target_order(oases: list) -> list:
        """Mitigation 1 — Weighted random ordering.

        Closer oases have higher selection probability but the final
        sequence is NOT distance-sorted, breaking the geometric fingerprint.

        Uses weighted sampling without replacement.
        """
        if len(oases) <= 1:
            return list(oases)

        indices = list(range(len(oases)))
        weights = [1.0 / (oases[idx].distance + 0.1) ** SHUFFLE_WEIGHT_FACTOR for idx in indices]

        result: list = []
        while indices:
            chosen = random.choices(range(len(indices)), weights=weights, k=1)[0]
            result.append(oases[indices[chosen]])
            indices.pop(chosen)
            weights.pop(chosen)

        return result

    @staticmethod
    def _compute_order_entropy(original_sorted: list, humanized: list) -> float:
        """Measure shuffle quality: 0.0 = same as distance sort, 1.0 = max shuffled."""
        n = len(humanized)
        if n <= 1:
            return 1.0
        dist_rank = {id(o): i for i, o in enumerate(original_sorted)}
        ranks = [dist_rank[id(o)] for o in humanized]
        inversions = 0
        for i in range(n):
            for j in range(i + 1, n):
                if ranks[i] > ranks[j]:
                    inversions += 1
        max_inversions = n * (n - 1) / 2
        return round(inversions / max_inversions, 2) if max_inversions > 0 else 1.0

    async def _simulate_map_browsing(
        self,
        send_log: Callable,
        village_id: int | None = None,
    ) -> None:
        """Mitigation 2 — Simulate a human scrolling the map between oasis clicks.

        Use navigate_to_map (karte.php) so the next tile-details XHR
        carries a Referer chain consistent with what the browser would
        produce — a tile-details popup is opened from the map page, not
        from /profile.php or /statistiken.php.
        """
        await send_log(
            "BROWSE",
            "🗺️",
            "Simulating map browsing (natural navigation break)",
            "info",
        )
        try:
            navigator = getattr(self._http, "navigator", None)
            if navigator is not None and navigator.enabled:
                await navigator.navigate_to_map(village_id=village_id)
        except Exception as exc:
            logger.debug("Map browse navigation failed (non-critical): %s", exc)

    async def _human_think_delay(
        self,
        send_log: Callable,
        check_stop: Callable,
    ) -> float:
        """Mitigation 3 — Simulate human decision time before raiding.

        Triangular distribution: most delays cluster around the mode (12s)
        with occasional quick (5s) and slow (25s) decisions.

        The delay is split into 1-second chunks so check_stop can
        interrupt it promptly when the user clicks Stop.

        Returns actual seconds waited.
        """
        delay = random.triangular(THINK_DELAY_MIN, THINK_DELAY_MAX, THINK_DELAY_MODE)
        await send_log(
            "THINK",
            "🤔",
            "Reading oasis details... (simulating decision time)",
            "info",
        )
        elapsed = 0.0
        while elapsed < delay:
            if await check_stop():
                break
            chunk = min(1.0, delay - elapsed)
            await asyncio.sleep(chunk)
            elapsed += chunk
        await send_log(
            "THINK",
            "🤔",
            f"Decision made: proceeding to raid ({elapsed:.1f}s deliberation)",
            "info",
        )
        return elapsed

    async def _noisy_sleep(
        self,
        interval: float,
        send_log: Callable,
        check_stop: Callable,
        village_id: int | None = None,
    ) -> float:
        """Mitigation 4 — Active game browsing during troop wait.

        Breaks the sleep into short segments interleaved with random
        page visits, so the server sees continuous low-frequency activity
        instead of dead silence → sudden burst.

        Returns actual wall-clock seconds elapsed.
        """
        start = time.monotonic()
        newdid = f"?newdid={village_id}" if village_id else ""

        while (time.monotonic() - start) < interval:
            if await check_stop():
                break
            remaining = interval - (time.monotonic() - start)
            segment = random.uniform(NOISY_SLEEP_SEGMENT_MIN, NOISY_SLEEP_SEGMENT_MAX)
            segment = min(segment, remaining)
            if segment > 0:
                await asyncio.sleep(segment)

            remaining = interval - (time.monotonic() - start)
            if remaining <= 0:
                break
            if await check_stop():
                break

            # Noise action — visit a random game page in the raiding village context
            page = random.choice(_NOISE_PAGES)
            url = f"{page}{newdid}" if page in ("/dorf1.php", "/dorf2.php") else page
            try:
                await self._http.get_html(url, skip_reauth=True)
                await send_log(
                    "SLEEP",
                    "💤",
                    f"Visited {page} (natural activity)",
                    "info",
                )
            except Exception as exc:
                logger.debug("Noisy sleep page visit failed (non-critical): %s", exc)

        return time.monotonic() - start

    @staticmethod
    def _should_randomly_skip(remaining_count: int) -> bool:
        """Mitigation 5 — Simulate human not raiding every single oasis.

        Returns True ~10% of the time, but never if fewer than
        SKIP_MIN_REMAINING targets are left.
        """
        if remaining_count < SKIP_MIN_REMAINING:
            return False
        return random.random() < SKIP_PROBABILITY

    async def _take_micro_break(
        self,
        send_log: Callable,
        check_stop: Callable,
        village_id: int | None = None,
    ) -> float:
        """Mitigation 6 — Burst-and-break session pacing.

        After a burst of consecutive raids, take a short break with
        light game browsing to create a bursty activity pattern.

        Returns actual seconds elapsed.
        """
        duration = random.uniform(BREAK_DURATION_MIN, BREAK_DURATION_MAX)
        await send_log(
            "BREAK",
            "☕",
            f"Micro-break after raid burst ({duration:.0f}s, natural pacing)",
            "info",
        )
        elapsed = await self._noisy_sleep(
            duration,
            send_log,
            check_stop,
            village_id=village_id,
        )
        await send_log(
            "BREAK",
            "☕",
            f"Break over ({elapsed:.0f}s). Resuming sweep.",
            "info",
        )
        return elapsed

    # ── Private helpers ──────────────────────────────────────────────

    async def _fetch_idle_troops(self, village_id: int | None = None) -> Dict[str, int]:
        return await self._military.get_available_troops(village_id)

    @staticmethod
    def _has_enough_troops(available: Dict[str, int], needed: Dict[str, int]) -> bool:
        for key, amount in needed.items():
            if available.get(key, 0) < amount:
                return False
        return True

    @staticmethod
    def _format_troops(troops_dict: Dict[str, int], tribe_id: int = 2) -> str:
        mapping = TROOP_MAPPINGS.get(tribe_id, TROOP_MAPPINGS.get(TribeType.TEUTONS, {}))
        parts = []
        for key in sorted(troops_dict.keys(), key=lambda k: int(k[1:]) if k[1:].isdigit() else 99):
            count = troops_dict[key]
            if count > 0:
                name = mapping.get(key, key)
                parts.append(f"{count} {name}")
        return ", ".join(parts) if parts else "none"

    async def _scan_for_oases(self, radius: int, cx: int, cy: int) -> list:
        tiles = await self._scout_svc.scan_map(cx, cy, radius)
        return [t for t in tiles if t.is_oasis and not t.player_id]

    async def _fetch_oasis_detail(self, x: int, y: int) -> dict:
        """Fetch oasis detail via tile-details API and parse bonus + troops.

        Uses ``request_type="xhr"`` so the request carries the same
        X-Requested-With / Sec-Fetch-Mode=cors headers the Travian
        frontend would send when a player opens a tile popup from the
        map page.
        """
        resp = await self._http.post_json(
            "/api/v1/map/tile-details",
            {"x": x, "y": y},
            request_type="xhr",
        )
        html = resp.get("html", "")

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        bonus = self._parse_oasis_bonus(soup)
        troops = self._parse_oasis_troops(soup)

        # Detect ANY troops (including human player units, not just animals)
        # by checking the troop_info table for any non-zero count rows.
        has_any_troops = self._has_any_troops(soup)

        # Parse distance from id="distance" table
        distance = 0.0
        dist_table = soup.find("table", id="distance")
        if dist_table:
            bold = dist_table.find("td", class_="bold")
            if bold:
                m = re.search(r"[\d.]+", bold.get_text(strip=True))
                if m:
                    distance = float(m.group())

        return {
            "bonus": bonus,
            "troops": troops,
            "troops_str": self._format_animal_troops(troops),
            "distance": distance,
            "has_any_troops": has_any_troops,
        }

    @staticmethod
    def _parse_oasis_bonus(soup) -> str:
        table = soup.find("table", id="distribution")
        if not table:
            return "Unknown"
        bonuses: list[str] = []
        for row in table.find_all("tr"):
            desc = row.find("td", class_="desc")
            val = row.find("td", class_="val")
            if desc and val:
                resource = desc.get_text(strip=True)
                pct_match = re.search(r"\d+", val.get_text(strip=True))
                if pct_match and resource:
                    bonuses.append(f"{pct_match.group()}% {resource}")
        return ", ".join(bonuses) if bonuses else "Unknown"

    @staticmethod
    def _parse_oasis_troops(soup) -> Dict[str, int]:
        table = soup.find("table", id="troop_info")
        if not table:
            return {}
        troops: Dict[str, int] = {}
        for row in table.find_all("tr"):
            img = row.find("img", class_=re.compile(r"unit"))
            val = row.find("td", class_="val")
            if not img or not val:
                continue
            cls = " ".join(img.get("class", []))
            uid_match = re.search(r"u\d+", cls)
            if not uid_match:
                continue
            uid = uid_match.group()
            if uid not in ANIMAL_NAMES:
                continue
            try:
                count = int(val.get_text(strip=True))
                if count > 0:
                    troops[ANIMAL_NAMES[uid]] = count
            except ValueError:
                pass
        return troops

    @staticmethod
    def _has_any_troops(soup) -> bool:
        """Check if the oasis has ANY troops (animals or human player units)."""
        table = soup.find("table", id="troop_info")
        if not table:
            return False
        for row in table.find_all("tr"):
            val = row.find("td", class_="val")
            if val:
                try:
                    count = int(val.get_text(strip=True))
                    if count > 0:
                        return True
                except ValueError:
                    pass
        return False

    @staticmethod
    def _format_animal_troops(troops: Dict[str, int]) -> str:
        if not troops:
            return "none"
        return ", ".join(f"{count} {name}" for name, count in troops.items())

    @staticmethod
    def _matches_bonus_filter(bonus_str: str, filter_list: List[str]) -> bool:
        if not filter_list:
            return True
        bonus_lower = bonus_str.lower()
        for f in filter_list:
            keywords = BONUS_KEYWORDS.get(f.lower(), [f.lower()])
            if any(kw in bonus_lower for kw in keywords):
                return True
        return False

    async def _send_raid(
        self,
        x: int,
        y: int,
        troops: Dict[str, int],
        village_id: int | None = None,
    ):
        return await self._military.send_raid(x=x, y=y, troops=troops, village_id=village_id)
