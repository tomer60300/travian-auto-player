"""Path 3 rebalance planner.

For every target coord, re-decides which village should fire on it, with which
unit and count, and which farm-list it should live in. Targets that fail an
economic floor are routed to per-owner-village ``V{n}-DEAD`` lists that the
operator keeps deactivated.

The objective the planner maximises:

    0.80 × normalised(expected_raids_per_day)
    + 0.20 × normalised(expected_daily_booty)

Frequency dominates because the operator runs Start All at irregular cadences
(roughly 25 min to 3 hours). A target the operator can hit on every Start All
is worth more than a higher-haul target whose round trip blocks half the cycles.

Read-only: this module returns Placement objects and never touches Travian.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ..logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tunable constants — see Methodology notes when changing.
# ---------------------------------------------------------------------------

# Operator-stated Start All cadence distribution (25 min to ~3 hours). Each
# entry is (cadence_midpoint_minutes, probability_weight). Weights sum to 1.0.
# Changing these reshapes the frequency component of the objective.
CADENCE_DISTRIBUTION: list[tuple[float, float]] = [
    (32.5, 0.60),
    (82.5, 0.30),
    (150.0, 0.10),
]

# Frequency / booty weighting in the objective. Operator-stated; do not change
# without a corresponding decision.
WEIGHT_FREQUENCY = 0.80
WEIGHT_BOOTY = 0.20

# Teuton strike units the planner is allowed to assign. t2 spear is defensive,
# t4 scout has zero carry, t7/t8 are slow-wave siege.
STRIKE_UNIT_CANDIDATES: tuple[str, ...] = ("t1", "t3", "t5", "t6")

# Game constants — fields/hour and carry. Mirror UNIT_PROFILES in the script.
UNIT_SPEED: dict[str, int] = {"t1": 7, "t2": 7, "t3": 6, "t4": 9, "t5": 10, "t6": 9}
UNIT_CARRY: dict[str, int] = {"t1": 60, "t2": 40, "t3": 50, "t4": 0, "t5": 110, "t6": 80}

UNIT_DISPLAY_NAME: dict[str, str] = {
    "t1": "Clubs",
    "t3": "Axes",
    "t5": "Pal",
    "t6": "TK",
}

# Safety floor: a single slot cannot claim more than this fraction of the
# village's available supply of that unit. Prevents one slot from draining V3
# of all its TKs.
SUPPLY_CLAIM_CAP = 0.90

# Carry safety multiplier when sizing a wave to cover the empirical cranny.
CARRY_SAFETY = 1.10

# Per-village role cutoffs (v5.0 wave-stacking): targets in each village's
# bucket are ranked by their TOTAL expected_haul across all waves, then split
# top 30% HIGH / next 50% MID / bottom 20% INACTIVE.
HIGH_FRACTION = 0.30
MID_FRACTION = 0.80  # below this → INACTIVE

# Dead-farm thresholds — see is_dead_farm().
DEAD_AVG_LOOT_THRESHOLD = 30.0
DEAD_MIN_RAIDS_FOR_LOW_LOOT = 5
DEAD_STALE_DAYS = 14.0
DEAD_STALE_AVG_LOOT_THRESHOLD = 50.0
DEAD_DEF_PROXY_LIMIT = 500


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VillagePosition:
    """Owner village position on the Travian map."""

    name: str
    x: int
    y: int


@dataclass(frozen=True)
class TroopSupply:
    """Current troop pool for one (village, unit) — used to gate placements."""

    village: str
    unit: str
    available: int


@dataclass
class Placement:
    """A planner decision about where a target should live.

    Not frozen because objective_score and target_list_{name,role} are filled
    in after construction (normalisation requires the full candidate set, and
    role assignment requires the full per-village placement set).
    """

    target_coord: tuple[int, int]
    optimal_village: str
    optimal_unit: str
    optimal_count: int
    round_trip_min: float
    expected_raids_per_day: float
    expected_daily_booty: float
    objective_score: float = 0.0
    target_list_name: str = ""
    target_list_role: str = ""
    # Provenance from the target aggregate — kept so the action emitter can
    # produce concrete manual_steps without re-querying the inventory.
    target_name: str = ""
    primary_owner_village: str = ""
    slot_instances: list[tuple[str, int]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------


def compute_round_trip_min(
    village_pos: VillagePosition, target: tuple[int, int], unit: str
) -> float:
    """Round-trip travel time in minutes on the 401×401 wrap-around map."""
    dx = abs(village_pos.x - target[0])
    dy = abs(village_pos.y - target[1])
    if dx > 200:
        dx = 401 - dx
    if dy > 200:
        dy = 401 - dy
    distance_fields = math.sqrt(dx * dx + dy * dy)
    speed_fh = UNIT_SPEED[unit]
    if speed_fh <= 0:
        return float("inf")
    one_way_min = (distance_fields / speed_fh) * 60.0
    return one_way_min * 2.0


def compute_expected_raids_per_day(round_trip_min: float) -> float:
    """Expected raids per day under the operator's Start All cadence mix.

    At each cadence bucket the slot fires once per max(round_trip, cadence).
    Round trips longer than the cadence cap the rate (the unit can't be home
    in time); shorter ones leave it idle waiting for the next Send All.
    """
    raids_per_day = 0.0
    for cadence_min, prob in CADENCE_DISTRIBUTION:
        gap = max(round_trip_min, cadence_min)
        if gap <= 0:
            continue
        raids_per_day += prob * (1440.0 / gap)
    return raids_per_day


# ---------------------------------------------------------------------------
# Placement scoring
# ---------------------------------------------------------------------------


def compute_placement_score(
    target_agg: Any,
    village_pos: VillagePosition,
    unit: str,
    supply: int,
) -> Placement | None:
    """Score a single (target, village, unit) candidate.

    Returns None when infeasible: unit has zero carry, would drain the village
    of more than SUPPLY_CLAIM_CAP of its supply, or the avg_loot is zero.
    """
    carry = UNIT_CARRY.get(unit, 0)
    if carry <= 0:
        return None
    avg_loot = float(target_agg.avg_loot or 0.0)
    if avg_loot <= 0:
        return None
    count = max(1, math.ceil(avg_loot * CARRY_SAFETY / carry))
    if supply <= 0 or count > supply * SUPPLY_CLAIM_CAP:
        return None
    round_trip = compute_round_trip_min(village_pos, target_agg.coord, unit)
    raids_per_day = compute_expected_raids_per_day(round_trip)
    haul_per_raid = min(count * carry, avg_loot)
    daily_booty = raids_per_day * haul_per_raid
    return Placement(
        target_coord=target_agg.coord,
        optimal_village=village_pos.name,
        optimal_unit=unit,
        optimal_count=count,
        round_trip_min=round_trip,
        expected_raids_per_day=raids_per_day,
        expected_daily_booty=daily_booty,
        target_name=getattr(target_agg, "target_name", ""),
        primary_owner_village=getattr(target_agg, "primary_owner_village", ""),
        slot_instances=list(getattr(target_agg, "slot_instances", []) or []),
    )


def _field_distance(vp: VillagePosition, coord: tuple[int, int]) -> float:
    """Wrap-around Euclidean field distance from a village to a target."""
    dx = abs(vp.x - coord[0])
    dy = abs(vp.y - coord[1])
    if dx > 200:
        dx = 401 - dx
    if dy > 200:
        dy = 401 - dy
    return math.sqrt(dx * dx + dy * dy)


def pick_best_placement(
    target_agg: Any,
    village_positions: list[VillagePosition],
    troop_supplies: dict[tuple[str, str], int],
) -> Placement | None:
    """Enumerate (village, unit) candidates, score, normalise, pick the winner.

    troop_supplies is keyed by (village_label, unit_id) and the value is the
    current available count in that village.

    Note: as of v5.0 (wave-stacking) plan_rebalance no longer calls this — it
    uses plan_waves_for_target instead. Kept for back-compat and for callers
    that need a single best placement (e.g., tests).
    """
    candidates: list[Placement] = []
    for vp in village_positions:
        for unit in STRIKE_UNIT_CANDIDATES:
            supply = int(troop_supplies.get((vp.name, unit), 0))
            if supply < 1:
                continue
            placement = compute_placement_score(target_agg, vp, unit, supply)
            if placement is not None:
                candidates.append(placement)
    if not candidates:
        return None

    max_raids = max((c.expected_raids_per_day for c in candidates), default=0.0)
    max_booty = max((c.expected_daily_booty for c in candidates), default=0.0)
    for c in candidates:
        freq_norm = (c.expected_raids_per_day / max_raids) if max_raids > 0 else 0.0
        booty_norm = (c.expected_daily_booty / max_booty) if max_booty > 0 else 0.0
        c.objective_score = WEIGHT_FREQUENCY * freq_norm + WEIGHT_BOOTY * booty_norm

    return max(candidates, key=lambda c: c.objective_score)


# ---------------------------------------------------------------------------
# Role / list-name assignment
# ---------------------------------------------------------------------------


def assign_role_and_list_name(
    village: str,
    placements_for_village: list[Placement],
) -> list[Placement]:
    """Assign HIGH/MID/INACTIVE roles within a single village's placements.

    Top HIGH_FRACTION by ranking score → HIGH, then up to MID_FRACTION → MID,
    rest → INACTIVE. Constructs target_list_name as ``V{n}-{ROLE}-{Unit}``.
    Mutates the placements in-place and returns them sorted by score desc.

    For v5.0 wave plans, callers should set p.objective_score to the target's
    TOTAL expected_haul across all waves (not the per-wave score) before
    calling this, and group by (village, target) so each (village, target)
    pair contributes one ranking entry. Multiple waves for the same target +
    village would all inherit the same role.
    """
    sorted_placements = sorted(placements_for_village, key=lambda p: -p.objective_score)
    n = len(sorted_placements)
    high_cut = int(n * HIGH_FRACTION)
    mid_cut = int(n * MID_FRACTION)
    for idx, p in enumerate(sorted_placements):
        if idx < high_cut:
            role = "HIGH"
        elif idx < mid_cut:
            role = "MID"
        else:
            role = "INACTIVE"
        unit_display = UNIT_DISPLAY_NAME.get(p.optimal_unit, p.optimal_unit)
        p.target_list_role = role
        p.target_list_name = f"{village}-{role}-{unit_display}"
    return sorted_placements


# ---------------------------------------------------------------------------
# Dead-farm verdict
# ---------------------------------------------------------------------------


def is_dead_farm(target_agg: Any, *, now_unix: float) -> tuple[bool, str]:
    """Return (is_dead, reason).

    Reason is a short string used directly in RELOCATE_TO_DEAD.reason.
    """
    avg_loot = float(target_agg.avg_loot or 0.0)
    total_raids = int(target_agg.total_raids_all_lists or 0)
    last_raid = target_agg.last_raid_time_unix
    max_def = int(target_agg.max_def_proxy or 0)
    any_ct = bool(target_agg.any_ct2_ct3_flag)

    if avg_loot < DEAD_AVG_LOOT_THRESHOLD and total_raids >= DEAD_MIN_RAIDS_FOR_LOW_LOOT:
        return True, (
            f"avg_loot {avg_loot:.1f} < {DEAD_AVG_LOOT_THRESHOLD:.0f} "
            f"with {total_raids} raids (>= {DEAD_MIN_RAIDS_FOR_LOW_LOOT})"
        )

    if last_raid is not None:
        days_since = (now_unix - float(last_raid)) / 86400.0
        if days_since > DEAD_STALE_DAYS and avg_loot < DEAD_STALE_AVG_LOOT_THRESHOLD:
            return True, (
                f"last raid {days_since:.1f}d ago (> {DEAD_STALE_DAYS:.0f}d) "
                f"and avg_loot {avg_loot:.1f} < {DEAD_STALE_AVG_LOOT_THRESHOLD:.0f}"
            )

    if max_def > DEAD_DEF_PROXY_LIMIT:
        return True, f"max_def_proxy {max_def} > {DEAD_DEF_PROXY_LIMIT}"

    if any_ct:
        return True, "pushing-protection suspect (CT2/CT3)"

    return False, ""


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeadDecision:
    """A target routed to its owner village's DEAD list."""

    target_coord: tuple[int, int]
    target_name: str
    primary_owner_village: str
    reason: str
    avg_loot: float
    total_raids: int
    last_raid_days_ago: float | None
    slot_instances: list[tuple[str, int]]
    target_list_name: str  # 'V{n}-DEAD'
    target_list_role: str = "DEAD"


@dataclass
class RebalancePlan:
    """Top-level output of the rebalance pass."""

    placements: list[Placement]
    dead_decisions: list[DeadDecision]
    unplaceable_as_dead: int  # targets the planner could not place; treated as dead


def plan_rebalance(
    target_inventory: dict[tuple[int, int], Any],
    village_positions: list[VillagePosition],
    troop_supplies: dict[tuple[str, str], int],
    *,
    now_unix: float,
) -> RebalancePlan:
    """Run the full rebalance pass over a target inventory.

    For each target:
      1. is_dead_farm → DeadDecision into V{n}-DEAD for the primary owner.
      2. Otherwise pick_best_placement. If the planner finds no feasible
         (village, unit) pairing the target also lands in V{n}-DEAD with reason
         ``no_feasible_placement`` (counted in ``unplaceable_as_dead``).
      3. After every survivor has a (village, unit) pick, group by village and
         run assign_role_and_list_name so each placement gets its final
         HIGH/MID/INACTIVE role and list name.
    """
    placements: list[Placement] = []
    dead_decisions: list[DeadDecision] = []
    unplaceable_as_dead = 0

    for coord, target_agg in target_inventory.items():
        owner = (
            target_agg.primary_owner_village
            if target_agg.primary_owner_village
            else _fallback_owner(village_positions, coord)
        )
        dead, reason = is_dead_farm(target_agg, now_unix=now_unix)
        if dead:
            dead_decisions.append(
                _build_dead_decision(target_agg, owner, reason, now_unix)
            )
            continue

        best = pick_best_placement(target_agg, village_positions, troop_supplies)
        if best is None:
            unplaceable_as_dead += 1
            dead_decisions.append(
                _build_dead_decision(target_agg, owner, "no_feasible_placement", now_unix)
            )
            continue
        placements.append(best)

    # Bucket per chosen village and assign roles. v5.0 wave-stacking groups
    # per-(village, target) so multiple waves of the same target in the same
    # village inherit one role. (In practice each (village, target) emits at
    # most one wave because the spacing constraint forces distinct units.)
    by_village: dict[str, list[Placement]] = {}
    for p in placements:
        by_village.setdefault(p.optimal_village, []).append(p)
    ordered_placements: list[Placement] = []
    for village, group in by_village.items():
        ordered_placements.extend(assign_role_and_list_name(village, group))

    logger.info(
        "rebalance_planner: %d targets, %d placements, %d dead (incl %d unplaceable)",
        len(target_inventory),
        len(ordered_placements),
        len(dead_decisions),
        unplaceable_as_dead,
    )
    return RebalancePlan(
        placements=ordered_placements,
        dead_decisions=dead_decisions,
        unplaceable_as_dead=unplaceable_as_dead,
    )


def _build_dead_decision(
    target_agg: Any, owner: str, reason: str, now_unix: float
) -> DeadDecision:
    last_raid = target_agg.last_raid_time_unix
    days_ago = ((now_unix - float(last_raid)) / 86400.0) if last_raid else None
    return DeadDecision(
        target_coord=target_agg.coord,
        target_name=getattr(target_agg, "target_name", ""),
        primary_owner_village=owner,
        reason=reason,
        avg_loot=float(target_agg.avg_loot or 0.0),
        total_raids=int(target_agg.total_raids_all_lists or 0),
        last_raid_days_ago=days_ago,
        slot_instances=list(getattr(target_agg, "slot_instances", []) or []),
        target_list_name=f"{owner}-DEAD" if owner else "UNOWNED-DEAD",
    )


def _fallback_owner(village_positions: list[VillagePosition], coord: tuple[int, int]) -> str:
    """When no slot-instance has an owner_village_id we recognise, route to the
    closest owner village. Keeps the DEAD list owned even for orphan targets.
    """
    if not village_positions:
        return ""

    def euclid(vp: VillagePosition) -> float:
        dx = abs(vp.x - coord[0])
        dy = abs(vp.y - coord[1])
        if dx > 200:
            dx = 401 - dx
        if dy > 200:
            dy = 401 - dy
        return math.sqrt(dx * dx + dy * dy)

    return min(village_positions, key=euclid).name
