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
            _slot(
                1,
                (10, 20),
                owner_village_id=1,
                total_raids=10,
                total_booty=500,
                last_raid_time=1700000000,
                defense_proxy=10,
            ),
            _slot(
                2,
                (10, 20),
                owner_village_id=2,
                list_name="V2-MID",
                total_raids=25,
                total_booty=800,
                last_raid_time=1700001000,
                defense_proxy=200,
            ),
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
            ("V1", "t1"): 694,
            ("V1", "t6"): 92,
            ("V2", "t1"): 258,
            ("V2", "t5"): 110,
            ("V3", "t3"): 2250,
            ("V3", "t6"): 1600,
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

    def test_30_50_20_bucketing(self):
        # 10 placements with descending scores. v5.0 cuts: HIGH 30%, MID 50%, INACTIVE 20%.
        # high_cut = int(10*0.30) = 3, mid_cut = int(10*0.80) = 8.
        scores = [1.0 - i * 0.1 for i in range(10)]
        placements = [self._make_placement(s) for s in scores]
        ordered = assign_role_and_list_name("V1", placements)
        roles = [p.target_list_role for p in ordered]
        assert roles[:3] == ["HIGH", "HIGH", "HIGH"]
        assert all(r == "MID" for r in roles[3:8])
        assert all(r == "INACTIVE" for r in roles[8:])

    def test_two_placements_keep_the_runner_up_active(self):
        """Flooring the cut index (int(2*0.80)=1) dropped the 2nd of exactly two
        placements straight to INACTIVE. The bottom 20% of two targets rounds to
        zero deactivations, so the runner-up belongs in MID, not INACTIVE."""
        placements = [self._make_placement(1.0), self._make_placement(0.5)]
        ordered = assign_role_and_list_name("V1", placements)
        roles = [p.target_list_role for p in ordered]
        assert roles == ["HIGH", "MID"]

    def test_list_name_uses_display_unit_and_village(self):
        p = self._make_placement(1.0, village="V3")
        assign_role_and_list_name("V3", [p])
        # A village's single best target must land in HIGH, not INACTIVE.
        assert p.target_list_name == "V3-HIGH-Clubs"

    def test_small_placement_sets_always_get_a_high_list(self):
        """Floored fraction cuts left 1-3 placement villages with no HIGH list
        at all — a lone placement went straight to INACTIVE, deactivating the
        village's only raid target by construction."""
        for n in (1, 2, 3):
            placements = [self._make_placement(1.0 - i * 0.1) for i in range(n)]
            ordered = assign_role_and_list_name("V1", placements)
            assert ordered[0].target_list_role == "HIGH", f"n={n}"

    def test_v4_v5_v6_v7_use_standard_roles(self):
        # v5.0 removed the LOCAL role; V4-V7 use HIGH/MID/INACTIVE like V1-V3.
        scores = [1.0 - i * 0.1 for i in range(10)]
        for village in ("V4", "V5", "V6", "V7"):
            placements = [self._make_placement(s, village=village) for s in scores]
            ordered = assign_role_and_list_name(village, placements)
            roles = {p.target_list_role for p in ordered}
            assert roles == {"HIGH", "MID", "INACTIVE"}
            assert not any("LOCAL" in p.target_list_name for p in ordered)


# --- Dead-farm truth table (spec acceptance) -----------------------------


class TestIsDeadFarm:
    def test_low_loot_with_sample(self):
        t = FakeTarget(
            coord=(1, 1),
            avg_loot=12.0,
            total_raids_all_lists=15,
            last_raid_time_unix=int(NOW - 86400),
        )
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "avg_loot" in reason

    def test_stale_and_mediocre(self):
        t = FakeTarget(
            coord=(2, 2),
            avg_loot=40.0,
            total_raids_all_lists=2,
            last_raid_time_unix=int(NOW - 30 * 86400),
        )
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "last raid" in reason

    def test_defended(self):
        t = FakeTarget(
            coord=(3, 3),
            avg_loot=100.0,
            total_raids_all_lists=10,
            last_raid_time_unix=int(NOW - 86400),
            max_def_proxy=DEAD_DEF_PROXY_LIMIT + 100,
        )
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "def_proxy" in reason

    def test_ct2_ct3_suspect(self):
        t = FakeTarget(
            coord=(4, 4),
            avg_loot=100.0,
            total_raids_all_lists=10,
            last_raid_time_unix=int(NOW - 86400),
            any_ct2_ct3_flag=True,
        )
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is True
        assert "CT2" in reason

    def test_alive_target_returns_false(self):
        t = FakeTarget(
            coord=(5, 5),
            avg_loot=80.0,
            total_raids_all_lists=20,
            last_raid_time_unix=int(NOW - 3 * 86400),
        )
        dead, reason = is_dead_farm(t, now_unix=NOW)
        assert dead is False
        assert reason == ""


# --- Orchestrator end-to-end ---------------------------------------------


class TestPlanRebalance:
    def test_dead_and_live_targets_route_correctly(self):
        # v5.0: a live high-loot target produces MULTIPLE wave placements,
        # not exactly one. We only assert routing correctness — the wave
        # math has its own dedicated tests in TestWaveStacking below.
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
        # The live target produces at least one wave placement.
        assert len(plan.placements) >= 1
        assert all(p.target_coord == (31, 83) for p in plan.placements)
        # The dead target is routed to V1-DEAD (its primary owner).
        assert len(plan.dead_decisions) == 1
        assert plan.dead_decisions[0].target_coord == (16, 92)
        assert plan.dead_decisions[0].target_list_name == "V1-DEAD"

    def test_unplaceable_routes_to_dead(self):
        vps = [VillagePosition("V6", 33, 83)]
        # No supply → no feasible placement
        empty_supplies: dict[tuple[str, str], int] = {}
        inv = {
            (31, 83): FakeTarget(
                coord=(31, 83),
                avg_loot=200.0,
                total_raids_all_lists=1,
                last_raid_time_unix=int(NOW - 86400),
                primary_owner_village="V6",
            )
        }
        plan = plan_rebalance(inv, vps, empty_supplies, now_unix=NOW)
        assert plan.unplaceable_as_dead == 1
        assert len(plan.dead_decisions) == 1
        assert "no_feasible_placement" in plan.dead_decisions[0].reason


class TestWaveStacking:
    """v5.0 wave-stacking planner: adaptive_wave_count, pick_wave_set_greedy,
    size_wave_with_residual_carry, plan_waves_for_target."""

    def test_adaptive_wave_count_tiers(self):
        from travian_api.services.rebalance_planner import adaptive_wave_count

        assert adaptive_wave_count(500) == 4
        assert adaptive_wave_count(200) == 4
        assert adaptive_wave_count(199) == 3
        assert adaptive_wave_count(100) == 3
        assert adaptive_wave_count(99) == 2
        assert adaptive_wave_count(50) == 2
        assert adaptive_wave_count(49) == 1
        assert adaptive_wave_count(30) == 1
        assert adaptive_wave_count(29) == 0
        assert adaptive_wave_count(0) == 0

    def test_size_wave_residual_carry_threads_correctly(self):
        from travian_api.services.rebalance_planner import size_wave_with_residual_carry

        # Wave 0: empirical 200, unit carry 60 (Clubs); count = ceil(200*1.1/60) = 4, haul = min(240, 200) = 200.
        c0, h0 = size_wave_with_residual_carry(200, 0, 0, 60)
        assert c0 == 4
        assert h0 == 200
        # Wave 1: residual = max(0, 200 - 200) = 0; refill = 15/60 * 60 = 15; expected = 15.
        # count = ceil(15*1.1/60) = 1; haul = min(60, 15) = 15.
        c1, h1 = size_wave_with_residual_carry(200, 1, 200, 60)
        assert c1 == 1
        assert h1 == 15
        # Wave 2: residual = max(0, 200 - 215) = 0; refill = 15; expected = 15. count=1, haul=15.
        c2, h2 = size_wave_with_residual_carry(200, 2, 215, 60)
        assert c2 == 1
        assert h2 == 15

    def test_pick_wave_set_greedy_enforces_spacing(self):
        from travian_api.services.rebalance_planner import pick_wave_set_greedy

        # Candidates with arrivals [5, 8, 25, 30, 50]; spacing 15.
        # Greedy: pick 5, then next ≥20 → 25, then next ≥40 → 50.
        candidates = [
            ("V7", "t1", 5.0),
            ("V6", "t1", 8.0),
            ("V3", "t6", 25.0),
            ("V1", "t6", 30.0),
            ("V2", "t5", 50.0),
        ]
        picks = pick_wave_set_greedy(candidates, wanted_waves=4)
        arrivals = [p[2] for p in picks]
        assert arrivals == [5.0, 25.0, 50.0]
        # Each consecutive pair is ≥15 apart.
        for i in range(1, len(arrivals)):
            assert arrivals[i] - arrivals[i - 1] >= 15

    def test_plan_waves_for_target_311_83_flagship(self):
        # Operator's flagship target (31,83) with avg_loot ~480 → 4 waves wanted.
        # Provide all 4 LOCAL-ish villages with Clubs supply; V3 with TK supply.
        from travian_api.services.rebalance_planner import plan_waves_for_target

        vps = [
            VillagePosition("V1", 15, 91),
            VillagePosition("V2", 22, 88),
            VillagePosition("V3", 23, 88),
            VillagePosition("V4", 39, 87),
            VillagePosition("V6", 33, 83),
            VillagePosition("V7", 30, 82),
        ]
        supplies = {
            ("V1", "t1"): 689,
            ("V1", "t6"): 92,
            ("V2", "t1"): 258,
            ("V2", "t5"): 111,
            ("V3", "t3"): 2024,
            ("V3", "t6"): 2506,
            ("V4", "t1"): 100,
            ("V6", "t1"): 100,
            ("V7", "t1"): 100,
        }
        target = FakeTarget(
            coord=(31, 83),
            avg_loot=480.0,
            total_raids_all_lists=20,
            last_raid_time_unix=int(NOW - 86400),
            primary_owner_village="V3",
        )
        waves = plan_waves_for_target(target, vps, supplies)
        assert len(waves) >= 3, f"avg_loot=480 should produce 3+ waves, got {len(waves)}"
        # Spacing ≥15min between consecutive waves.
        for i in range(1, len(waves)):
            gap = waves[i].arrival_min - waves[i - 1].arrival_min
            assert gap >= 15, f"Wave {i + 1} arrives only {gap:.1f}min after wave {i}"
        # No two waves share the same (village, unit) pair.
        vu_pairs = {(w.optimal_village, w.optimal_unit) for w in waves}
        assert len(vu_pairs) == len(waves), "duplicate (village, unit) pair"
        # Wave-1 should be V7 (1.41 fields, fastest with Clubs at 7 f/h, but V7's t1 at 7 f/h
        # ties with V6; V7 wins on closer distance).
        assert waves[0].optimal_village == "V7", (
            f"V7 (30,82) is 1.41 fields from (31,83) — should win wave 1; got {waves[0].optimal_village}"
        )

    def test_plan_waves_supply_decrement(self):
        from travian_api.services.rebalance_planner import plan_waves_for_target

        vps = [VillagePosition("V7", 30, 82)]
        supplies = {("V7", "t1"): 100}
        target = FakeTarget(
            coord=(31, 83),
            avg_loot=300.0,
            total_raids_all_lists=10,
            last_raid_time_unix=int(NOW - 86400),
        )
        waves = plan_waves_for_target(target, vps, supplies)
        # Only one village so only one wave possible.
        assert len(waves) == 1
        # Supply was decremented.
        assert supplies[("V7", "t1")] == 100 - waves[0].optimal_count

    def test_truncated_plans_advertise_the_waves_that_exist(self, monkeypatch):
        """Supply can run out mid-plan; the surviving placements must not claim
        wave 1/2 when only one wave was actually planned."""
        import travian_api.services.rebalance_planner as rp

        real_sizer = rp.size_wave_with_residual_carry

        def demanding_second_wave(avg_loot, wave_index, cumulative_carry_taken, unit_carry, **kw):
            if wave_index >= 1:
                return 999_999, 100  # more troops than any village holds
            return real_sizer(avg_loot, wave_index, cumulative_carry_taken, unit_carry, **kw)

        monkeypatch.setattr(rp, "size_wave_with_residual_carry", demanding_second_wave)

        # V6 sits far enough that its arrival clears the 15-min wave spacing
        # and it survives into the greedy pick set as wave 2.
        vps = [VillagePosition("V7", 30, 82), VillagePosition("V6", 35, 83)]
        supplies = {("V7", "t1"): 1000, ("V6", "t1"): 100}
        target = FakeTarget(
            coord=(31, 83),
            avg_loot=480.0,
            total_raids_all_lists=20,
            last_raid_time_unix=int(NOW - 86400),
        )

        waves = rp.plan_waves_for_target(target, vps, supplies)

        assert waves, "the first wave must survive the truncation"
        for wave in waves:
            assert wave.of_total_waves == len(waves)

    def test_an_undersupplied_pick_falls_back_to_later_candidates(self):
        """The fastest candidate can have supply >= 1 (so it enumerates) but
        fewer units than the wave needs; truncating there dropped feasible
        placements from slower villages with plenty of troops."""
        from travian_api.services.rebalance_planner import plan_waves_for_target

        # V7 is fastest but has one clubswinger; V6 is farther with plenty.
        vps = [VillagePosition("V7", 30, 82), VillagePosition("V6", 35, 83)]
        supplies = {("V7", "t1"): 1, ("V6", "t1"): 1000}
        target = FakeTarget(
            coord=(31, 83),
            avg_loot=480.0,
            total_raids_all_lists=20,
            last_raid_time_unix=int(NOW - 86400),
        )

        waves = plan_waves_for_target(target, vps, supplies)

        assert waves, "the slower-but-supplied village must still get the wave"
        assert waves[0].optimal_village == "V6"
        assert waves[0].wave_index == 1
        assert all(w.of_total_waves == len(waves) for w in waves)

    def test_backfill_reaches_candidates_beyond_the_greedy_slice(self):
        """Greedy pre-selection excluded candidates inside the spacing window
        of an undersupplied pick; skipping that pick must reopen its window so
        slower candidates can fill the wave."""
        from travian_api.services.rebalance_planner import plan_waves_for_target

        # VA fastest (12min) but one club; VB (19min) sits inside VA's spacing
        # window so the old greedy slice never contained it; VC at 34min.
        vps = [
            VillagePosition("VA", 30, 82),
            VillagePosition("VB", 33, 84),
            VillagePosition("VC", 35, 83),
        ]
        supplies = {("VA", "t1"): 1, ("VB", "t1"): 1000, ("VC", "t1"): 1000}
        target = FakeTarget(
            coord=(31, 83),
            avg_loot=480.0,
            total_raids_all_lists=20,
            last_raid_time_unix=int(NOW - 86400),
        )

        waves = plan_waves_for_target(target, vps, supplies)

        assert [w.optimal_village for w in waves] == ["VB", "VC"]
        assert [w.wave_index for w in waves] == [1, 2]

    def test_dead_floor_returns_empty_plan(self):
        from travian_api.services.rebalance_planner import plan_waves_for_target

        vps = [VillagePosition("V7", 30, 82)]
        supplies = {("V7", "t1"): 100}
        target = FakeTarget(
            coord=(31, 83),
            avg_loot=20.0,
            total_raids_all_lists=10,
            last_raid_time_unix=int(NOW - 86400),
        )
        assert plan_waves_for_target(target, vps, supplies) == []
