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

    In v5.0 wave-stacking, a single target can produce multiple Placement
    objects — one per wave. ``wave_index`` (1-based) and ``of_total_waves``
    identify the wave's position in the plan; ``arrival_min`` is the one-way
    travel time for this specific wave; ``expected_haul`` is the per-wave
    haul (residual cranny + refill, capped at the empirical avg_loot).
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
    # Wave-stacking fields (v5.0). For single-placement back-compat callers,
    # wave_index=1, of_total_waves=1, arrival_min=round_trip_min/2.
    wave_index: int = 1
    of_total_waves: int = 1
    arrival_min: float = 0.0
    expected_haul: int = 0


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


def _field_distance(vp: VillagePosition, coord: tuple[int, int]) -> float:
    """Wrap-around Euclidean field distance from a village to a target."""
    dx = abs(vp.x - coord[0])
    dy = abs(vp.y - coord[1])
    if dx > 200:
        dx = 401 - dx
    if dy > 200:
        dy = 401 - dy
    return math.sqrt(dx * dx + dy * dy)


# ---------------------------------------------------------------------------
# Wave-stacking (v5.0)
# ---------------------------------------------------------------------------

# Minimum minutes between consecutive wave arrivals at the target. Cranny
# refills slowly; without this gap, wave 2 lands on an empty cranny and
# returns the carry-overhead of a wasted trip.
WAVE_SPACING_MIN = 15

# Average cranny refill rate across all resources combined (res/hour). Mirrors
# the orchestrator's DEFAULT_FARM_REFILL_PER_HOUR (v3.1 cranny model). Used
# inside size_wave_with_residual_carry to estimate haul of waves N≥2.
DEFAULT_REFILL_PER_HOUR = 60.0


def adaptive_wave_count(avg_loot: float) -> int:
    """Wave count per target based on its empirical average loot per raid.

    ≥200 res → 4 waves; 100-200 → 3; 50-100 → 2; 30-50 → 1; below 30 → 0
    (caller routes to DEAD via the economic floor).
    """
    if avg_loot >= 200:
        return 4
    if avg_loot >= 100:
        return 3
    if avg_loot >= 50:
        return 2
    if avg_loot >= 30:
        return 1
    return 0


def enumerate_candidate_waves(
    target_coord: tuple[int, int],
    village_positions: list[VillagePosition],
    troop_supplies: dict[tuple[str, str], int],
) -> list[tuple[str, str, float]]:
    """Return every feasible (village, unit, arrival_min) for hitting one target.

    Filters: village must have ≥1 of the unit in stock. No scoring; spacing is
    enforced downstream by pick_wave_set_greedy.
    """
    candidates: list[tuple[str, str, float]] = []
    for vp in village_positions:
        for unit in STRIKE_UNIT_CANDIDATES:
            supply = int(troop_supplies.get((vp.name, unit), 0))
            if supply < 1:
                continue
            speed = UNIT_SPEED.get(unit, 0)
            if speed <= 0:
                continue
            distance_fields = _field_distance(vp, target_coord)
            arrival_min = (distance_fields / speed) * 60.0
            candidates.append((vp.name, unit, arrival_min))
    return candidates


def pick_wave_set_greedy(
    candidates: list[tuple[str, str, float]],
    wanted_waves: int,
    *,
    spacing_min: float = WAVE_SPACING_MIN,
) -> list[tuple[str, str, float]]:
    """Greedy ≥spacing_min selector. Sort by arrival ASC, pick fastest, then
    walk forward picking the next candidate whose arrival is ≥last+spacing.
    Stops at wanted_waves. No two picks share a (village, unit) pair — the
    spacing constraint enforces that naturally.
    """
    if wanted_waves <= 0 or not candidates:
        return []
    sorted_cand = sorted(candidates, key=lambda c: c[2])
    picked: list[tuple[str, str, float]] = [sorted_cand[0]]
    seen_vu: set[tuple[str, str]] = {(sorted_cand[0][0], sorted_cand[0][1])}
    for cand in sorted_cand[1:]:
        if len(picked) >= wanted_waves:
            break
        if cand[2] - picked[-1][2] >= spacing_min and (cand[0], cand[1]) not in seen_vu:
            picked.append(cand)
            seen_vu.add((cand[0], cand[1]))
    return picked


def size_wave_with_residual_carry(
    avg_loot: float,
    wave_index: int,
    cumulative_carry_taken: int,
    unit_carry: int,
    *,
    refill_per_hour: float = DEFAULT_REFILL_PER_HOUR,
    interval_min: float = WAVE_SPACING_MIN,
) -> tuple[int, int]:
    """Return (unit_count, expected_haul_int) for one wave.

    Wave 0 (the first arrival) hauls min(empirical_cranny, count*carry).
    Subsequent waves haul residual_after_prior + refill_during_interval,
    capped at the empirical avg_loot. Count is sized to cover the expected
    haul with a 10% carry-safety margin, never below 1.
    """
    if wave_index <= 0:
        expected = avg_loot
    else:
        residual = max(0.0, avg_loot - float(cumulative_carry_taken))
        refilled = (interval_min / 60.0) * refill_per_hour
        expected = min(avg_loot, residual + refilled)
    if unit_carry <= 0:
        return 0, 0
    count = max(1, math.ceil(expected * CARRY_SAFETY / unit_carry))
    actual_haul = int(min(count * unit_carry, expected))
    return count, actual_haul


def plan_waves_for_target(
    target_agg: Any,
    village_positions: list[VillagePosition],
    troop_supplies: dict[tuple[str, str], int],
) -> list[Placement]:
    """Build the wave plan for one target. Returns one Placement per wave.

    Side effect: decrements troop_supplies for each wave committed. Caller is
    responsible for processing targets in value-descending order so the
    highest-value targets claim supply first.

    If wave count is 0 (avg_loot below DEAD_AVG_LOOT_THRESHOLD), returns an
    empty list — caller routes to DEAD.
    """
    avg_loot = float(target_agg.avg_loot or 0.0)
    wanted = adaptive_wave_count(avg_loot)
    if wanted == 0:
        return []

    candidates = sorted(
        enumerate_candidate_waves(target_agg.coord, village_positions, troop_supplies),
        key=lambda c: c[2],
    )
    if not candidates:
        return []

    plan: list[Placement] = []
    cumulative_taken = 0
    of_total = min(wanted, len(candidates))
    target_name = getattr(target_agg, "target_name", "")
    primary_owner = getattr(target_agg, "primary_owner_village", "")
    slot_instances = list(getattr(target_agg, "slot_instances", []) or [])

    # Selection and supply-checking run together over ALL candidates rather
    # than a greedy pre-slice: a candidate can enumerate with supply >= 1 yet
    # lack the units its wave needs, and pre-slicing would have discarded the
    # slower candidates (including ones inside the skipped pick's spacing
    # window) that could still fill the wave.
    seen_vu: set[tuple[str, str]] = set()
    last_arrival: float | None = None
    for village, unit, arrival_min in candidates:
        if len(plan) >= wanted:
            break
        if (village, unit) in seen_vu:
            continue
        if last_arrival is not None and arrival_min - last_arrival < WAVE_SPACING_MIN:
            continue
        carry = UNIT_CARRY.get(unit, 0)
        if carry <= 0:
            continue
        count, haul = size_wave_with_residual_carry(
            avg_loot,
            wave_index=len(plan),
            cumulative_carry_taken=cumulative_taken,
            unit_carry=carry,
        )
        avail = int(troop_supplies.get((village, unit), 0))
        if count > avail:
            continue
        seen_vu.add((village, unit))
        last_arrival = arrival_min
        round_trip = arrival_min * 2.0
        raids_per_day = compute_expected_raids_per_day(round_trip)
        plan.append(
            Placement(
                target_coord=target_agg.coord,
                optimal_village=village,
                optimal_unit=unit,
                optimal_count=count,
                round_trip_min=round_trip,
                expected_raids_per_day=raids_per_day,
                expected_daily_booty=raids_per_day * haul,
                target_name=target_name,
                primary_owner_village=primary_owner,
                slot_instances=slot_instances,
                wave_index=len(plan) + 1,  # 1-based over committed waves
                of_total_waves=of_total,
                arrival_min=arrival_min,
                expected_haul=haul,
            )
        )
        troop_supplies[(village, unit)] = avail - count
        cumulative_taken += haul

    # Truncation (or a zero-carry skip) can leave fewer waves than picked;
    # advertise the waves that exist, not the ones that were merely wanted.
    if len(plan) != of_total:
        for placement in plan:
            placement.of_total_waves = len(plan)
    return plan


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
    # Floored cuts would leave 1-3 placement villages with no HIGH list at all
    # (a single placement landed straight in INACTIVE), so every non-empty set
    # promotes at least its best target.
    high_cut = max(1, int(n * HIGH_FRACTION)) if n else 0
    mid_cut = max(high_cut, int(n * MID_FRACTION))
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
    """Run the v5.0 wave-stacking rebalance pass over a target inventory.

    For each target processed in avg_loot DESCENDING order so high-value
    targets claim supply first:
      1. is_dead_farm → DeadDecision into V{n}-DEAD for the primary owner.
      2. Otherwise plan_waves_for_target builds an ordered wave plan: 1-4
         (village, unit, count) waves with ≥WAVE_SPACING_MIN spacing at the
         target. Each wave decrements ``troop_supplies`` so the next target
         sees the remaining pool.
      3. If the wave plan is empty (no feasible waves; usually because every
         village has 0 supply for every unit), the target lands in DEAD with
         reason ``no_feasible_placement`` (counted in ``unplaceable_as_dead``).
      4. After every target is planned, each (village, target) bucket is
         ranked by total expected_haul across that target's waves and
         assigned HIGH/MID/INACTIVE roles. The same target can have HIGH role
         in V3 and MID role in V1 — list names are per-village-per-wave.
    """
    placements: list[Placement] = []
    dead_decisions: list[DeadDecision] = []
    unplaceable_as_dead = 0

    # Value-descending iteration: top targets get first claim of supply.
    sorted_targets = sorted(
        target_inventory.items(),
        key=lambda item: -float(item[1].avg_loot or 0.0),
    )
    for coord, target_agg in sorted_targets:
        owner = (
            target_agg.primary_owner_village
            if target_agg.primary_owner_village
            else _fallback_owner(village_positions, coord)
        )
        dead, reason = is_dead_farm(target_agg, now_unix=now_unix)
        if dead:
            dead_decisions.append(_build_dead_decision(target_agg, owner, reason, now_unix))
            continue

        wave_plan = plan_waves_for_target(target_agg, village_positions, troop_supplies)
        if not wave_plan:
            unplaceable_as_dead += 1
            dead_decisions.append(
                _build_dead_decision(target_agg, owner, "no_feasible_placement", now_unix)
            )
            continue
        placements.extend(wave_plan)

    # Role assignment. v5.0 ranks per-(village, target) by total expected_haul
    # of that target's waves; the same target_coord may earn HIGH in one
    # village's bucket and MID in another's. To keep all of a target's waves
    # in the same village under the same role we collapse to one ranking
    # entry per (village, target) and apply the role back to every wave.
    by_village_target: dict[str, dict[tuple[int, int], list[Placement]]] = {}
    for p in placements:
        by_village_target.setdefault(p.optimal_village, {}).setdefault(p.target_coord, []).append(p)

    ordered_placements: list[Placement] = []
    for village, by_target in by_village_target.items():
        # Build one "representative" Placement per target for ranking, with
        # objective_score = sum of expected_haul across that target's waves.
        per_target_rep: list[Placement] = []
        rep_to_waves: dict[int, list[Placement]] = {}
        for coord, waves in by_target.items():
            total_haul = float(sum(w.expected_haul for w in waves))
            rep = waves[0]
            rep.objective_score = total_haul
            per_target_rep.append(rep)
            rep_to_waves[id(rep)] = waves
        assign_role_and_list_name(village, per_target_rep)
        # Propagate the role + list_name back to every wave for this (village, target).
        # The list's unit suffix comes from each wave's own unit (different
        # waves of the same target can use different units, hence different
        # list names within the same role).
        for rep in per_target_rep:
            role = rep.target_list_role
            for w in rep_to_waves[id(rep)]:
                w.target_list_role = role
                unit_display = UNIT_DISPLAY_NAME.get(w.optimal_unit, w.optimal_unit)
                w.target_list_name = f"{village}-{role}-{unit_display}"
                ordered_placements.append(w)

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


def _build_dead_decision(target_agg: Any, owner: str, reason: str, now_unix: float) -> DeadDecision:
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
