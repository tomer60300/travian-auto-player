"""Tests for the Path 3 rebalance pass (v4.0).

Covers target_aggregate_service.build_target_inventory and the pure-math /
verdict surface of rebalance_planner — exactly the spec's acceptance criteria.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from travian_api.services.rebalance_planner import (
    CADENCE_DISTRIBUTION,
    DEAD_DEF_PROXY_LIMIT,
    Placement,
    VillagePosition,
    assign_role_and_list_name,
    compute_expected_raids_per_day,
    compute_placement_score,
    compute_round_trip_min,
    is_dead_farm,
    pick_best_placement,
    plan_rebalance,
)
from travian_api.services.target_aggregate_service import (
    build_target_inventory,
)

# --- Fakes -----------------------------------------------------------------


@dataclass
class FakeTarget:
    coord: tuple[int, int]
    avg_loot: float
    total_raids_all_lists: int = 0
    last_raid_time_unix: int | None = None
    max_def_proxy: int = 0
    any_ct2_ct3_flag: bool = False
    target_name: str = ""
    primary_owner_village: str = "V1"
    total_booty_all_lists: int = 0
    slot_instances: list[tuple[str, int]] = field(default_factory=list)


NOW = 1_700_000_000.0


def _slot(slot_id: int, coord: tuple[int, int], **overrides):
    base = {
        "slot_id": slot_id,
        "list_id": 1,
        "list_name": "V1-MID-Clubs01",
        "owner_village_id": 1,
        "coords": coord,
        "name": f"T{slot_id}",
        "total_raids": 0,
        "total_booty": 0,
        "last_raid_time": None,
        "defense_proxy": 0,
        "pushing_protection_suspect": False,
    }
    base.update(overrides)
    return base


# --- target_aggregate_service ---------------------------------------------


class TestBuildTargetInventory:
    def test_groups_by_coord_and_sums_stats(self):
        slot_ms = [
            _slot(1, (10, 20), owner_village_id=1, total_raids=10,
                  total_booty=500, last_raid_time=1700000000, defense_proxy=10),
            _slot(2, (10, 20), owner_village_id=2, list_name="V2-MID",
                  total_raids=25, total_booty=800, last_raid_time=1700001000,
                  defense_proxy=200),
        ]
        inv = build_target_inventory(slot_ms, {1: "V1", 2: "V2"})
        agg = inv[(10, 20)]
        assert agg.total_raids_all_lists == 35
        assert agg.total_booty_all_lists == 1300
        assert agg.avg_loot == pytest.approx(1300 / 35, abs=0.01)
        assert agg.primary_owner_village == "V2"  # 25 > 10 raids
        assert agg.last_raid_time_unix == 1700001000
        assert agg.max_def_proxy == 200
        assert agg.any_ct2_ct3_flag is False
        assert ("V1-MID-Clubs01", 1) in agg.slot_instances
        assert ("V2-MID", 2) in agg.slot_instances

    def test_ct2_ct3_flag_is_or_across_instances(self):
        slot_ms = [
            _slot(1, (1, 2), pushing_protection_suspect=False),
            _slot(2, (1, 2), pushing_protection_suspect=True),
        ]
        inv = build_target_inventory(slot_ms, {})
        assert inv[(1, 2)].any_ct2_ct3_flag is True

    def test_singleton_coord_produces_singleton_aggregate(self):
        slot_ms = [_slot(1, (5, 5), owner_village_id=1, total_raids=3, total_booty=90)]
        inv = build_target_inventory(slot_ms, {1: "V1"})
        agg = inv[(5, 5)]
        assert agg.total_raids_all_lists == 3
        assert agg.avg_loot == 30.0
        assert agg.primary_owner_village == "V1"

    def test_zero_raids_yields_zero_avg(self):
        slot_ms = [_slot(1, (7, 7), total_raids=0, total_booty=0)]
        inv = build_target_inventory(slot_ms, {})
        assert inv[(7, 7)].avg_loot == 0.0


# --- rebalance_planner math (spec acceptance) -----------------------------


class TestRoundTripMath:
    def test_v3_to_target_31_83_with_tk_is_125_8(self):
        v3 = VillagePosition("V3", 23, 88)
        rt = compute_round_trip_min(v3, (31, 83), "t6")
        assert rt == pytest.approx(125.8, abs=0.5)

    def test_v6_to_target_31_83_with_clubs_is_34_3(self):
        v6 = VillagePosition("V6", 33, 83)
        rt = compute_round_trip_min(v6, (31, 83), "t1")
        assert rt == pytest.approx(34.3, abs=0.5)

    def test_v1_to_target_31_83_with_clubs_is_306_7(self):
        v1 = VillagePosition("V1", 15, 91)
        rt = compute_round_trip_min(v1, (31, 83), "t1")
        assert rt == pytest.approx(306.7, abs=0.5)


class TestExpectedRaidsPerDay:
    def test_round_trip_30_yields_32_78(self):
        # bucket A: gap=max(30, 32.5)=32.5, weight 0.6, rate 44.31 → 26.59
        # bucket B: gap=82.5, rate 17.45 → 5.24
        # bucket C: gap=150,  rate 9.6   → 0.96
        # total ≈ 32.78
        rpd = compute_expected_raids_per_day(30.0)
        assert rpd == pytest.approx(32.78, abs=0.5)

    def test_distribution_weights_sum_to_one(self):
        total = sum(weight for _, weight in CADENCE_DISTRIBUTION)
        assert total == pytest.approx(1.0, abs=1e-9)

    def test_very_long_round_trip_drops_rate(self):
        # rt 600 min > every cadence midpoint, rate = 1440/600 = 2.4 regardless
        assert compute_expected_raids_per_day(600.0) == pytest.approx(2.4, abs=0.01)


# --- Placement scoring (operator's V6 vs V3 preference test) --------------


class TestPickBestPlacement:
    """The operator's flagship test: target (31, 83) under 80/20 frequency
    weighting picks V6 (close, fewer hauls) over V3 (far, more carry per hit).
    """

    def setup_method(self):
        self.target = FakeTarget(
            coord=(31, 83),
            avg_loot=200.0,
            total_raids_all_lists=10,
            total_booty_all_lists=2000,
            last_raid_time_unix=int(NOW - 86400),
        )
        self.vps = [
            VillagePosition("V1", 15, 91),
            VillagePosition("V2", 22, 88),
            VillagePosition("V3", 23, 88),
            VillagePosition("V4", 39, 87),
            VillagePosition("V6", 33, 83),
        ]
        self.supplies = {
            ("V1", "t1"): 694, ("V1", "t6"): 92,
            ("V2", "t1"): 258, ("V2", "t5"): 110,
            ("V3", "t3"): 2250, ("V3", "t6"): 1600,
            ("V4", "t1"): 100,
            ("V6", "t1"): 100,
        }

    def test_v6_wins_flagship_target_under_8020_weighting(self):
        best = pick_best_placement(self.target, self.vps, self.supplies)
        assert best is not None
        assert best.optimal_village == "V6", (
            f"V6 must win on short round-trip; got {best.optimal_village} "
            f"with score {best.objective_score:.3f}"
        )

    def test_score_is_normalised_to_unit_interval(self):
        best = pick_best_placement(self.target, self.vps, self.supplies)
        assert 0.0 <= best.objective_score <= 1.0

    def test_returns_none_when_no_supplies(self):
        empty: dict[tuple[str, str], int] = {}
        assert pick_best_placement(self.target, self.vps, empty) is None


# --- compute_placement_score edge cases ----------------------------------


class TestComputePlacementScore:
    def test_supply_drain_cap_drops_candidate(self):
        # Tiny supply forces count > 0.9*supply → None
        target = FakeTarget(coord=(31, 83), avg_loot=200.0)
        vp = VillagePosition("V6", 33, 83)
        # 200 * 1.10 / 60 = 3.67 → ceil = 4. Supply 4 -> 4 > 4*0.9 = 3.6 → drop.
        assert compute_placement_score(target, vp, "t1", supply=4) is None

    def test_zero_carry_unit_drops_candidate(self):
        # t4 scout has carry=0, should be rejected even with infinite supply
        target = FakeTarget(coord=(31, 83), avg_loot=200.0)
        vp = VillagePosition("V6", 33, 83)
        assert compute_placement_score(target, vp, "t4", supply=999) is None


# --- Role assignment ----------------------------------------------------


class TestAssignRoleAndListName:
    def _make_placement(self, score: float, village: str = "V1") -> Placement:
        return Placement(
            target_coord=(1, 1),
            optimal_village=village,
            optimal_unit="t1",
            optimal_count=1,
            round_trip_min=30.0,
            expected_raids_per_day=30.0,
            expected_daily_booty=300.0,
            objective_score=score,
        )

    def test_quartile_bucketing_high_mid_inactive(self):
        # 10 placements with descending scores. high_cut=2, mid_cut=7.
        # Use V1 (non-LOCAL) so quartile bucketing applies.
        scores = [1.0 - i * 0.1 for i in range(10)]
        placements = [self._make_placement(s) for s in scores]
        ordered = assign_role_and_list_name("V1", placements)
        roles = [p.target_list_role for p in ordered]
        assert roles[:2] == ["HIGH", "HIGH"]
        assert all(r == "MID" for r in roles[2:7])
        assert all(r == "INACTIVE" for r in roles[7:])

    def test_list_name_uses_display_unit_and_village(self):
        p = self._make_placement(1.0, village="V3")
        assign_role_and_list_name("V3", [p])
        # 1 placement, high_cut=0, mid_cut=0 → idx 0 >= mid_cut → INACTIVE
        assert p.target_list_name == "V3-INACTIVE-Clubs"

    def test_local_villages_always_get_local_role(self):
        # V4/V5/V6/V7 skip quartile bucketing entirely.
        scores = [1.0 - i * 0.1 for i in range(10)]
        placements = [self._make_placement(s, village="V6") for s in scores]
        ordered = assign_role_and_list_name("V6", placements)
        assert all(p.target_list_role == "LOCAL" for p in ordered)
        assert all(p.target_list_name == "V6-LOCAL-Clubs" for p in ordered)
        # Sort order is still score desc.
        assert [p.objective_score for p in ordered] == sorted(scores, reverse=True)


# --- Dead-farm truth table (spec acceptance) -----------------------------


class TestIsDeadFarm:
    def test_low_loot_with_sample(self):
        t = FakeTarget(coord=(1, 1), avg_loot=12.0, total_raids_all_lists=15,
                       last_raid_time_unix=int(NOW - 86400))
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "avg_loot" in reason

    def test_stale_and_mediocre(self):
        t = FakeTarget(coord=(2, 2), avg_loot=40.0, total_raids_all_lists=2,
                       last_raid_time_unix=int(NOW - 30 * 86400))
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "last raid" in reason

    def test_defended(self):
        t = FakeTarget(coord=(3, 3), avg_loot=100.0, total_raids_all_lists=10,
                       last_raid_time_unix=int(NOW - 86400),
                       max_def_proxy=DEAD_DEF_PROXY_LIMIT + 100)
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "def_proxy" in reason

    def test_ct2_ct3_suspect(self):
        t = FakeTarget(coord=(4, 4), avg_loot=100.0, total_raids_all_lists=10,
                       last_raid_time_unix=int(NOW - 86400),
                       any_ct2_ct3_flag=True)
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "CT2" in reason

    def test_alive_target_returns_false(self):
        t = FakeTarget(coord=(5, 5), avg_loot=80.0, total_raids_all_lists=20,
                       last_raid_time_unix=int(NOW - 3 * 86400))
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is False
        assert reason == ""


# --- Orchestrator end-to-end ---------------------------------------------


class TestPlanRebalance:
    def test_dead_and_live_targets_route_correctly(self):
        vps = [VillagePosition("V6", 33, 83), VillagePosition("V1", 15, 91)]
        supplies = {("V6", "t1"): 100, ("V1", "t1"): 200}
        inventory = {
            (31, 83): FakeTarget(
                coord=(31, 83),
                avg_loot=200.0,
                total_raids_all_lists=10,
                last_raid_time_unix=int(NOW - 86400),
                primary_owner_village="V6",
            ),
            (16, 92): FakeTarget(
                coord=(16, 92),
                avg_loot=12.0,
                total_raids_all_lists=15,
                last_raid_time_unix=int(NOW - 86400),
                primary_owner_village="V1",
            ),
        }
        plan = plan_rebalance(inventory, vps, supplies, now_unix=NOW)
        assert len(plan.placements) == 1
        assert plan.placements[0].target_coord == (31, 83)
        assert len(plan.dead_decisions) == 1
        assert plan.dead_decisions[0].target_coord == (16, 92)
        assert plan.dead_decisions[0].target_list_name == "V1-DEAD"

    def test_unplaceable_routes_to_dead(self):
        vps = [VillagePosition("V6", 33, 83)]
        # No supply → no feasible placement
        empty_supplies: dict[tuple[str, str], int] = {}
        inv = {
            (31, 83): FakeTarget(
                coord=(31, 83), avg_loot=200.0, total_raids_all_lists=1,
                last_raid_time_unix=int(NOW - 86400),
                primary_owner_village="V6",
            )
        }
        plan = plan_rebalance(inv, vps, empty_supplies, now_unix=NOW)
        assert plan.unplaceable_as_dead == 1
        assert len(plan.dead_decisions) == 1
        assert "no_feasible_placement" in plan.dead_decisions[0].reason


class TestLocalRoleReach:
    """V4/V5/V6/V7 are LOCAL micro-raiders. A target farther than
    LOCAL_MAX_DISTANCE_FIELDS (8) must NOT pick a LOCAL village even if it
    would otherwise win the objective.
    """

    def test_local_village_rejected_for_distant_target(self):
        # V6 at (33, 83); target at (3, 50) → distance ~44 fields, well >8.
        vps = [VillagePosition("V6", 33, 83), VillagePosition("V1", 15, 91)]
        supplies = {("V6", "t1"): 100, ("V1", "t1"): 200}
        target = FakeTarget(
            coord=(3, 50), avg_loot=200.0, total_raids_all_lists=5,
            last_raid_time_unix=int(NOW - 86400),
        )
        best = pick_best_placement(target, vps, supplies)
        assert best is not None
        assert best.optimal_village == "V1", (
            f"V6 must be filtered out by LOCAL reach; got {best.optimal_village}"
        )

    def test_local_village_kept_for_near_target(self):
        # V6 at (33, 83); target at (31, 83) → distance 2, within reach.
        vps = [VillagePosition("V6", 33, 83), VillagePosition("V1", 15, 91)]
        supplies = {("V6", "t1"): 100, ("V1", "t1"): 200}
        target = FakeTarget(
            coord=(31, 83), avg_loot=200.0, total_raids_all_lists=5,
            last_raid_time_unix=int(NOW - 86400),
        )
        best = pick_best_placement(target, vps, supplies)
        assert best is not None
        # V6 should win because round-trip is ~34min vs V1's ~300min.
        assert best.optimal_village == "V6"


class TestLocalOverflow:
    """When a LOCAL village wins more than LOCAL_MAX_SLOTS placements, the
    surplus is routed to that village's DEAD pool with reason
    'local_overflow_above_15_slots'.
    """

    def test_overflow_above_15_routes_to_dead(self):
        # Place V6 only, with 20 near targets all winning V6.
        vps = [VillagePosition("V6", 33, 83)]
        supplies = {("V6", "t1"): 2000}  # plenty of supply
        # 20 distinct coords all within 5 fields of V6.
        offsets = [(dx, dy) for dx in range(-2, 3) for dy in range(-2, 3) if (dx, dy) != (0, 0)]
        inventory = {}
        for i, (dx, dy) in enumerate(offsets[:20]):
            coord = (33 + dx, 83 + dy)
            inventory[coord] = FakeTarget(
                coord=coord,
                avg_loot=100.0 + i,  # distinct scores to force a clean ordering
                total_raids_all_lists=10,
                last_raid_time_unix=int(NOW - 86400),
                primary_owner_village="V6",
            )
        plan = plan_rebalance(inventory, vps, supplies, now_unix=NOW)
        assert len(plan.placements) == 15, (
            f"LOCAL list must cap at 15; got {len(plan.placements)}"
        )
        assert all(p.target_list_role == "LOCAL" for p in plan.placements)
        assert all(p.target_list_name == "V6-LOCAL-Clubs" for p in plan.placements)
        overflow_dead = [d for d in plan.dead_decisions if "local_overflow" in d.reason]
        assert len(overflow_dead) == 5, (
            f"5 overflow targets must land in DEAD; got {len(overflow_dead)}"
        )
        assert all(d.target_list_name == "V6-DEAD" for d in overflow_dead)
