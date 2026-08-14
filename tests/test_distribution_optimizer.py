"""Optimizer invariants, checked over randomly generated accounts.

The account this plans for is a moving target -- 22 villages today, 23 landing,
and every production figure different between runs -- so correctness is pinned
with properties that must hold for *any* shape of account rather than by
reproducing one snapshot. A golden fixture of a 20-village plan would be stale
the day it was written.
"""

import random

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
    resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON, route_cost
from travian_api.services.distribution.optimizer import VillageState, build_plan

GEOMETRY = MapGeometry(span=401, speed_fields_per_hour=12.0)
MODEL = EUROPE2_TEUTON


def make_account(village_count: int, seed: int) -> dict[int, VillageState]:
    """An arbitrary account of *village_count* villages."""
    rng = random.Random(seed)
    return {
        vid: VillageState(
            village_id=vid,
            x=rng.randint(-120, 120),
            y=rng.randint(-120, 120),
            merchant_count=rng.choice([2, 13, 19, 20]),
            trade_office_level=rng.randint(0, 20),
        )
        for vid in range(1, village_count + 1)
    }


def make_plans(
    villages: dict[int, VillageState], seed: int
) -> tuple[dict[Resource, object], dict[Resource, dict[int, float]]]:
    """Random production plus one remainder village per resource."""
    rng = random.Random(seed)
    plans = {}
    productions = {}
    ids = sorted(villages)
    for resource in Resource:
        production = {vid: float(rng.randint(200, 9000)) for vid in ids}
        remainder = ids[rng.randrange(len(ids))]
        allocations = {remainder: Allocation(AllocationMode.REMAINDER)}
        for vid in ids:
            if vid == remainder:
                continue
            if rng.random() < 0.5:
                allocations[vid] = Allocation(AllocationMode.ABSOLUTE, float(rng.randint(0, 9000)))
        plans[resource] = resolve_resource(resource, production, allocations)
        productions[resource] = production
    return plans, productions


ACCOUNT_SIZES = [1, 2, 3, 5, 12, 22, 23, 40]


class TestScalesToAnyAccountSize:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_produces_a_plan_without_error(self, village_count):
        villages = make_account(village_count, seed=village_count)
        plans, _ = make_plans(villages, seed=village_count)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        assert set(plan.merchants_committed) == set(villages)

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_routes_only_reference_known_villages(self, village_count):
        villages = make_account(village_count, seed=village_count + 100)
        plans, _ = make_plans(villages, seed=village_count + 100)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        for route in plan.routes:
            assert route.origin in villages
            assert route.destination in villages
            assert route.origin != route.destination

    def test_a_new_village_does_not_break_an_existing_plan(self):
        """Village 23 lands mid-server; the planner must absorb it."""
        villages = make_account(22, seed=7)
        plans, _ = make_plans(villages, seed=7)
        before = build_plan(villages, plans, GEOMETRY, MODEL)

        grown = dict(villages)
        grown[23] = VillageState(village_id=23, x=40, y=40, merchant_count=20)
        after = build_plan(grown, plans, GEOMETRY, MODEL)

        # The newcomer has no allocation yet, so it neither sends nor receives.
        assert after.merchants_committed[23] == 0
        assert before.routes == after.routes

    def test_an_empty_account_plans_nothing_rather_than_failing(self):
        plan = build_plan({}, {}, GEOMETRY, MODEL)

        assert plan.routes == ()
        assert plan.is_feasible


class TestStructuralInvariants:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_never_ships_one_resource_both_ways_between_a_pair(self, village_count):
        """Known issue #2. Impossible by construction -- netting in the
        allocation layer means a village cannot both send and receive the same
        resource -- so this asserts the property rather than a guard."""
        villages = make_account(village_count, seed=village_count + 3)
        plans, _ = make_plans(villages, seed=village_count + 3)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        carried: dict[tuple[int, int], set[Resource]] = {}
        for route in plan.routes:
            carried[(route.origin, route.destination)] = set(route.cargo_per_hour)
        for (origin, destination), resources in carried.items():
            back = carried.get((destination, origin), set())
            assert not (resources & back), f"{origin}<->{destination} both ways"

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_no_village_relays_the_same_resource(self, village_count):
        """The no-waterfall rule for W/C/I, and it holds for crop too."""
        villages = make_account(village_count, seed=village_count + 11)
        plans, _ = make_plans(villages, seed=village_count + 11)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        sends: dict[int, set[Resource]] = {}
        receives: dict[int, set[Resource]] = {}
        for route in plan.routes:
            sends.setdefault(route.origin, set()).update(route.cargo_per_hour)
            receives.setdefault(route.destination, set()).update(route.cargo_per_hour)
        for vid in villages:
            assert not (sends.get(vid, set()) & receives.get(vid, set()))

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_merchant_arithmetic_matches_the_cost_model(self, village_count):
        villages = make_account(village_count, seed=village_count + 21)
        plans, _ = make_plans(villages, seed=village_count + 21)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        for route in plan.routes:
            expected = route_cost(
                route.hourly_total,
                route.cycle_hours,
                2.0 * route.one_way_minutes,
                MODEL.capacity(villages[route.origin].trade_office_level),
            )
            assert route.merchants_per_send == expected.merchants_per_send
            assert route.sets_in_flight == expected.sets_in_flight

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_committed_merchants_equal_the_sum_of_the_routes(self, village_count):
        villages = make_account(village_count, seed=village_count + 31)
        plans, _ = make_plans(villages, seed=village_count + 31)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        per_origin: dict[int, int] = {vid: 0 for vid in villages}
        for route in plan.routes:
            per_origin[route.origin] += route.merchants_committed
        assert plan.merchants_committed == per_origin
        assert plan.total_merchants == sum(r.merchants_committed for r in plan.routes)


class TestBudgetIsReportedNotHidden:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_every_over_budget_village_is_reported(self, village_count):
        """Known issue #6: the cap must never be breached invisibly."""
        villages = make_account(village_count, seed=village_count + 41)
        plans, _ = make_plans(villages, seed=village_count + 41)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        reported = {o.village_id for o in plan.over_budget}
        for vid, used in plan.merchants_committed.items():
            if used > villages[vid].spare_merchants():
                assert vid in reported, f"village {vid} over budget but not reported"
        for over in plan.over_budget:
            assert over.excess > 0

    def test_reserve_is_withheld_from_the_budget(self):
        village = VillageState(village_id=1, x=0, y=0, merchant_count=20)

        assert village.spare_merchants(reserve=2) == 18
        assert village.spare_merchants(reserve=0) == 20

    def test_a_village_with_fewer_merchants_than_the_reserve_has_none_spare(self):
        """Real accounts have low-marketplace villages; 2 merchants is observed."""
        village = VillageState(village_id=1, x=0, y=0, merchant_count=2)

        assert village.spare_merchants(reserve=2) == 0


class TestDeterminism:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_replanning_unchanged_input_gives_an_identical_plan(self, village_count):
        """Known issue #10: without this the route diff is meaningless."""
        villages = make_account(village_count, seed=village_count + 51)
        plans, _ = make_plans(villages, seed=village_count + 51)

        first = build_plan(villages, plans, GEOMETRY, MODEL)
        second = build_plan(villages, plans, GEOMETRY, MODEL)

        assert first.routes == second.routes
        assert first.merchants_committed == second.merchants_committed

    def test_village_ordering_does_not_change_the_plan(self):
        villages = make_account(12, seed=61)
        plans, _ = make_plans(villages, seed=61)
        reversed_villages = dict(reversed(list(villages.items())))

        assert (
            build_plan(villages, plans, GEOMETRY, MODEL).routes
            == build_plan(reversed_villages, plans, GEOMETRY, MODEL).routes
        )


class TestFlowConservation:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_received_plus_shortfall_equals_demand(self, village_count):
        """Every receiver either gets what it asked for or the gap is recorded."""
        villages = make_account(village_count, seed=village_count + 71)
        plans, _ = make_plans(villages, seed=village_count + 71)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        for resource, resource_plan in plans.items():
            received: dict[int, float] = {}
            for route in plan.routes:
                if resource in route.cargo_per_hour:
                    received[route.destination] = (
                        received.get(route.destination, 0.0) + route.cargo_per_hour[resource]
                    )
            missing = {s.village_id: s.per_hour for s in plan.shortfalls if s.resource is resource}
            for village in resource_plan.receivers:
                got = received.get(village.village_id, 0.0)
                gap = missing.get(village.village_id, 0.0)
                assert got + gap == pytest.approx(village.ship_per_hour, rel=1e-6, abs=1e-6)

    def test_unmet_demand_is_reported_as_a_shortfall(self):
        """Reaches a branch the generated accounts cannot.

        With a remainder village every resource plan is conserved, so supply
        equals demand exactly and nothing can go unrouted. Only an unconserved
        plan -- an absolute target with no remainder to fund it -- produces a
        shortfall, and it must be reported rather than quietly under-shipped.
        """
        villages = {
            1: VillageState(1, 0, 0, merchant_count=20, trade_office_level=13),
            2: VillageState(2, 12, 0, merchant_count=20, trade_office_level=13),
        }
        plans = {
            Resource.IRON: resolve_resource(
                Resource.IRON,
                {1: 1000.0, 2: 0.0},
                {
                    1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    2: Allocation(AllocationMode.ABSOLUTE, 5000.0),
                },
            )
        }

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        assert len(plan.shortfalls) == 1
        shortfall = plan.shortfalls[0]
        assert shortfall.village_id == 2
        assert shortfall.per_hour == pytest.approx(4000.0)
        assert not plan.is_feasible
        # The 1,000 that *could* be shipped still is.
        assert plan.routes[0].cargo_per_hour[Resource.IRON] == pytest.approx(1000.0)

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_no_village_sends_more_than_its_surplus(self, village_count):
        villages = make_account(village_count, seed=village_count + 81)
        plans, _ = make_plans(villages, seed=village_count + 81)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        for resource, resource_plan in plans.items():
            surplus = {v.village_id: -v.ship_per_hour for v in resource_plan.senders}
            sent: dict[int, float] = {}
            for route in plan.routes:
                if resource in route.cargo_per_hour:
                    sent[route.origin] = (
                        sent.get(route.origin, 0.0) + route.cargo_per_hour[resource]
                    )
            for vid, amount in sent.items():
                assert amount <= surplus.get(vid, 0.0) + 1e-6


class TestRouteShape:
    def test_cargo_is_merged_per_pair_not_split_by_resource(self):
        """A Travian route carries all four resources together, and merchant cost
        is driven by the combined tonnage -- so one pair means one route."""
        villages = {
            1: VillageState(1, 0, 0, merchant_count=20, trade_office_level=13),
            2: VillageState(2, 12, 0, merchant_count=20, trade_office_level=13),
        }
        plans = {
            resource: resolve_resource(
                resource,
                {1: 1000.0, 2: 0.0},
                {
                    1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    2: Allocation(AllocationMode.REMAINDER),
                },
            )
            for resource in Resource
        }

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        assert len(plan.routes) == 1
        route = plan.routes[0]
        assert (route.origin, route.destination) == (1, 2)
        assert set(route.cargo_per_hour) == set(Resource)
        assert route.hourly_total == pytest.approx(4000.0)

    def test_latency_counts_the_cycle_wait_as_well_as_the_trip(self):
        villages = {
            1: VillageState(1, 0, 0, merchant_count=20),
            2: VillageState(2, 12, 0, merchant_count=20),
        }
        plans = {
            Resource.IRON: resolve_resource(
                Resource.IRON,
                {1: 500.0, 2: 0.0},
                {
                    1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    2: Allocation(AllocationMode.REMAINDER),
                },
            )
        }

        route = build_plan(villages, plans, GEOMETRY, MODEL).routes[0]

        assert route.one_way_minutes == pytest.approx(60.0)
        assert route.latency_hours == pytest.approx(route.cycle_hours + 1.0)
