"""Raid Analyzer Service — core analysis engine for raid target prioritization."""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..clients.http_client import HttpClient
from ..logging_config import get_logger
from ..models.auth import AuthState
from ..models.raid_analyzer import (
    AnalysisResult,
    AnalyzerSettings,
    RaidRecommendation,
    TargetVillageState,
)
from ..services.recon_account import acquire_recon_client
from ..services.reports_service import ReportsService

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Game constants
# ---------------------------------------------------------------------------

# unit_id -> (name, def_inf, upkeep)
UNIT_DEF_TABLE: Dict[str, Tuple[str, int, int]] = {
    # Gauls
    "u21": ("Phalanx", 40, 1),
    "u22": ("Swordsman", 35, 1),
    "u25": ("Druidrider", 115, 2),
    "u26": ("Haeduan", 60, 3),
    # Teutons
    "u11": ("Clubswinger", 20, 1),
    "u12": ("Spearman", 35, 1),
    "u13": ("Axeman", 10, 1),
    "u15": ("Paladin", 100, 2),
    "u16": ("Teutonic Knight", 50, 3),
    # Romans
    "u1": ("Legionnaire", 35, 1),
    "u2": ("Praetorian", 65, 1),
    "u3": ("Imperian", 40, 1),
    "u5": ("Eq. Imperatoris", 65, 3),
    "u6": ("Eq. Caesaris", 80, 4),
    # Huns
    "u61": ("Mercenary", 40, 1),
    "u62": ("Bowman", 30, 1),
    "u63": ("Marksman", 80, 2),
    "u64": ("Marauder", 60, 3),
    # Egyptians
    "u81": ("Slave Militia", 30, 1),
    "u82": ("Ash Warden", 55, 1),
    "u83": ("Khopesh Warrior", 50, 1),
    "u84": ("Anhur Guard", 110, 2),
    "u85": ("Resheph Chariot", 120, 3),
    # Nature (oasis defenders)
    "u31": ("Rat", 25, 1),
    "u32": ("Spider", 25, 1),
    "u33": ("Snake", 25, 1),
    "u34": ("Bat", 25, 1),
    "u35": ("Wild Boar", 33, 2),
    "u36": ("Wolf", 40, 1),
    "u37": ("Bear", 50, 3),
    "u38": ("Crocodile", 33, 3),
    "u39": ("Tiger", 60, 3),
    "u40": ("Elephant", 55, 5),
}

WALL_BASES: Dict[str, float] = {
    "teuton": 1.020,
    "gaul": 1.025,
    "roman": 1.030,
    "egyptian": 1.025,
    "hun": 1.020,
    "spartan": 1.020,
    "viking": 1.020,
}

WALL_NAMES: Dict[str, str] = {
    "Earth Wall": "teuton",
    "Palisade": "gaul",
    "City Wall": "roman",
    "Makeshift Wall": "egyptian",
    "Mud Wall": "hun",
    "Stone Wall": "spartan",
    "Wooden Fence": "viking",
}

# Clubswinger stats
CLUB_ATK = 40
CLUB_CARRY = 60
CLUB_COST = 250
CLUB_SPEED = 7  # fields/hour
CLUB_UPKEEP = 1

# Axeman stats
AXE_ATK = 60
AXE_CARRY = 50
AXE_COST = 490
AXE_SPEED = 6
AXE_UPKEEP = 1

T_SCOUT = 6.0  # hours — confidence decay half-life
T_REGEN = 8.0  # hours — resource regen reference window

CONCURRENCY_LIMIT = 20  # parallel report fetches


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def parse_report_date(date_str: str) -> Optional[datetime]:
    """Parse report list date string to datetime.

    Formats: 'today, 11:15' | 'yesterday, 14:30' | '22.03.26, 09:45'
    """
    now = datetime.now()
    date_str = date_str.strip()

    m = re.match(r"today,\s*(\d{1,2}):(\d{2})", date_str, re.I)
    if m:
        return now.replace(
            hour=int(m.group(1)),
            minute=int(m.group(2)),
            second=0,
            microsecond=0,
        )

    m = re.match(r"yesterday,\s*(\d{1,2}):(\d{2})", date_str, re.I)
    if m:
        yesterday = now - timedelta(days=1)
        return yesterday.replace(
            hour=int(m.group(1)),
            minute=int(m.group(2)),
            second=0,
            microsecond=0,
        )

    m = re.match(r"(\d{2})\.(\d{2})\.(\d{2}),\s*(\d{1,2}):(\d{2})", date_str)
    if m:
        return datetime(
            2000 + int(m.group(3)),
            int(m.group(2)),
            int(m.group(1)),
            int(m.group(4)),
            int(m.group(5)),
        )

    return None


def hours_since(dt: Optional[datetime]) -> Optional[float]:
    """Return hours elapsed since *dt*, or None."""
    if dt is None:
        return None
    delta = datetime.now() - dt
    return max(0.0, delta.total_seconds() / 3600)


def travian_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Euclidean distance on the Travian map (401×401 wrap-around)."""
    dx = abs(x1 - x2)
    dy = abs(y1 - y2)
    # Wrap-around for a 401×401 map (coords -200..200)
    if dx > 200:
        dx = 401 - dx
    if dy > 200:
        dy = 401 - dy
    return math.sqrt(dx * dx + dy * dy)


def smithy_stat(base: float, upkeep: int, level: int) -> float:
    """S(b, u, l) = b + (b + 300*u/7) * (1.007^l - 1). If l=0: b."""
    if level == 0:
        return base
    return base + (base + 300 * upkeep / 7) * (1.007**level - 1)


def extract_wall_info(buildings: List[Dict[str, Any]]) -> Tuple[int, str]:
    """Extract wall level and tribe from a building list."""
    for b in buildings:
        name = b.get("name", "")
        for wall_name, tribe in WALL_NAMES.items():
            if wall_name.lower() in name.lower():
                level_match = re.search(r"level\s*(\d+)", b.get("detail", ""))
                level = int(level_match.group(1)) if level_match else 0
                return level, tribe
    return 0, ""


def extract_trap_capacity(buildings: List[Dict[str, Any]]) -> int:
    """Extract total trap capacity from buildings list.

    Travian Kingdoms Trapper: 12 traps per level (L1=12, L10=120, L20=240).
    """
    for b in buildings:
        if "trapper" in b.get("name", "").lower():
            level_match = re.search(r"level\s*(\d+)", b.get("detail", ""))
            if level_match:
                level = int(level_match.group(1))
                return 12 * level
    return 0


def unsupported_defender_ids(defenders: dict[str, int]) -> list[str]:
    """Defender unit ids with no UNIT_DEF_TABLE entry, sorted for a stable reason.

    The hero is one of them: its defence depends on attributes no report
    carries, so it has no entry either. A defender listed here cannot be
    scored, and treating it as zero defence would read as "undefended".
    """
    return sorted(
        uid for uid, count in defenders.items() if count > 0 and uid not in UNIT_DEF_TABLE
    )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def calculate_score(
    state: TargetVillageState,
    my_x: int,
    my_y: int,
    smithy_level: int = 0,
    hero_offense: int = 0,
    hero_strength: int = 0,
) -> Optional[RaidRecommendation]:
    """Score a target using the raid dispatch formula.

    Finds the smallest n (clubs to send) where:
      OFF > DEF, profit > 0, surviving*60 >= eff_R

    Score = (profit / round_trip) * C_scout * C_confirm
    est_loot shows R (raw confirmed estimate), score uses eff_R.
    """
    dist = state.distance
    if dist == 0:
        return None

    # Require confirmed scout data
    if state.raidable_confidence == "none":
        return None
    if state.raidable_confidence == "depleted":
        return None

    R = state.estimated_raidable
    if R < 1:
        return None

    t_scout = hours_since(state.last_scout_time)
    t_raid = hours_since(state.last_raid_time)

    # ── eff_R: resource estimate for dispatch ──────────────────
    # Only apply regen decay for "scouted" confidence (no post-scout raids).
    # "raided" confidence already has bounties subtracted — don't double-penalize.
    if t_raid is None or state.raidable_confidence == "raided":
        eff_R = float(R)
    else:
        eff_R = R * min(1.0, t_raid / T_REGEN)

    if eff_R < 1:
        return None

    # ── Attack power ───────────────────────────────────────────
    club_atk = smithy_stat(CLUB_ATK, CLUB_UPKEEP, smithy_level)

    # Quick check: single club always dies if atk < 83 and no defenders
    # (still try, the loop handles it)

    # ── Refuse what cannot be scored ───────────────────────────
    if unsupported_defender_ids(state.defenders):
        return None

    # ── Defense calculation ────────────────────────────────────
    N_def = 0
    DEF = 10.0  # base
    for uid, count in state.defenders.items():
        if uid == "uhero":
            continue
        if uid in UNIT_DEF_TABLE:
            _, def_inf, upk = UNIT_DEF_TABLE[uid]
            DEF += count * smithy_stat(def_inf, upk, 0)
            N_def += count

    # Wall bonus
    if state.wall_level > 0 and state.wall_tribe in WALL_BASES:
        DEF *= WALL_BASES[state.wall_tribe] ** state.wall_level

    traps = state.trap_capacity

    # ── Find optimal n_send ────────────────────────────────────
    best_n = None
    best_profit = 0.0

    for n in range(1, 5001):
        trapped = min(n, traps)
        fighters = n - trapped
        if fighters <= 0:
            continue

        OFF = (fighters * club_atk + hero_strength) * (1 + hero_offense * 0.002)
        if OFF <= DEF:
            continue

        total_units = fighters + N_def
        if total_units <= 1000:
            K = 1.5
        else:
            K = max(1.2578, min(1.5, 2 * (1.8592 - total_units**0.015)))

        x = (DEF / OFF) ** K
        loss_ratio = x / (1 + x)
        combat_dead = round(fighters * loss_ratio)
        total_dead = combat_dead + trapped
        surviving = n - total_dead

        if surviving <= 0:
            continue

        loot = min(surviving * CLUB_CARRY, eff_R)
        profit = loot - total_dead * CLUB_COST

        if profit <= 0:
            continue

        # All three conditions met: OFF > DEF, profit > 0
        # Check if carry capacity covers eff_R
        if surviving * CLUB_CARRY >= eff_R:
            best_n = n
            best_profit = profit
            break  # smallest n that satisfies everything

        # Store first profitable n, keep searching for full carry
        if best_n is None or profit > best_profit:
            best_n = n
            best_profit = profit

    if best_n is None:
        return None

    # ── Recalculate at best_n for final values ─────────────────
    n = best_n
    trapped = min(n, traps)
    fighters = n - trapped
    OFF = (fighters * club_atk + hero_strength) * (1 + hero_offense * 0.002)
    total_units = fighters + N_def
    K = 1.5 if total_units <= 1000 else max(1.2578, min(1.5, 2 * (1.8592 - total_units**0.015)))
    x = (DEF / OFF) ** K
    loss_ratio = x / (1 + x)
    combat_dead = round(fighters * loss_ratio)
    total_dead = combat_dead + trapped
    surviving = n - total_dead
    loot = min(surviving * CLUB_CARRY, eff_R)
    profit = loot - total_dead * CLUB_COST

    # ── Confidence adjustments ─────────────────────────────────
    C_scout = T_SCOUT / (T_SCOUT + t_scout) if t_scout is not None else 0.5
    C_confirm = 1.2 if (t_raid is not None and t_scout is not None and t_raid < t_scout) else 1.0

    round_trip = 2 * dist / CLUB_SPEED
    if round_trip <= 0:
        return None

    score = round((profit / round_trip) * C_scout * C_confirm, 2)

    mode = "ATTACK" if traps > 0 else "RAID"

    return RaidRecommendation(
        n_send=n,
        unit_type="CLUB",
        send_label=f"{n} CLUB",
        profit=profit,
        score=score,
        mode=mode,
        round_trip_minutes=round(round_trip * 60),
        est_loot=R,  # Display raw confirmed estimate, not eff_R
    )


# ---------------------------------------------------------------------------
# State reconstruction
# ---------------------------------------------------------------------------

WAREHOUSE_RATIO = 0.67  # fallback stealable fraction when carry icon not parsed


def reconstruct_state(
    coord_key: Tuple[int, int],
    reports: List[Dict[str, Any]],
    my_player_name: str,
) -> TargetVillageState:
    """Reconstruct target state from reports.

    Algorithm (matches reference implementation):
    1. Find the newest RESOURCE scout (total > 10, not espionage-only)
    2. The carry icon value IS the stealable amount directly
    3. Sum ALL raid bounties that happened AFTER that scout
    4. If ANY post-scout raid had stillLeft=false (carry NOT full) → est=0
    5. remaining = stealable - totalTaken

    Each entry in *reports* has keys: type, data, report_id, timestamp.
    """
    state = TargetVillageState(x=coord_key[0], y=coord_key[1])

    # Separate scouts and raids, newest first
    scouts: List[Dict[str, Any]] = []
    raids: List[Dict[str, Any]] = []

    for report in reports:
        rtype = report.get("type")
        data = report.get("data")
        report_id = report.get("report_id", "")
        timestamp = report.get("timestamp")

        if data is None:
            continue

        d = data if isinstance(data, dict) else data.model_dump()

        # Populate identity from parsed data or GQL fallback
        if rtype == "scout":
            target = d.get("target", {})
            state.village_name = target.get("village_name") or state.village_name
            state.player_name = target.get("player_name") or state.player_name
            state.village_id = target.get("village_id") or state.village_id
        elif rtype == "battle":
            defender = d.get("defender", {})
            state.village_name = defender.get("village_name") or state.village_name
            state.player_name = defender.get("player_name") or state.player_name
            state.village_id = defender.get("village_id") or state.village_id

        # GQL fallback for identity fields
        state.village_name = state.village_name or report.get("_gql_vname", "")
        state.player_name = state.player_name or report.get("_gql_pname", "")
        state.village_id = state.village_id or report.get("_gql_vid", 0)

        entry = {"type": rtype, "data": d, "report_id": report_id, "timestamp": timestamp}

        if rtype == "scout":
            scouts.append(entry)
        elif rtype == "battle":
            raids.append(entry)

        # Track newest report time (not last-iterated)
        if timestamp and (state.last_report_time is None or timestamp > state.last_report_time):
            state.last_report_time = timestamp
            state.last_report_id = report_id
        state.report_count += 1

    # Sort newest first
    scouts.sort(key=lambda r: r.get("timestamp") or datetime.min, reverse=True)
    raids.sort(key=lambda r: r.get("timestamp") or datetime.min, reverse=True)

    # ── Find newest valid resource scout ──────────────────────
    best_scout = None
    for sc in scouts:
        d = sc["data"]
        res = d.get("resources", {})
        total_res = sum(res.get(k, 0) for k in ("lumber", "clay", "iron", "crop"))
        if total_res <= 10:
            continue  # espionage-only scout, skip
        best_scout = sc
        break

    if best_scout:
        d = best_scout["data"]
        res = d.get("resources", {})
        steal = d.get("stealable_resources", {})
        total_res = sum(res.get(k, 0) for k in ("lumber", "clay", "iron", "crop"))

        # Carry icon value IS the stealable amount directly
        stealable = steal.get("raidable", 0)
        if stealable <= 0:
            # Fallback: use total * WAREHOUSE_RATIO
            stealable = round(total_res * WAREHOUSE_RATIO)

        state.estimated_raidable = stealable
        state.raidable_confidence = "scouted"
        state.last_scout_time = best_scout["timestamp"]

        # Defenders from scout
        troops = d.get("troops", {})
        if troops:
            state.defenders = {k: v for k, v in troops.items() if v > 0}
            state.defender_source = "scout"
            state.defender_timestamp = best_scout["timestamp"]

        # Wall / trap info from scout
        buildings = d.get("buildings", [])
        wl, wt = extract_wall_info(buildings)
        if wl > 0:
            state.wall_level = wl
            state.wall_tribe = wt
        tc = extract_trap_capacity(buildings)
        if tc > 0:
            state.trap_capacity = tc

        # ── Sum post-scout raid bounties ──────────────────────
        scout_time = best_scout["timestamp"] or datetime.min
        total_taken = 0
        depleted_by_raid = False

        for raid in raids:
            raid_time = raid.get("timestamp") or datetime.min
            if raid_time <= scout_time:
                continue  # raid was before scout, irrelevant

            rd = raid["data"]
            bounty = rd.get("bounty", {})
            bounty_total = sum(bounty.get(k, 0) for k in ("lumber", "clay", "iron", "crop"))
            total_taken += bounty_total

            carry_full = rd.get("carry_full", False)
            if not carry_full:
                # Village was emptied by this raid
                depleted_by_raid = True

            # Track last raid
            state.last_raid_time = raid_time
            state.last_raid_bounty = bounty_total

        if depleted_by_raid:
            state.estimated_raidable = 0
            state.raidable_confidence = "depleted"
        else:
            state.estimated_raidable = max(0, stealable - total_taken)
            if total_taken > 0:
                state.raidable_confidence = "raided"

    # ── Update defenders from raids (most recent data wins) ───
    for raid in raids:
        rd = raid["data"]
        def_troops = rd.get("defender_troops", {})
        def_losses = rd.get("defender_losses", {})
        if def_troops:
            surviving: Dict[str, int] = {}
            for uid, count in def_troops.items():
                lost = def_losses.get(uid, 0)
                remaining = count - lost
                if remaining > 0:
                    surviving[uid] = remaining
            # Use newest raid's defender data
            if not state.defender_timestamp or (
                raid["timestamp"] and raid["timestamp"] > state.defender_timestamp
            ):
                state.defenders = surviving
                state.defender_source = "battle"
                state.defender_timestamp = raid["timestamp"]
        break  # newest raid only for defenders

    return state


# ---------------------------------------------------------------------------
# Optimised scoring helpers (v2)
# ---------------------------------------------------------------------------


def _score_undefended(
    eff_R: float, dist: float, t_scout, t_raid, hero_strength: int = 0, hero_offense: int = 0
) -> Optional[RaidRecommendation]:
    """Direct calculation for targets with no defenders and no traps."""
    if eff_R < 1 or dist <= 0:
        return None
    n = math.ceil(eff_R / CLUB_CARRY)
    if n < 1:
        n = 1
    loot = min(n * CLUB_CARRY, eff_R)
    profit = loot  # no losses → profit = loot
    C_scout = T_SCOUT / (T_SCOUT + t_scout) if t_scout is not None else 0.5
    C_confirm = 1.2 if (t_raid is not None and t_scout is not None and t_raid < t_scout) else 1.0
    round_trip = 2 * dist / CLUB_SPEED
    if round_trip <= 0:
        return None
    score = round((profit / round_trip) * C_scout * C_confirm, 2)
    return RaidRecommendation(
        n_send=n,
        unit_type="CLUB",
        send_label=f"{n} CLUB",
        profit=profit,
        score=score,
        mode="RAID",
        round_trip_minutes=round(round_trip * 60),
        est_loot=round(eff_R),
    )


def _score_defended_binary(
    state: TargetVillageState,
    eff_R: float,
    dist: float,
    club_atk: float,
    hero_strength: int,
    hero_offense: int,
    t_scout,
    t_raid,
) -> Optional[RaidRecommendation]:
    """Binary search for optimal n_send against defended targets."""
    N_def = 0
    DEF = 10.0
    for uid, count in state.defenders.items():
        if uid == "uhero":
            continue
        if uid in UNIT_DEF_TABLE:
            _, def_inf, upk = UNIT_DEF_TABLE[uid]
            DEF += count * smithy_stat(def_inf, upk, 0)
            N_def += count
    if state.wall_level > 0 and state.wall_tribe in WALL_BASES:
        DEF *= WALL_BASES[state.wall_tribe] ** state.wall_level
    traps = state.trap_capacity

    def _evaluate(n: int):
        trapped = min(n, traps)
        fighters = n - trapped
        if fighters <= 0:
            return None
        OFF = (fighters * club_atk + hero_strength) * (1 + hero_offense * 0.002)
        if OFF <= DEF:
            return None
        total_units = fighters + N_def
        K = 1.5 if total_units <= 1000 else max(1.2578, min(1.5, 2 * (1.8592 - total_units**0.015)))
        x = (DEF / OFF) ** K
        loss_ratio = x / (1 + x)
        combat_dead = round(fighters * loss_ratio)
        total_dead = combat_dead + trapped
        surviving = n - total_dead
        if surviving <= 0:
            return None
        loot = min(surviving * CLUB_CARRY, eff_R)
        profit = loot - total_dead * CLUB_COST
        return surviving, profit, total_dead, loot

    # Binary search: find smallest n where all conditions met
    lo, hi = 1, 5000
    best_n = None
    best_result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        res = _evaluate(mid)
        if res is not None:
            surviving, profit, _, _ = res
            if profit > 0 and surviving * CLUB_CARRY >= eff_R:
                best_n = mid
                best_result = res
                hi = mid - 1
            elif profit > 0:
                # Profitable but not enough carry — need more
                best_n = mid
                best_result = res
                lo = mid + 1
            else:
                lo = mid + 1
        else:
            lo = mid + 1

    if best_n is None:
        return None

    surviving, profit, total_dead, loot = best_result
    if profit <= 0:
        return None

    C_scout = T_SCOUT / (T_SCOUT + t_scout) if t_scout is not None else 0.5
    C_confirm = 1.2 if (t_raid is not None and t_scout is not None and t_raid < t_scout) else 1.0
    round_trip = 2 * dist / CLUB_SPEED
    if round_trip <= 0:
        return None
    score = round((profit / round_trip) * C_scout * C_confirm, 2)
    mode = "ATTACK" if traps > 0 else "RAID"
    return RaidRecommendation(
        n_send=best_n,
        unit_type="CLUB",
        send_label=f"{best_n} CLUB",
        profit=profit,
        score=score,
        mode=mode,
        round_trip_minutes=round(round_trip * 60),
        est_loot=round(eff_R),
    )


def calculate_score_v2(
    state: TargetVillageState,
    my_x: int,
    my_y: int,
    smithy_level: int = 0,
    hero_offense: int = 0,
    hero_strength: int = 0,
) -> Optional[RaidRecommendation]:
    """Score a target using optimised v2 paths (direct calc / binary search)."""
    dist = state.distance
    if dist == 0:
        return None
    if state.raidable_confidence in ("none", "depleted"):
        return None
    R = state.estimated_raidable
    if R < 1:
        return None

    t_scout = hours_since(state.last_scout_time)
    t_raid = hours_since(state.last_raid_time)
    # Only apply regen decay when confidence is "scouted" (no post-scout raids).
    # When "raided", R is already reduced by subtracted bounties — don't double-penalize.
    if t_raid is None or state.raidable_confidence == "raided":
        eff_R = float(R)
    else:
        eff_R = R * min(1.0, t_raid / T_REGEN)
    if eff_R < 1:
        return None

    club_atk = smithy_stat(CLUB_ATK, CLUB_UPKEEP, smithy_level)

    # ── Refuse what cannot be scored ───────────────────────────
    # An unrecognised defender adds nothing to DEF, so without this the fast
    # path below reads its garrison as "no defenders" and predicts no losses.
    if unsupported_defender_ids(state.defenders):
        return None

    # Fast path: no defenders, no traps
    N_def = sum(v for k, v in state.defenders.items() if k != "uhero" and k in UNIT_DEF_TABLE)
    if N_def == 0 and state.trap_capacity == 0:
        return _score_undefended(eff_R, dist, t_scout, t_raid, hero_strength, hero_offense)

    # Defended: binary search
    return _score_defended_binary(
        state, eff_R, dist, club_atk, hero_strength, hero_offense, t_scout, t_raid
    )


# ---------------------------------------------------------------------------
# Main service class (v2 pipeline)
# ---------------------------------------------------------------------------

CONCURRENCY_LIMIT = 20  # for legacy methods


class RaidAnalyzerService:
    """Orchestrates the raid analysis pipeline (v2).

    Pipeline: scout inbox → GQL pre-filter → village-reports fetch → reconstruct → score → re-scout queue.
    """

    def __init__(
        self,
        client: HttpClient,
        auth_state: AuthState,
    ) -> None:
        self.client = client
        self.auth_state = auth_state
        self.reports_service = ReportsService(client)
        self._on_progress: Optional[Callable] = None
        from .village_report_cache import VillageReportCache

        self._cache = VillageReportCache()

    def on_progress(self, callback: Callable) -> None:
        self._on_progress = callback

    def _progress(self, phase: str, message: str, **kwargs) -> None:
        if self._on_progress:
            try:
                self._on_progress(phase, message, kwargs)
            except Exception:
                pass

    # ==================================================================
    # v2 Pipeline
    # ==================================================================

    async def analyze(self, settings: AnalyzerSettings) -> AnalysisResult:
        """Run the v2 analysis pipeline.

        Phases: scout inbox → GQL pre-filter → village-reports → reconstruct → score → re-scout.
        """
        start = time.monotonic()
        warnings: List[str] = []

        # Configure cache TTL
        self._cache._ttl = settings.cache_ttl_minutes * 60

        source = self._resolve_source_village(settings.village_id)
        my_x, my_y = source.x, source.y

        result = AnalysisResult(
            source_village_id=source.id,
            source_village_name=source.name,
            source_x=my_x,
            source_y=my_y,
            min_resources=settings.min_resources,
            max_report_age_hours=settings.max_report_age_hours,
            radius=settings.radius,
            excluded_alliances=settings.exclude_alliances + settings.nap_alliances,
            excluded_players=settings.exclude_players,
            pipeline_version="v2",
        )

        # ── Phase 1A: Scout-gated entry ───────────────────────────
        self._progress(
            "scout_inbox", "Scanning report inbox for scouts...", phase_num=1, total_phases=6
        )
        scout_coords, scout_gql = await self._phase_1a_scout_inbox(settings, result, warnings)
        if not scout_coords:
            result.warnings = warnings or [
                "No scout reports found in inbox within age limit. Run scouts first."
            ]
            result.analysis_duration_seconds = time.monotonic() - start
            return result

        # ── Phase 1B: GQL pre-filter ──────────────────────────────
        self._progress(
            "gql_filter",
            f"Pre-filtering {len(scout_coords)} coords...",
            phase_num=2,
            total_phases=6,
        )
        surviving_coords = await self._phase_1b_gql_prefilter(
            scout_coords,
            scout_gql,
            source,
            settings,
            result,
            warnings,
        )
        if not surviving_coords:
            result.warnings = warnings or [
                "All targets filtered out by radius/alliance/population."
            ]
            result.analysis_duration_seconds = time.monotonic() - start
            return result

        # ── Phase 1C: Fetch village-reports per target ────────────
        self._progress(
            "village_reports",
            f"Fetching reports for {len(surviving_coords)} targets...",
            phase_num=3,
            total_phases=6,
        )
        village_reports_map = await self._phase_1c_fetch_village_reports(
            surviving_coords,
            settings,
            result,
            warnings,
        )

        # ── Phase 2+3: Group, normalise, reconstruct ─────────────
        self._progress(
            "reconstruct", "Reconstructing target states...", phase_num=4, total_phases=6
        )
        all_states, re_scout_early = self._phase_23_reconstruct(
            village_reports_map,
            source,
            result,
        )

        # ── Phase 4: Score ────────────────────────────────────────
        self._progress(
            "scoring", f"Scoring {len(all_states)} targets...", phase_num=5, total_phases=6
        )
        scored, re_scout_from_score = self._phase_4_score(
            all_states,
            source,
            settings,
            result,
            warnings,
        )

        # ── Phase 5: Filter & sort ────────────────────────────────
        self._progress("filter_sort", "Filtering and ranking...", phase_num=6, total_phases=6)
        final_targets = self._phase_5_filter_sort(scored, settings, result)

        # ── Assemble result ───────────────────────────────────────
        result.targets = final_targets
        result.re_scout_targets = re_scout_early + re_scout_from_score
        result.warnings = warnings
        result.analysis_duration_seconds = time.monotonic() - start

        self._progress(
            "complete",
            f"Done: {len(final_targets)} targets, {len(result.re_scout_targets)} need re-scout",
        )
        return result

    # ==================================================================
    # Phase methods
    # ==================================================================

    async def _phase_1a_scout_inbox(
        self,
        settings: AnalyzerSettings,
        result: AnalysisResult,
        warnings: List[str],
    ) -> Tuple[List[Tuple[int, int]], Dict[str, Dict[str, Any]]]:
        """Fetch scout reports from inbox, deduplicate to unique (x,y) coords.

        Returns (unique_coords, gql_metadata_dict).
        """

        (
            reports_list,
            pages_fetched,
            pages_failed,
            failed_pages,
        ) = await self.reports_service.fetch_reports_robust(
            max_age_hours=settings.max_report_age_hours,
            max_pages=settings.max_pages,
        )
        result.total_reports_listed = len(reports_list)
        result.pages_fetched = pages_fetched
        result.pages_failed = pages_failed
        if pages_failed:
            warnings.append(f"{pages_failed} report pages failed to load")

        # Prefer scout reports; fall back to battle reports if no scouts
        scout_reports = [r for r in reports_list if r.report_type == "scout"]
        battle_reports = [r for r in reports_list if r.report_type == "battle"]
        self._progress(
            "scout_inbox",
            f"Found {len(scout_reports)} scouts, {len(battle_reports)} battles out of {len(reports_list)} total",
        )

        if scout_reports:
            entry_reports = scout_reports
        elif battle_reports:
            # No scouts — use raids as entry points (less data but still has coords + bounty)
            entry_reports = battle_reports
            warnings.append(
                f"No scout reports found. Using {len(battle_reports)} battle reports as entry points. "
                "Scout targets for better resource estimates."
            )
        else:
            return [], {}

        # Batch GQL metadata for coords + dedup
        scout_ids = [r.report_id for r in entry_reports]
        BATCH = 250
        all_gql: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(scout_ids), BATCH):
            batch = scout_ids[i : i + BATCH]
            meta = await self.reports_service.fetch_report_batch_metadata(batch)
            all_gql.update(meta)

        coords_set: set[Tuple[int, int]] = set()
        for rid, meta in all_gql.items():
            defender = meta.get("defender") or {}
            village = defender.get("village") or {}
            x, y = village.get("x"), village.get("y")
            if x is not None and y is not None:
                coords_set.add((int(x), int(y)))

        result.unique_coords_discovered = len(coords_set)
        self._progress("scout_inbox", f"Deduped to {len(coords_set)} unique target coords")
        return list(coords_set), all_gql

    async def _phase_1b_gql_prefilter(
        self,
        coords: List[Tuple[int, int]],
        scout_gql: Dict[str, Dict[str, Any]],
        source,
        settings: AnalyzerSettings,
        result: AnalysisResult,
        warnings: List[str],
    ) -> List[Tuple[int, int]]:
        """Filter coords by radius, alliance, NAP, population threshold."""
        all_exclude_alliances = set(settings.exclude_alliances + settings.nap_alliances)

        # Build a coord→village_id map from GQL data
        coord_to_vid: Dict[Tuple[int, int], int] = {}
        for meta in scout_gql.values():
            defender = meta.get("defender") or {}
            village = defender.get("village") or {}
            x, y = village.get("x"), village.get("y")
            vid = village.get("id")
            if x is not None and y is not None and vid:
                coord_to_vid[(int(x), int(y))] = int(vid)

        # Batch-query village metadata for alliance + population
        vid_meta: Dict[int, Dict[str, Any]] = {}
        unique_vids = list(set(coord_to_vid.values()))
        if unique_vids:
            # Village metadata (player / population / alliance) is account-
            # independent — mask the GraphQL batch through the recon account
            # by default, fall back to the primary when recon is unavailable.
            gql_client = await acquire_recon_client(self.client.base_url) or self.client
            BATCH = 250
            for i in range(0, len(unique_vids), BATCH):
                batch = unique_vids[i : i + BATCH]
                aliases = [
                    f"v{j}:village(id:{vid}){{player{{name population alliance{{tag}}}} population}}"
                    for j, vid in enumerate(batch)
                ]
                query = "{" + " ".join(aliases) + "}"
                try:
                    resp = await gql_client.post_json(
                        "/api/v1/graphql", {"query": query, "variables": {}}
                    )
                    data = resp.get("data", {})
                    for j, vid in enumerate(batch):
                        vdata = data.get(f"v{j}")
                        if vdata:
                            vid_meta[vid] = vdata
                except Exception as e:
                    logger.warning("GQL village batch failed: %s", e)
                    warnings.append(f"GQL village metadata batch failed: {e}")

        surviving: List[Tuple[int, int]] = []
        for x, y in coords:
            dist = travian_distance(source.x, source.y, x, y)

            # Radius filter
            if settings.radius and dist > settings.radius:
                result.skipped_out_of_range += 1
                continue

            vid = coord_to_vid.get((x, y))
            meta = vid_meta.get(vid, {}) if vid else {}
            player = meta.get("player") or {}
            alliance = player.get("alliance") or {}
            alliance_tag = alliance.get("tag", "")
            village_pop = meta.get("population", 0)

            # Alliance filter (own + NAP)
            if alliance_tag and alliance_tag in all_exclude_alliances:
                result.skipped_alliance += 1
                continue

            # Population threshold
            if settings.max_population is not None and village_pop > settings.max_population:
                continue

            # Player name exclusion
            player_name = player.get("name", "")
            if player_name and player_name in settings.exclude_players:
                result.skipped_player += 1
                continue

            surviving.append((x, y))

        result.coords_after_gql_filter = len(surviving)
        self._progress(
            "gql_filter",
            f"{len(surviving)} coords survive pre-filter (dropped {len(coords) - len(surviving)})",
        )
        return surviving

    async def _phase_1c_fetch_village_reports(
        self,
        coords: List[Tuple[int, int]],
        settings: AnalyzerSettings,
        result: AnalysisResult,
        warnings: List[str],
    ) -> Dict[Tuple[int, int], Dict[str, Any]]:
        """Parallel fetch_village_reports with caching and short-circuit."""
        sem = asyncio.Semaphore(settings.village_report_concurrency)
        village_reports_map: Dict[Tuple[int, int], Dict[str, Any]] = {}
        cache_hits = 0
        fetched = 0
        failed = 0
        total = len(coords)

        async def fetch_one(x: int, y: int, idx: int):
            nonlocal cache_hits, fetched, failed
            # Check cache
            cached = self._cache.get(x, y)
            if cached is not None:
                cache_hits += 1
                return (x, y), cached

            async with sem:
                try:
                    data = await self.reports_service.fetch_village_reports(
                        x,
                        y,
                        fetch_details=True,
                        max_detail_count=5,
                    )
                    self._cache.put(x, y, data)
                    fetched += 1
                    self._progress(
                        "village_reports",
                        f"Fetched ({x},{y}) — {fetched + cache_hits}/{total}",
                        done=fetched + cache_hits,
                        total=total,
                    )
                    return (x, y), data
                except Exception as e:
                    failed += 1
                    logger.warning("Village reports fetch failed for (%s,%s): %s", x, y, e)
                    return (x, y), None

        tasks = [fetch_one(x, y, i) for i, (x, y) in enumerate(coords)]
        results = await asyncio.gather(*tasks)

        for (x, y), data in results:
            if data is not None:
                village_reports_map[(x, y)] = data

        result.village_reports_fetched = fetched
        result.village_reports_cached = cache_hits
        result.village_reports_failed = failed
        if failed > 0:
            warnings.append(f"{failed} village report fetches failed")
        if failed > total // 2:
            warnings.append("More than half of fetches failed — check connection or try again")

        self._progress(
            "village_reports", f"Done: {fetched} fetched, {cache_hits} cached, {failed} failed"
        )
        return village_reports_map

    def _phase_23_reconstruct(
        self,
        village_reports_map: Dict[Tuple[int, int], Dict[str, Any]],
        source,
        result: AnalysisResult,
    ) -> Tuple[List[TargetVillageState], List]:
        """Normalise village-reports data, reconstruct state, short-circuit depleted."""
        from ..models.raid_analyzer import ReScoutTarget

        all_states: List[TargetVillageState] = []
        re_scout_early: list = []

        for (x, y), vr_data in village_reports_map.items():
            reports_raw = vr_data.get("reports", [])
            village_meta = vr_data.get("village", {})

            # Short-circuit: if most recent report is a non-full carry raid → depleted
            if reports_raw:
                top = reports_raw[0]
                top_carry_cur = top.get("carry_current", 0)
                top_carry_max = top.get("carry_max", 0)
                top_icon = top.get("icon_type", 0)
                # icon 1-3 = battle/raid results; carry_max > 0 means raid with carry data
                if top_icon in (1, 2) and top_carry_max > 0 and top_carry_cur < top_carry_max:
                    # Carry was not full → village was emptied
                    re_scout_early.append(
                        ReScoutTarget(
                            x=x,
                            y=y,
                            village_name=village_meta.get("name", ""),
                            player_name=village_meta.get("owner", ""),
                            reason="depleted",
                            distance=travian_distance(source.x, source.y, x, y),
                        )
                    )
                    continue

            # Normalise reports into reconstruct_state format
            normalized: List[Dict[str, Any]] = []
            for entry in reports_raw:
                detail = entry.get("detail")
                if detail is None:
                    continue
                timestamp = parse_report_date(entry.get("date_str", ""))
                normalized.append(
                    {
                        "type": detail.get("type"),
                        "data": detail.get("data"),
                        "report_id": entry.get("report_id", ""),
                        "timestamp": timestamp,
                        # Pass village meta as fallback for identity
                        "_gql_vname": village_meta.get("name", ""),
                        "_gql_pname": village_meta.get("owner", ""),
                    }
                )

            if not normalized:
                # No parseable reports — need re-scout
                re_scout_early.append(
                    ReScoutTarget(
                        x=x,
                        y=y,
                        village_name=village_meta.get("name", ""),
                        player_name=village_meta.get("owner", ""),
                        reason="no_scout_data",
                        distance=travian_distance(source.x, source.y, x, y),
                    )
                )
                continue

            state = reconstruct_state((x, y), normalized, self.auth_state.player_name)
            state.distance = travian_distance(source.x, source.y, x, y)

            # Populate from village meta if missing
            state.village_name = state.village_name or village_meta.get("name", "")
            state.player_name = state.player_name or village_meta.get("owner", "")
            state.village_population = village_meta.get("population", 0)
            state.alliance_tag = state.alliance_tag or village_meta.get("alliance", "")

            all_states.append(state)

        result.reports_fetched_ok = sum(
            len([r for r in vr.get("reports", []) if r.get("detail")])
            for vr in village_reports_map.values()
        )
        return all_states, re_scout_early

    def _phase_4_score(
        self,
        all_states: List[TargetVillageState],
        source,
        settings: AnalyzerSettings,
        result: AnalysisResult,
        warnings: List[str],
    ) -> Tuple[List[Tuple[TargetVillageState, RaidRecommendation]], list]:
        """Score targets. Separate depleted/stale into re-scout list."""
        from ..models.raid_analyzer import ReScoutTarget

        scored: List[Tuple[TargetVillageState, RaidRecommendation]] = []
        re_scout: list = []

        for state in all_states:
            # Depleted → re-scout
            if state.raidable_confidence == "depleted":
                re_scout.append(
                    ReScoutTarget(
                        x=state.x,
                        y=state.y,
                        village_name=state.village_name,
                        player_name=state.player_name,
                        reason="depleted",
                        last_report_time=state.last_report_time,
                        estimated_raidable_before=state.estimated_raidable,
                        distance=state.distance,
                    )
                )
                continue

            # Stale → re-scout
            scout_age = hours_since(state.last_scout_time)
            if scout_age is not None and scout_age > settings.stale_hours:
                re_scout.append(
                    ReScoutTarget(
                        x=state.x,
                        y=state.y,
                        village_name=state.village_name,
                        player_name=state.player_name,
                        reason="stale",
                        last_report_time=state.last_scout_time,
                        estimated_raidable_before=state.estimated_raidable,
                        distance=state.distance,
                    )
                )
                # Still score it (stale data is better than none) but flag it

            # Unscorable garrison → refuse by name, and say which unit did it
            unsupported = unsupported_defender_ids(state.defenders)
            if unsupported:
                warnings.append(
                    f"({state.x}|{state.y}) {state.village_name or '?'} skipped: "
                    f"no defence stats for unit ids {', '.join(unsupported)} "
                    "— losses cannot be predicted."
                )
                continue

            rec = calculate_score_v2(
                state,
                source.x,
                source.y,
                settings.smithy_level,
                settings.hero_offense,
                settings.hero_strength,
            )
            if rec is None:
                if state.raidable_confidence == "none":
                    result.skipped_needs_scout += 1
                else:
                    result.skipped_low_resources += 1
                continue

            scored.append((state, rec))
            self._progress(
                "target_scored",
                f"({state.x},{state.y}) {state.village_name}: score={rec.score:.1f}",
                targets_found=len(scored),
            )

        return scored, re_scout

    def _phase_5_filter_sort(
        self,
        scored: List[Tuple[TargetVillageState, RaidRecommendation]],
        settings: AnalyzerSettings,
        result: AnalysisResult,
    ) -> List[Tuple[TargetVillageState, RaidRecommendation]]:
        """Apply secondary filters and sort by score."""
        filtered: List[Tuple[TargetVillageState, RaidRecommendation]] = []
        all_exclude = set(settings.exclude_alliances + settings.nap_alliances)

        for state, rec in scored:
            if state.alliance_tag and state.alliance_tag in all_exclude:
                result.skipped_alliance += 1
                continue
            if state.player_name and state.player_name in settings.exclude_players:
                result.skipped_player += 1
                continue
            if rec.est_loot < settings.min_resources:
                result.skipped_low_resources += 1
                continue
            filtered.append((state, rec))

        filtered.sort(key=lambda x: x[1].score, reverse=True)
        return filtered

    # ==================================================================
    # Internal helpers
    # ==================================================================

    def _resolve_source_village(self, village_id: Optional[int]):
        """Find the source village from auth state."""
        villages = self.auth_state.villages
        if not villages:
            raise ValueError(
                "No villages found in auth state. Cannot compute distances. "
                "Check your login or reconnect to the Travian server."
            )
        if village_id:
            match = next((v for v in villages if v.id == village_id), None)
            if match:
                return match
        main = next((v for v in villages if v.is_main_village), None)
        return main or villages[0]
