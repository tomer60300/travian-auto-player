"""Raid Analyzer Service — core analysis engine for raid target prioritization."""

from __future__ import annotations

import asyncio
import math
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union

from ..clients.http_client import HttpClient
from ..logging_config import get_logger
from ..models.auth import AuthState
from ..models.raid_analyzer import (
    AnalyzerSettings,
    AnalysisResult,
    RaidRecommendation,
    TargetVillageState,
)
from ..models.reports import BattleReportData, ScoutReportData, ReportListItem
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

T_SCOUT = 6.0   # hours — confidence decay half-life
T_REGEN = 8.0   # hours — resource regen reference window

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

    m = re.match(r'today,\s*(\d{1,2}):(\d{2})', date_str, re.I)
    if m:
        return now.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0,
        )

    m = re.match(r'yesterday,\s*(\d{1,2}):(\d{2})', date_str, re.I)
    if m:
        yesterday = now - timedelta(days=1)
        return yesterday.replace(
            hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0,
        )

    m = re.match(r'(\d{2})\.(\d{2})\.(\d{2}),\s*(\d{1,2}):(\d{2})', date_str)
    if m:
        return datetime(
            2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)),
            int(m.group(4)), int(m.group(5)),
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
    return base + (base + 300 * upkeep / 7) * (1.007 ** level - 1)


def extract_wall_info(buildings: List[Dict[str, Any]]) -> Tuple[int, str]:
    """Extract wall level and tribe from a building list."""
    for b in buildings:
        name = b.get("name", "")
        for wall_name, tribe in WALL_NAMES.items():
            if wall_name.lower() in name.lower():
                level_match = re.search(r'level\s*(\d+)', b.get("detail", ""))
                level = int(level_match.group(1)) if level_match else 0
                return level, tribe
    return 0, ""


def extract_trap_capacity(buildings: List[Dict[str, Any]]) -> int:
    """Extract total trap capacity from buildings list."""
    for b in buildings:
        if "trapper" in b.get("name", "").lower():
            level_match = re.search(r'level\s*(\d+)', b.get("detail", ""))
            if level_match:
                level = int(level_match.group(1))
                return 4 + level * 36  # L1=40, L2=76, …
    return 0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_with_unit(
    state: TargetVillageState,
    eff_R: float,
    unit_atk: float,
    unit_carry: int,
    unit_cost: int,
    unit_speed: int,
    unit_upkeep: int,
    smithy_level: int,
    hero_offense: int,
    hero_strength: int,
    dist: float,
    t_scout: Optional[float],
    t_raid: Optional[float],
) -> Optional[Dict[str, Any]]:
    """Calculate score for a specific unit type against a target."""
    atk_per_unit = smithy_stat(unit_atk, unit_upkeep, smithy_level)

    DEF = 10.0  # base
    N_def = 0
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
    best_n = None
    best_profit = 0.0

    for n in range(1, 5001):
        trapped = min(n, traps)
        fighters = n - trapped
        if fighters <= 0:
            continue

        OFF = (fighters * atk_per_unit + hero_strength) * (1 + hero_offense * 0.002)
        if OFF <= DEF:
            continue

        total_units = fighters + N_def
        if total_units <= 1000:
            K = 1.5
        else:
            K = max(1.2578, min(1.5, 2 * (1.8592 - total_units ** 0.015)))

        x = (DEF / OFF) ** K
        loss_ratio = x / (1 + x)
        combat_dead = round(fighters * loss_ratio)
        total_dead = combat_dead + trapped
        surviving = n - total_dead
        if surviving <= 0:
            continue

        loot = min(surviving * unit_carry, eff_R)
        profit = loot - total_dead * unit_cost
        if profit <= 0:
            continue

        if surviving * unit_carry >= eff_R:
            best_n = n
            best_profit = profit
            break

        if best_n is None or profit > best_profit:
            best_n = n
            best_profit = profit

    if best_n is None:
        return None

    # Recalculate final values at best_n
    n = best_n
    trapped = min(n, traps)
    fighters = n - trapped
    OFF = (fighters * atk_per_unit + hero_strength) * (1 + hero_offense * 0.002)
    total_units = fighters + N_def
    K = 1.5 if total_units <= 1000 else max(1.2578, min(1.5, 2 * (1.8592 - total_units ** 0.015)))
    x_val = (DEF / OFF) ** K
    loss_ratio = x_val / (1 + x_val)
    combat_dead = round(fighters * loss_ratio)
    total_dead = combat_dead + trapped
    surviving = n - total_dead
    loot = min(surviving * unit_carry, eff_R)
    profit = loot - total_dead * unit_cost

    # Confidence adjustments
    C_scout = T_SCOUT / (T_SCOUT + t_scout) if t_scout is not None else 0.5
    C_confirm = 1.2 if (t_raid is not None and t_scout is not None and t_raid < t_scout) else 1.0

    round_trip = 2 * dist / unit_speed if unit_speed > 0 else 999
    if round_trip <= 0:
        return None

    score = (profit / round_trip) * C_scout * C_confirm

    return {
        "n_send": n,
        "profit": profit,
        "score": score,
        "round_trip_minutes": round(round_trip * 60),
        "est_loot": round(eff_R),  # Show what we estimate is there
        "surviving": surviving,
    }


# Scoring configuration
RAID_COOLDOWN_H = 2.0
OVER_RAID_PENALTY = 0.5
CONF_CARRY_FULL = 1.5
CONF_FRESH_INTEL = 1.3
DEF_HEAVY_PEN = 0.2
DEF_LIGHT_PEN = 0.6
TRAP_BOOST = 1.1
COMBAT_BUFFER_MIN = 10
COMBAT_BUFFER_RATIO = 2
TRAP_EXTRA_TROOPS = 15


def calculate_score(
    state: TargetVillageState,
    my_x: int,
    my_y: int,
    smithy_level: int = 0,
    hero_offense: int = 0,
    hero_strength: int = 0,
) -> Optional[RaidRecommendation]:
    """Score a target. Returns None if target has no scout data or is not viable.

    Scoring: (est * multipliers) / distance
    Troop recommendation: ceil(est / carry) + combat buffer
    """
    dist = state.distance
    if dist == 0:
        return None

    # Require confirmed scout data — no guessing
    if state.raidable_confidence == "none":
        return None
    if state.raidable_confidence == "depleted":
        return None  # needs re-scouting

    est = state.estimated_raidable
    if est < 1:
        return None

    t_scout = hours_since(state.last_scout_time)
    t_raid = hours_since(state.last_raid_time)

    # Defender analysis
    def_n = sum(v for k, v in state.defenders.items() if k != "uhero")
    is_defended = def_n > 0
    has_traps = state.trap_capacity > 0

    # Last raid carry ratio (from last_raid data — approximate)
    last_carry_ratio = 0.0

    # ── Score multipliers ──────────────────────────────────────
    mult = 1.0
    if last_carry_ratio >= 0.9:
        mult *= CONF_CARRY_FULL
    if t_scout is not None and t_scout < RAID_COOLDOWN_H:
        mult *= CONF_FRESH_INTEL
    if is_defended:
        mult *= DEF_HEAVY_PEN if def_n > 20 else DEF_LIGHT_PEN
    if has_traps:
        mult *= TRAP_BOOST
    if t_raid is not None and t_raid < RAID_COOLDOWN_H:
        mult *= OVER_RAID_PENALTY

    score = round((est * mult) / dist, 2)

    # ── Troop recommendation ───────────────────────────────────
    buffer = 0
    if is_defended:
        buffer = max(COMBAT_BUFFER_MIN, def_n * COMBAT_BUFFER_RATIO)
    if has_traps:
        buffer += TRAP_EXTRA_TROOPS

    clubs_needed = math.ceil(est / CLUB_CARRY) + buffer
    axes_needed = math.ceil(est / AXE_CARRY) + buffer

    club_rt = round(2 * dist / CLUB_SPEED * 60)
    axe_rt = round(2 * dist / AXE_SPEED * 60)

    # Use AXE if defended or traps, CLUB otherwise
    if is_defended or has_traps:
        unit_type = "AXE"
        n_send = axes_needed
        rt = axe_rt
    else:
        unit_type = "CLUB"
        n_send = clubs_needed
        rt = club_rt

    mode = "ATTACK" if has_traps else "RAID"

    return RaidRecommendation(
        n_send=n_send,
        unit_type=unit_type,
        send_label=f"{n_send} {unit_type}",
        profit=float(est),
        score=score,
        mode=mode,
        round_trip_minutes=rt,
        est_loot=est,
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

        # Populate identity from any report
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

        entry = {"type": rtype, "data": d, "report_id": report_id, "timestamp": timestamp}

        if rtype == "scout":
            scouts.append(entry)
        elif rtype == "battle":
            raids.append(entry)

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
            if not state.defender_timestamp or (raid["timestamp"] and raid["timestamp"] > state.defender_timestamp):
                state.defenders = surviving
                state.defender_source = "battle"
                state.defender_timestamp = raid["timestamp"]
        break  # newest raid only for defenders

    return state


# ---------------------------------------------------------------------------
# Main service class
# ---------------------------------------------------------------------------

class RaidAnalyzerService:
    """Orchestrates the full raid analysis pipeline."""

    def __init__(
        self,
        client: HttpClient,
        auth_state: AuthState,
    ) -> None:
        self.client = client
        self.auth_state = auth_state
        self.reports_service = ReportsService(client)

    async def analyze(self, settings: AnalyzerSettings) -> AnalysisResult:
        """Run the full analysis pipeline."""
        start = time.monotonic()
        warnings: List[str] = []

        # ── Source village ──────────────────────────────────────────
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
            excluded_alliances=settings.exclude_alliances,
            excluded_players=settings.exclude_players,
        )

        # ── Phase 1A: Fetch report list ────────────────────────────
        reports_list, pages_fetched, pages_failed, failed_pages = (
            await self.reports_service.fetch_reports_robust(
                max_age_hours=settings.max_report_age_hours,
                max_pages=settings.max_pages,
            )
        )
        result.pages_fetched = pages_fetched
        result.pages_failed = pages_failed
        result.total_reports_listed = len(reports_list)

        if pages_failed:
            warnings.append(
                f"{pages_failed} report pages failed to load "
                f"(pages {', '.join(str(p) for p in failed_pages)})"
            )

        # ── Phase 1B: Alliance reports (optional) ──────────────────
        if settings.include_alliance_reports:
            ally_reports, ally_ok = await self.reports_service.fetch_alliance_reports()
            if ally_ok:
                reports_list.extend(ally_reports)
            else:
                warnings.append(
                    "Alliance reports unavailable — route not discovered. "
                    "Only personal reports used."
                )

        # ── Filter by age and type ─────────────────────────────────
        now = datetime.now()
        max_age = timedelta(hours=settings.max_report_age_hours)
        filtered_list: List[ReportListItem] = []
        skipped_type = 0

        for r in reports_list:
            if r.report_type not in ("battle", "scout"):
                skipped_type += 1
                continue
            report_dt = parse_report_date(r.date_str)
            if report_dt and (now - report_dt) > max_age:
                continue
            filtered_list.append(r)

        result.reports_skipped_type = skipped_type

        if not filtered_list:
            result.warnings = warnings + ["No battle/scout reports found within age limit."]
            result.analysis_duration_seconds = time.monotonic() - start
            return result

        # ── Phase 1C: Fetch report details (parallel) ──────────────
        report_ids = [r.report_id for r in filtered_list]
        parsed_reports, fetch_ok, fetch_fail, fail_ids = await self._fetch_all_details(
            report_ids,
        )
        result.reports_fetched_ok = fetch_ok
        result.reports_fetched_fail = fetch_fail
        result.failed_report_ids = fail_ids

        if fetch_fail:
            warnings.append(
                f"{fetch_fail} individual reports failed to parse: "
                f"{', '.join(fail_ids[:20])}{'...' if len(fail_ids) > 20 else ''}"
            )

        # Attach timestamps from the list items
        list_lookup = {r.report_id: r for r in filtered_list}
        for pr in parsed_reports:
            rid = pr.get("report_id", "")
            li = list_lookup.get(rid)
            if li:
                pr["timestamp"] = parse_report_date(li.date_str) or now

        # ── Phase 1D: GraphQL metadata for coordinates + classification ──
        # GQL title is the authoritative source for report type:
        #   "scouts" → scout, "raids"/"attacks" → battle
        # Also fills missing coordinates from GQL defender data.
        await self._enrich_reports_with_graphql(parsed_reports)

        # Re-fetch and re-parse reports reclassified from battle→scout by GQL title.
        # These need parse_scout_report() to extract resources/cranny/carry properly.
        reclassified_ids = [
            pr["report_id"] for pr in parsed_reports
            if pr.get("type") == "scout" and pr.get("_original_type") == "battle"
        ]
        if reclassified_ids:
            from ..parsers.report_parser import parse_scout_report
            logger.info(
                f"Re-parsing {len(reclassified_ids)} reports as scout "
                f"(GQL title says scout, HTML parser said battle)"
            )
            sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
            async def refetch_as_scout(rid: str):
                async with sem:
                    try:
                        html = await self.client.get_html(f"/report?id={rid}")
                        return rid, parse_scout_report(html)
                    except Exception as e:
                        logger.error(f"Re-parse scout {rid} failed: {e}")
                        return rid, None

            refetch_results = await asyncio.gather(
                *[refetch_as_scout(rid) for rid in reclassified_ids]
            )
            re_map = {rid: data for rid, data in refetch_results if data is not None}

            for pr in parsed_reports:
                rid = pr.get("report_id", "")
                if rid in re_map:
                    pr["data"] = re_map[rid]
                    pr["type"] = "scout"

        # ── Validate parsed reports ────────────────────────────────
        parse_warnings = self._validate_parsed_reports(parsed_reports)
        if parse_warnings:
            warnings.extend(parse_warnings[:30])
            if len(parse_warnings) > 30:
                warnings.append(f"... and {len(parse_warnings) - 30} more parse warnings")

        # ── Phase 3: Group by target & reconstruct state ───────────
        targets_map: Dict[Tuple[int, int], List[Dict[str, Any]]] = defaultdict(list)

        for pr in parsed_reports:
            rtype = pr.get("type")
            data = pr.get("data")
            if data is None:
                continue
            d = data if isinstance(data, dict) else data.model_dump()

            x, y = 0, 0
            if rtype == "scout":
                target = d.get("target", {})
                coords = target.get("coordinates", {})
                x = coords.get("x", 0)
                y = coords.get("y", 0)
            elif rtype == "battle":
                defender = d.get("defender", {})
                coords = defender.get("coordinates", {})
                x = coords.get("x", 0)
                y = coords.get("y", 0)
            else:
                continue

            if x == 0 and y == 0:
                continue

            targets_map[(x, y)].append(pr)

        # Sort each group chronologically
        for key in targets_map:
            targets_map[key].sort(key=lambda r: r.get("timestamp", now))

        # Reconstruct states
        all_states: List[TargetVillageState] = []
        for coord_key, reports in targets_map.items():
            state = reconstruct_state(
                coord_key, reports, self.auth_state.player_name,
            )
            state.distance = travian_distance(my_x, my_y, state.x, state.y)
            all_states.append(state)

        # ── Phase 1D: Enrich with alliance/population via GraphQL ──
        await self._enrich_targets(all_states)

        # ── Phase 4+5: Score, filter, sort ─────────────────────────
        scored: List[Tuple[TargetVillageState, RaidRecommendation]] = []

        for state in all_states:
            if state.alliance_tag and state.alliance_tag in settings.exclude_alliances:
                result.skipped_alliance += 1
                continue
            if state.player_name in settings.exclude_players:
                result.skipped_player += 1
                continue
            if settings.radius and state.distance > settings.radius:
                result.skipped_out_of_range += 1
                continue

            rec = calculate_score(
                state, my_x, my_y,
                settings.smithy_level,
                settings.hero_offense,
                settings.hero_strength,
            )
            if rec is None:
                result.skipped_needs_scout += 1
                continue
            if rec.est_loot < settings.min_resources:
                result.skipped_low_resources += 1
                continue

            scored.append((state, rec))

        scored.sort(key=lambda x: x[1].score, reverse=True)

        result.targets = scored
        result.warnings = warnings
        result.analysis_duration_seconds = time.monotonic() - start

        if parsed_reports:
            last = max(parsed_reports, key=lambda r: r.get("timestamp", datetime.min))
            result.last_report_id = last.get("report_id", "")
            result.last_report_time = last.get("timestamp")

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_source_village(self, village_id: Optional[int]):
        """Find the source village from auth state."""
        villages = self.auth_state.villages
        if not villages:
            # Fallback: synthetic village at 0,0
            from ..models.auth import Village
            return Village(id=self.auth_state.village_id, name="Unknown", x=0, y=0)

        if village_id:
            match = next((v for v in villages if v.id == village_id), None)
            if match:
                return match

        # Default: main village or first village
        main = next((v for v in villages if v.is_main_village), None)
        return main or villages[0]

    async def _fetch_all_details(
        self,
        report_ids: List[str],
    ) -> Tuple[List[Dict[str, Any]], int, int, List[str]]:
        """Fetch all report details in parallel with concurrency limit."""
        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)
        fetched_ok = 0
        fetched_fail = 0
        failed_ids: List[str] = []
        results: List[Dict[str, Any]] = []

        async def fetch_one(rid: str) -> Optional[Dict[str, Any]]:
            async with sem:
                try:
                    detail = await self.reports_service.fetch_report_detail(rid)
                    return detail
                except Exception as e:
                    logger.error(f"Failed to fetch report {rid}: {e}")
                    return None

        tasks = [fetch_one(rid) for rid in report_ids]
        raw_results = await asyncio.gather(*tasks)

        for rid, detail in zip(report_ids, raw_results):
            if detail is None:
                fetched_fail += 1
                failed_ids.append(rid)
            else:
                fetched_ok += 1
                results.append(detail)

        return results, fetched_ok, fetched_fail, failed_ids

    async def _enrich_reports_with_graphql(
        self, parsed_reports: List[Dict[str, Any]],
    ) -> None:
        """Supplement parsed report data with GraphQL metadata.

        Battle report HTML often lacks defender coordinates — the GraphQL
        endpoint provides them along with village ID, name, and player name.
        """
        report_ids = [pr.get("report_id", "") for pr in parsed_reports if pr.get("report_id")]
        if not report_ids:
            return

        # Batch in groups of 250
        BATCH = 250
        all_meta: Dict[str, Dict[str, Any]] = {}
        for i in range(0, len(report_ids), BATCH):
            batch = report_ids[i:i + BATCH]
            meta = await self.reports_service.fetch_report_batch_metadata(batch)
            all_meta.update(meta)

        # Merge metadata into parsed reports
        for pr in parsed_reports:
            rid = pr.get("report_id", "")
            meta = all_meta.get(rid)
            if not meta:
                continue

            data = pr.get("data")
            if data is None:
                continue

            rtype = pr.get("type")
            d = data if isinstance(data, dict) else data.model_dump()

            defender_meta = meta.get("defender") or {}
            village_meta = defender_meta.get("village") or {}

            gql_x = village_meta.get("x", 0)
            gql_y = village_meta.get("y", 0)
            gql_vid = village_meta.get("id", 0)
            gql_vname = village_meta.get("name", "")
            gql_pname = defender_meta.get("playerName", "")

            # Use GraphQL timestamp if available (more reliable)
            gql_time = meta.get("time")
            if gql_time and isinstance(gql_time, (int, float)):
                pr["timestamp"] = datetime.fromtimestamp(gql_time)

            # Classify report type from GQL title (more reliable than HTML parsing)
            gql_title = meta.get("title", "")
            old_type = pr.get("type")
            if " scouts " in gql_title:
                if old_type != "scout":
                    pr["_original_type"] = old_type
                pr["type"] = "scout"
                rtype = "scout"
            elif " raids " in gql_title or " attacks " in gql_title:
                if old_type != "battle":
                    pr["_original_type"] = old_type
                pr["type"] = "battle"
                rtype = "battle"

            if rtype == "battle":
                defender = d.get("defender", {})
                coords = defender.get("coordinates", {})
                # Fill in missing coordinates from GraphQL
                if (not coords.get("x") and not coords.get("y")) and (gql_x or gql_y):
                    coords["x"] = gql_x
                    coords["y"] = gql_y
                    defender["coordinates"] = coords
                if not defender.get("village_id") and gql_vid:
                    defender["village_id"] = gql_vid
                if not defender.get("village_name") and gql_vname:
                    defender["village_name"] = gql_vname
                if not defender.get("player_name") and gql_pname:
                    defender["player_name"] = gql_pname
                d["defender"] = defender
                # Write back mutated dict (for Pydantic models, replace data)
                if not isinstance(data, dict):
                    pr["data"] = d
            elif rtype == "scout":
                target = d.get("target", {})
                coords = target.get("coordinates", {})
                if (not coords.get("x") and not coords.get("y")) and (gql_x or gql_y):
                    coords["x"] = gql_x
                    coords["y"] = gql_y
                    target["coordinates"] = coords
                if not target.get("village_id") and gql_vid:
                    target["village_id"] = gql_vid
                if not target.get("village_name") and gql_vname:
                    target["village_name"] = gql_vname
                if not target.get("player_name") and gql_pname:
                    target["player_name"] = gql_pname
                d["target"] = target
                if not isinstance(data, dict):
                    pr["data"] = d

    async def _enrich_targets(self, states: List[TargetVillageState]) -> None:
        """Enrich targets with alliance/population data via GraphQL batch."""
        village_ids = [s.village_id for s in states if s.village_id > 0]
        if not village_ids:
            return

        # Batch query villages for player info
        BATCH_SIZE = 250
        vid_to_state: Dict[int, List[TargetVillageState]] = defaultdict(list)
        for s in states:
            if s.village_id > 0:
                vid_to_state[s.village_id].append(s)

        unique_vids = list(set(village_ids))

        for i in range(0, len(unique_vids), BATCH_SIZE):
            batch = unique_vids[i:i + BATCH_SIZE]
            aliases = []
            for j, vid in enumerate(batch):
                aliases.append(
                    f'v{j}:village(id:{vid})'
                    f'{{player{{name population alliance{{tag}}}} population}}'
                )
            query = "{" + " ".join(aliases) + "}"

            try:
                response = await self.client.post_json(
                    "/api/v1/graphql", {"query": query, "variables": {}},
                )
                data = response.get("data", {})

                for j, vid in enumerate(batch):
                    alias = f"v{j}"
                    vdata = data.get(alias)
                    if not vdata:
                        continue
                    player = vdata.get("player") or {}
                    alliance = player.get("alliance") or {}

                    for s in vid_to_state.get(vid, []):
                        s.alliance_tag = alliance.get("tag", "")
                        s.player_name = player.get("name") or s.player_name
                        s.player_population = player.get("population", 0)
                        s.village_population = vdata.get("population", 0)

            except Exception as e:
                logger.warning(f"Failed to enrich village batch: {e}")

    def _validate_parsed_reports(
        self, parsed_reports: List[Dict[str, Any]],
    ) -> List[str]:
        """Validate parsed reports and return warnings."""
        warnings: List[str] = []

        for pr in parsed_reports:
            rid = pr.get("report_id", "?")
            rtype = pr.get("type", "unknown")
            data = pr.get("data")

            if rtype == "unknown":
                warnings.append(f"Report {rid}: type=unknown, could not classify")
                continue

            if data is None:
                continue

            d = data if isinstance(data, dict) else data.model_dump()

            if rtype == "scout":
                target = d.get("target", {})
                coords = target.get("coordinates", {})
                if not coords.get("x") and not coords.get("y"):
                    warnings.append(f"Report {rid}: scout with no coordinates")
                steal = d.get("stealable_resources", {})
                res = d.get("resources", {})
                total_res = sum(res.get(k, 0) for k in ("lumber", "clay", "iron", "crop"))
                if steal.get("raidable", 0) == 0 and total_res > 10:
                    warnings.append(
                        f"Report {rid}: scout has resources but raidable=0 (parsing issue?)"
                    )

            elif rtype == "battle":
                defender = d.get("defender", {})
                coords = defender.get("coordinates", {})
                if not coords.get("x") and not coords.get("y"):
                    warnings.append(f"Report {rid}: battle with no defender coordinates")
                if d.get("battle_result") == "unknown":
                    warnings.append(f"Report {rid}: battle result could not be determined")

        return warnings
