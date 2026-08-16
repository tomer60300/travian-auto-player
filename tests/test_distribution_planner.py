"""Rounding, the daily beat, and end-to-end orchestration.

Same approach as the optimizer tests: properties that must hold for any account,
checked across a range of sizes, because the real account grows and every figure
changes between runs.
"""

import random

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import Route, VillageState
from travian_api.services.distribution.planner import PlannerConfig, SheetRow, craft_plan
from travian_api.services.distribution.rounding import round_preserving_total
from travian_api.services.distribution.schedule import MINUTES_PER_DAY, build_beat

CONFIG = PlannerConfig(
    geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
    merchant_model=EUROPE2_TEUTON,
)


class TestCollectThenShip:
    """A relay hub must forward cargo it has already collected.

    This was moot until crop relay existed: netting left every village a sender
    or a receiver of a resource but never both, so nothing relayed and no
    ordering constraint could arise. A hub whose outbound fires just before its
    inbound lands ships from its own granary and waits nearly a whole cycle for
    the replacement, which is exactly the buffer the relay was meant to avoid
    needing.
    """

    def _leg(self, origin: int, destination: int, one_way: float, cycle: int = 4) -> Route:
        return Route(
            origin=origin,
            destination=destination,
            cargo_per_hour={Resource.CROP: 3000.0},
            cycle_hours=cycle,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=one_way,
        )

    def test_a_relay_hub_ships_soon_after_it_collects(self):
        # 1 -> 2 -> 3: village 2 forwards what village 1 sends it.
        inbound = self._leg(1, 2, one_way=30.0)
        outbound = self._leg(2, 3, one_way=45.0)

        beat = build_beat((inbound, outbound), min_arrival_gap_minutes=3)

        placed = {s.route.origin: s for s in beat.routes}
        arrivals = placed[1].arrival_minutes  # when cargo lands at the hub
        dispatches = placed[2].dispatch_minutes  # when the hub forwards it

        # Worst wait between collecting and shipping, measured round the clock.
        worst = max(min((d - a) % MINUTES_PER_DAY for a in arrivals) for d in dispatches)
        cycle_minutes = outbound.cycle_hours * 60
        assert worst < cycle_minutes, (
            f"hub waits {worst} min after collecting before it forwards, which is "
            f"a whole {cycle_minutes} min cycle -- it is shipping stale stock"
        )

    def test_ordinary_routes_are_scheduled_exactly_as_before(self):
        """Nothing relays here, so the collect-then-ship term must be inert: two
        legs that merely share a destination are not a relay."""
        a = self._leg(1, 3, one_way=30.0)
        b = self._leg(2, 3, one_way=45.0)

        beat = build_beat((a, b), min_arrival_gap_minutes=3)

        assert beat.warnings == ()
        # Both still get the widest-spacing treatment across the shared receiver.
        first, second = (s.arrival_minutes[0] for s in beat.routes)
        raw = abs(first - second)
        assert min(raw, MINUTES_PER_DAY - raw) >= 3


class TestReservedWindow:
    def _route(self) -> Route:
        return Route(
            origin=1,
            destination=2,
            cargo_per_hour={Resource.LUMBER: 1000.0},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=30.0,
        )

    def test_arrivals_avoid_the_reserved_npc_burst(self):
        """schedule.py has honoured reserved_window all along; until now the
        route never passed one, so the NPC slot could not actually be kept."""
        window = (120, 300)  # 02:00-05:00

        beat = build_beat((self._route(),), reserved_window=window)

        for scheduled in beat.routes:
            for minute in scheduled.arrival_minutes:
                assert not (window[0] <= minute < window[1]), (
                    f"arrival at {minute // 60:02d}:{minute % 60:02d} lands inside "
                    f"the reserved window {window}"
                )
        assert beat.warnings == ()


class TestBeatSpacing:
    def _route(self, origin: int) -> Route:
        return Route(
            origin=origin,
            destination=99,
            cargo_per_hour={Resource.LUMBER: 1000.0},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=30.0,
        )

    def test_sheet_reports_per_send_merchants_not_total_commitment(self):
        """A multi-set route commits merchants_per_send x sets, but the sheet
        row describes ONE Gold Club route definition; reporting the total
        overstates the route and conflicts with the budget section."""
        villages = {
            1: VillageState(village_id=1, x=0, y=0, merchant_count=20, trade_office_level=0),
            2: VillageState(village_id=2, x=60, y=0, merchant_count=20, trade_office_level=0),
        }
        productions = {Resource.LUMBER: {1: 1000.0, 2: 0.0}}
        allocations = {
            Resource.LUMBER: {
                1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                2: Allocation(AllocationMode.REMAINDER),
            }
        }

        plan = craft_plan(villages, productions, allocations, CONFIG)

        assert len(plan.rows) == 1
        route = plan.routing.routes[0]
        assert route.sets_in_flight > 1, "60 fields must need several sets in flight"
        assert plan.rows[0].merchants == route.merchants_per_send

    def test_a_route_outpacing_the_gap_warns_about_its_own_arrivals(self):
        """A 1h cycle lands hourly; no dispatch offset can space its own
        arrivals to a 90 min target, and staying silent hides the violation
        exactly when one busy inbound route causes it alone."""
        beat = build_beat(
            (
                Route(
                    origin=1,
                    destination=99,
                    cargo_per_hour={Resource.LUMBER: 100.0},
                    cycle_hours=1,
                    merchants_per_send=1,
                    sets_in_flight=1,
                    one_way_minutes=30.0,
                ),
            ),
            min_arrival_gap_minutes=90,
        )

        assert any("its own" in w for w in beat.warnings)

    def test_the_sweep_keeps_the_widest_spacing_not_the_first_legal_one(self):
        """Two daily routes into one village have 720 minutes of room; stopping
        at the first offset that merely clears the minimum gap crowds arrivals
        for no reason and contradicts the stated widest-spacing preference."""
        beat = build_beat((self._route(1), self._route(2)), min_arrival_gap_minutes=3)

        first, second = (s.arrival_minutes[0] for s in beat.routes)
        raw = abs(first - second)
        gap = min(raw, MINUTES_PER_DAY - raw)
        assert gap >= 360
        assert beat.warnings == ()


ACCOUNT_SIZES = [1, 2, 3, 6, 12, 22, 23, 35]


def make_inputs(village_count: int, seed: int):
    """An arbitrary account: villages, production, and allocations."""
    rng = random.Random(seed)
    villages = {
        vid: VillageState(
            village_id=vid,
            x=rng.randint(-60, 60),
            y=rng.randint(-60, 60),
            merchant_count=rng.choice([2, 13, 19, 20]),
            trade_office_level=rng.randint(0, 20),
        )
        for vid in range(1, village_count + 1)
    }
    ids = sorted(villages)
    productions, allocations = {}, {}
    for resource in Resource:
        productions[resource] = {vid: float(rng.randint(300, 9000)) for vid in ids}
        remainder = ids[rng.randrange(len(ids))]
        per_village = {remainder: Allocation(AllocationMode.REMAINDER)}
        for vid in ids:
            if vid != remainder and rng.random() < 0.5:
                per_village[vid] = Allocation(AllocationMode.ABSOLUTE, float(rng.randint(0, 9000)))
        allocations[resource] = per_village
    return villages, productions, allocations


class TestSumPreservingRounding:
    def test_total_survives_rounding(self):
        """Profile section 12: rounding must not create or destroy resources."""
        values = {Resource.LUMBER: 10.4, Resource.CLAY: 10.4, Resource.IRON: 10.4}

        rounded = round_preserving_total(values)

        assert sum(rounded.values()) == round(sum(values.values()))  # 31, not 30

    @pytest.mark.parametrize("seed", range(25))
    def test_total_survives_for_arbitrary_values(self, seed):
        rng = random.Random(seed)
        values = {f"r{i}": rng.uniform(0, 5000) for i in range(rng.randint(1, 8))}

        rounded = round_preserving_total(values)

        assert sum(rounded.values()) == round(sum(values.values()))

    @pytest.mark.parametrize("seed", range(25))
    def test_each_entry_stays_within_one_of_its_exact_value(self, seed):
        rng = random.Random(seed + 500)
        values = {f"r{i}": rng.uniform(0, 5000) for i in range(rng.randint(1, 8))}

        rounded = round_preserving_total(values)

        for key, value in values.items():
            assert abs(rounded[key] - value) < 1.0

    def test_exact_integers_are_untouched(self):
        values = {"a": 100.0, "b": 250.0}

        assert round_preserving_total(values) == {"a": 100, "b": 250}

    def test_empty_input_is_empty_output(self):
        assert round_preserving_total({}) == {}

    def test_negative_cargo_is_rejected(self):
        with pytest.raises(ValueError):
            round_preserving_total({"a": -1.0})

    def test_sheet_cargo_matches_the_merchant_budget_ceiling(self):
        """route_cost budgets merchants for ceil(batch); rounding the sheet to
        round(sum) under-delivers every cycle when the fraction is below .5,
        even though merchants were reserved for the higher batch."""
        values = {"lumber": 1.1}

        rounded = round_preserving_total(values, target_total=2)

        assert sum(rounded.values()) == 2

    @pytest.mark.parametrize("seed", range(25))
    def test_ceiling_target_keeps_each_entry_within_one(self, seed):
        import math

        rng = random.Random(seed + 900)
        values = {f"r{i}": rng.uniform(0, 5000) for i in range(rng.randint(1, 8))}
        target = math.ceil(sum(values.values()))

        rounded = round_preserving_total(values, target_total=target)

        assert sum(rounded.values()) == target
        for key, value in values.items():
            assert abs(rounded[key] - value) < 1.0

    def test_rounding_is_deterministic(self):
        values = {"a": 1.5, "b": 1.5, "c": 1.5}

        assert round_preserving_total(values) == round_preserving_total(values)


class TestBeat:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_every_route_is_scheduled(self, village_count):
        villages, productions, allocations = make_inputs(village_count, seed=village_count)
        plan = craft_plan(villages, productions, allocations, CONFIG)

        assert len(plan.beat.routes) == len(plan.routing.routes)
        assert len(plan.rows) == len(plan.routing.routes)

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_dispatch_falls_inside_the_first_cycle(self, village_count):
        """The slot repeats every cycle, so the offset must be within one."""
        villages, productions, allocations = make_inputs(village_count, seed=village_count + 5)
        plan = craft_plan(villages, productions, allocations, CONFIG)

        for scheduled in plan.beat.routes:
            assert 0 <= scheduled.dispatch_minute < scheduled.route.cycle_hours * 60

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_firing_count_matches_the_cycle(self, village_count):
        """A 3h cycle fires 8 times a day -- which a 60-minute table could not
        express at all (review R5)."""
        villages, productions, allocations = make_inputs(village_count, seed=village_count + 9)
        plan = craft_plan(villages, productions, allocations, CONFIG)

        for scheduled in plan.beat.routes:
            assert len(scheduled.dispatch_minutes) == 24 // scheduled.route.cycle_hours
            assert len(scheduled.arrival_minutes) == len(scheduled.dispatch_minutes)

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_all_minutes_are_within_the_day(self, village_count):
        villages, productions, allocations = make_inputs(village_count, seed=village_count + 13)
        plan = craft_plan(villages, productions, allocations, CONFIG)

        for scheduled in plan.beat.routes:
            for minute in scheduled.dispatch_minutes + scheduled.arrival_minutes:
                assert 0 <= minute < MINUTES_PER_DAY

    def test_arrivals_at_one_village_are_spaced_when_there_is_room(self):
        """Two inbound routes on a quiet village must not land together."""
        villages, productions, allocations = make_inputs(6, seed=77)
        plan = craft_plan(villages, productions, allocations, CONFIG)

        for vid in villages:
            arrivals = [minute for minute, _ in plan.beat.arrivals_at(vid)]
            if len(arrivals) < 2:
                continue
            # Only assert where the day is not saturated by this village's own
            # inbound firings; a busy hub legitimately cannot space everything.
            if len(arrivals) <= 8:
                assert len(set(arrivals)) == len(arrivals), vid

    def test_a_reserved_window_is_avoided_when_possible(self):
        """The manual NPC burst needs a clear slot."""
        villages, productions, allocations = make_inputs(4, seed=91)
        config = PlannerConfig(
            geometry=CONFIG.geometry,
            merchant_model=CONFIG.merchant_model,
            reserved_window=(600, 660),
        )

        plan = craft_plan(villages, productions, allocations, config)
        landed_in_window = [
            minute
            for scheduled in plan.beat.routes
            for minute in scheduled.arrival_minutes
            if 600 <= minute < 660
        ]

        # Any unavoidable collision must be declared, not silent.
        if landed_in_window:
            assert any("reserved window" in w for w in plan.beat.warnings)

    def test_scheduling_is_deterministic(self):
        villages, productions, allocations = make_inputs(12, seed=101)

        first = craft_plan(villages, productions, allocations, CONFIG)
        second = craft_plan(villages, productions, allocations, CONFIG)

        assert first.rows == second.rows

    def test_route_order_does_not_change_the_beat(self):
        villages, productions, allocations = make_inputs(8, seed=113)
        plan = craft_plan(villages, productions, allocations, CONFIG)

        reversed_beat = build_beat(tuple(reversed(plan.routing.routes)))

        assert {
            (s.route.origin, s.route.destination, s.dispatch_minute) for s in plan.beat.routes
        } == {
            (s.route.origin, s.route.destination, s.dispatch_minute) for s in reversed_beat.routes
        }

    def test_invalid_settings_are_rejected(self):
        with pytest.raises(ValueError):
            build_beat((), min_arrival_gap_minutes=-1)
        with pytest.raises(ValueError):
            build_beat((), step_minutes=0)


class TestEndToEnd:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_produces_a_sheet_for_any_account_size(self, village_count):
        villages, productions, allocations = make_inputs(village_count, seed=village_count + 17)

        plan = craft_plan(villages, productions, allocations, CONFIG)

        assert set(plan.merchants_committed) == set(villages)
        assert set(plan.spare_merchants) == set(villages)
        for row in plan.rows:
            assert row.total_cargo == sum(row.cargo.values())
            assert row.merchants > 0

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_cargo_totals_survive_the_round_trip_to_integers(self, village_count):
        villages, productions, allocations = make_inputs(village_count, seed=village_count + 23)

        plan = craft_plan(villages, productions, allocations, CONFIG)

        for scheduled, row in zip(plan.beat.routes, plan.rows, strict=True):
            exact = sum(scheduled.route.batch_per_resource.values())
            assert row.total_cargo == round(exact)

    def test_an_empty_account_yields_an_empty_sheet(self):
        plan = craft_plan({}, {}, {}, CONFIG)

        assert plan.rows == ()
        assert plan.is_feasible
        assert plan.total_merchants == 0

    def test_a_new_village_needs_no_reconfiguration(self):
        """Village 23 arrives; the plan absorbs it without re-entry."""
        villages, productions, allocations = make_inputs(22, seed=31)
        before = craft_plan(villages, productions, allocations, CONFIG)

        villages[23] = VillageState(23, 15, 15, merchant_count=20)
        after = craft_plan(villages, productions, allocations, CONFIG)

        assert after.rows == before.rows
        assert after.merchants_committed[23] == 0
        assert after.free_merchants(23) == 18

    def test_free_merchants_accounts_for_the_reserve(self):
        villages = {
            1: VillageState(1, 0, 0, merchant_count=20, trade_office_level=13),
            2: VillageState(2, 12, 0, merchant_count=20, trade_office_level=13),
        }
        productions = {Resource.IRON: {1: 1000.0, 2: 0.0}}
        allocations = {
            Resource.IRON: {
                1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                2: Allocation(AllocationMode.REMAINDER),
            }
        }

        plan = craft_plan(villages, productions, allocations, CONFIG)

        assert plan.spare_merchants[1] == 18
        assert plan.free_merchants(1) == 18 - plan.merchants_committed[1]
        assert plan.merchants_committed[2] == 0

    def test_sheet_rows_render_clock_times_for_the_operator(self):
        """The sheet is copied into the game by hand, so minutes need faces."""
        row = SheetRow(
            origin=1,
            destination=2,
            cargo={Resource.IRON: 100},
            cycle_hours=3,
            dispatch_minute=125,
            arrival_minute=605,
            merchants=4,
        )

        assert row.dispatch_clock() == "02:05"
        assert row.arrival_clock() == "10:05"
        assert row.total_cargo == 100

    def test_allocation_warnings_reach_the_plan(self):
        """A warning raised three layers down must not be swallowed."""
        villages = {1: VillageState(1, 0, 0, merchant_count=20)}
        productions = {Resource.LUMBER: {1: 1000.0}}
        allocations = {Resource.LUMBER: {1: Allocation(AllocationMode.ABSOLUTE, 400.0)}}

        plan = craft_plan(villages, productions, allocations, CONFIG)

        assert any("unallocated" in w for w in plan.warnings)
