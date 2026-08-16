"""Optimizer invariants, checked over randomly generated accounts.

The account this plans for is a moving target -- 22 villages today, 23 landing,
and every production figure different between runs -- so correctness is pinned
with properties that must hold for *any* shape of account rather than by
reproducing one snapshot. A golden fixture of a 20-village plan would be stale
the day it was written.
"""

import math
import random

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
    resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import (
    DAILY_BEAT_CYCLES,
    EUROPE2_TEUTON,
    route_cost,
)
from travian_api.services.distribution.optimizer import (
    MIN_SEND_FILL,
    VillageState,
    _flows_for_resource,
    _route_for_pair,
    build_plan,
)

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
    def test_no_village_relays_a_material(self, village_count):
        """The no-waterfall rule, scoped to W/C/I as profile section 3.5 states.

        Materials must never chain A->B->C. Crop is explicitly exempt there --
        relay through a sub-hub is permitted -- and the optimizer's relay move
        uses that exemption to lift merchant load off villages that cannot staff
        their own haul. Restricting relay to crop is what keeps this rule true by
        construction for the other three rather than by a runtime check.
        """
        villages = make_account(village_count, seed=village_count + 11)
        plans, _ = make_plans(villages, seed=village_count + 11)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        materials = {Resource.LUMBER, Resource.CLAY, Resource.IRON}
        sends: dict[int, set[Resource]] = {}
        receives: dict[int, set[Resource]] = {}
        for route in plan.routes:
            carried = set(route.cargo_per_hour) & materials
            sends.setdefault(route.origin, set()).update(carried)
            receives.setdefault(route.destination, set()).update(carried)
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

    def test_an_over_budget_village_gets_a_trade_office_recommendation(self):
        """Escalation step 4: say what would fix it, not just that it is broken.

        A TO 0 sender hauling a lot needs more capacity per merchant; the plan
        must name how many levels rather than only declaring infeasibility.
        """
        villages = {
            1: VillageState(1, 0, 0, merchant_count=10, trade_office_level=0),
            2: VillageState(2, 30, 0, merchant_count=20, trade_office_level=0),
        }
        plans = {
            Resource.IRON: resolve_resource(
                Resource.IRON,
                {1: 9000.0, 2: 0.0},
                {
                    1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    2: Allocation(AllocationMode.REMAINDER),
                },
            )
        }

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        assert plan.over_budget
        over = plan.over_budget[0]
        assert over.village_id == 1
        assert over.excess > 0
        assert over.trade_office_levels_needed is not None
        assert over.trade_office_levels_needed >= 1

    def test_an_impossible_village_reports_no_upgrade_that_would_help(self):
        """A village with no spare merchants cannot be fixed by any upgrade."""
        villages = {
            1: VillageState(1, 0, 0, merchant_count=2, trade_office_level=0),
            2: VillageState(2, 60, 0, merchant_count=20, trade_office_level=0),
        }
        plans = {
            Resource.IRON: resolve_resource(
                Resource.IRON,
                {1: 9000.0, 2: 0.0},
                {
                    1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    2: Allocation(AllocationMode.REMAINDER),
                },
            )
        }

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        over = plan.over_budget[0]
        assert over.available == 0
        assert over.trade_office_levels_needed is None

    def test_reserve_is_withheld_from_the_budget(self):
        village = VillageState(village_id=1, x=0, y=0, merchant_count=20)

        assert village.spare_merchants(reserve=2) == 18
        assert village.spare_merchants(reserve=0) == 20

    def test_a_village_with_fewer_merchants_than_the_reserve_has_none_spare(self):
        """Real accounts have low-marketplace villages; 2 merchants is observed."""
        village = VillageState(village_id=1, x=0, y=0, merchant_count=2)

        assert village.spare_merchants(reserve=2) == 0


def _greedy_seed_objective(villages, plans):
    """(total merchants, over-budget excess) for the greedy seed alone.

    Both halves are needed because the search optimises them lexicographically:
    a plan that spends more merchants to relieve excess is an improvement.
    """
    pair: dict[tuple[int, int], dict[Resource, float]] = {}
    for resource, resource_plan in plans.items():
        flows, _ = _flows_for_resource(resource_plan, villages, GEOMETRY)
        for key, amount in flows.items():
            pair.setdefault(key, {})[resource] = amount
    total = 0
    committed: dict[int, int] = {}
    for (origin, destination), cargo in pair.items():
        route = _route_for_pair(
            origin, destination, cargo, villages, GEOMETRY, MODEL, DAILY_BEAT_CYCLES
        )
        total += route.merchants_committed
        committed[origin] = committed.get(origin, 0) + route.merchants_committed
    excess = sum(max(0, used - villages[vid].spare_merchants()) for vid, used in committed.items())
    return total, excess


class TestImprovementNeverRegresses:
    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_improved_plan_is_never_worse_than_the_greedy_seed(self, village_count):
        """The search only accepts strictly-improving moves, so the plan is never
        worse than its seed **on the objective it optimises** -- which is the
        lexicographic pair (over_budget_excess, total_merchants), not merchants
        alone. Relay deliberately buys feasibility with extra merchants: it can
        only be accepted when excess strictly falls, so a plan may commit more
        merchants than the seed while being unambiguously better.

        Measured with the latency pass off; that stage separately spends idle
        merchants on speed and is covered by its own tests.
        """
        villages = make_account(village_count, seed=village_count + 91)
        plans, _ = make_plans(villages, seed=village_count + 91)

        seed_merchants, seed_excess = _greedy_seed_objective(villages, plans)
        plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        got = (sum(o.excess for o in plan.over_budget), plan.total_merchants)

        assert got <= (seed_excess, seed_merchants), (
            f"plan {got} is worse than the greedy seed {(seed_excess, seed_merchants)}"
        )

    @pytest.mark.parametrize("village_count", ACCOUNT_SIZES)
    def test_latency_pass_never_pushes_a_village_over_budget(self, village_count):
        """Idle-merchant spending is gated by the per-village budget, so a plan
        that was feasible without it stays feasible with it, and villages already
        over budget (no spare) are left untouched."""
        villages = make_account(village_count, seed=village_count + 91)
        plans, _ = make_plans(villages, seed=village_count + 91)

        minimal = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        with_latency = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=2.0)

        minimal_over = {o.village_id for o in minimal.over_budget}
        assert {o.village_id for o in with_latency.over_budget} == minimal_over
        for vid, used in with_latency.merchants_committed.items():
            if vid not in minimal_over:
                assert used <= villages[vid].spare_merchants()


class TestSearchTerminationIsHonest:
    """The pass cap must never quietly hand back a half-finished search.

    A truncated search overstates ``over_budget_excess`` -- the FIRST key of the
    lexicographic objective -- and that number becomes the UI's over-budget
    report and the Trade Office upgrade advice derived from it. Villages get told
    to build things a finished search would not have asked for.
    """

    def test_a_converged_search_is_a_fixed_point(self):
        """Re-running the search on its own output must change nothing. This is
        the detector for truncation: it fails exactly when the cap cut in."""
        villages = make_account(22, seed=113)
        plans, _ = make_plans(villages, seed=113)

        once = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        # Feed the finished plan's own flows back in; a converged local optimum
        # has no improving swap left, so the second run must be identical.
        twice = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

        assert once.routes == twice.routes
        assert not [w for w in once.warnings if "improvement passes" in w]

    def test_truncating_the_search_is_reported_not_hidden(self):
        """With the cap set to 1 the search cannot finish, and the plan must say
        so rather than presenting its over-budget figures as final."""
        villages = make_account(22, seed=113)
        plans, _ = make_plans(villages, seed=113)

        truncated = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=None, max_improve_passes=1
        )
        finished = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

        assert [w for w in truncated.warnings if "improvement passes" in w], (
            "a truncated search must warn; silently reporting its inflated "
            "over-budget figures is how villages get bogus upgrade advice"
        )
        # And the truncation genuinely costs something worth warning about.
        assert truncated.total_merchants >= finished.total_merchants

    def test_a_finished_search_does_not_cry_wolf(self):
        villages = make_account(12, seed=113)
        plans, _ = make_plans(villages, seed=113)

        plan = build_plan(villages, plans, GEOMETRY, MODEL, max_improve_passes=100_000)

        assert not [w for w in plan.warnings if "improvement passes" in w]


class TestLatencyPassRespectsTheFillFloor:
    def test_removing_the_fill_floor_buys_speed_with_emptier_merchants(self):
        """Pins what MIN_SEND_FILL is *for*. Without a floor the latency pass
        runs half-empty merchants purely to go faster, trading away the
        merchant-fill axis it is not allowed to spend.

        Asserted on the routes the pass actually shortens, not on a median over
        every route: most routes are never touched, so an account-wide median is
        dominated by them and barely moves either way.
        """
        villages = make_account(22, seed=127)
        plans, _ = make_plans(villages, seed=127)

        minimal = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        floored = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=2.0)
        unfloored = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=2.0, min_send_fill=0.0
        )

        def worst_shortened_fill(plan):
            before = {(r.origin, r.destination): r for r in minimal.routes}
            fills = []
            for route in plan.routes:
                was = before.get((route.origin, route.destination))
                if was is None or route.cycle_hours == was.cycle_hours:
                    continue
                capacity = MODEL.capacity(villages[route.origin].trade_office_level)
                batch = math.ceil(route.hourly_total * route.cycle_hours)
                fills.append(batch / (route.merchants_per_send * capacity))
            return min(fills, default=1.0)

        # Without the floor the pass reaches for emptier sends...
        assert worst_shortened_fill(unfloored) < MIN_SEND_FILL
        # ...while the floor holds every shortened route at or above it.
        assert worst_shortened_fill(floored) >= MIN_SEND_FILL - 1e-9
        # And the emptier sends are not free: they cost strictly more merchants.
        assert unfloored.total_merchants > floored.total_merchants

    def test_every_route_the_latency_pass_shortened_still_meets_the_floor(self):
        villages = make_account(22, seed=131)
        plans, _ = make_plans(villages, seed=131)

        minimal = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        fast = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=2.0)

        before = {(r.origin, r.destination): r for r in minimal.routes}
        for route in fast.routes:
            was = before.get((route.origin, route.destination))
            if was is None or route.cycle_hours == was.cycle_hours:
                continue  # untouched by the latency pass
            capacity = MODEL.capacity(villages[route.origin].trade_office_level)
            batch = math.ceil(route.hourly_total * route.cycle_hours)
            fill = batch / (route.merchants_per_send * capacity)
            assert fill >= MIN_SEND_FILL - 1e-9, (
                f"route {route.origin}->{route.destination} was shortened to "
                f"{fill:.0%} full, below the {MIN_SEND_FILL:.0%} floor"
            )

    def test_routes_already_within_the_target_are_left_alone(self):
        """Spending merchants on a route that already meets the target is pure
        waste: the merchants buy nothing the plan asked for.

        Honest note on what this does and does not pin. At the 2h default the
        guard is unreachable: cycles are whole hours, so a compliant route
        (cycle + trip <= 2) is necessarily already on the shortest cycle there
        is. Hence the wider 6h target here, where a route can be both compliant
        and shortenable. Even there, deleting the guard does not change any plan
        we can construct -- the fill floor rejects the same candidates first, so
        the guard is currently a defensive early-exit rather than observable
        behaviour. This test therefore pins the *invariant* (it would catch an
        implementation that shortened compliant routes) rather than killing a
        mutant, and it is documented as such instead of being dressed up as
        stronger coverage than it is.
        """
        target = 6.0
        villages = make_account(22, seed=113)
        plans, _ = make_plans(villages, seed=113)

        minimal = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        fast = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=target)

        before = {(r.origin, r.destination): r for r in minimal.routes}
        compliant_seen = 0
        for route in fast.routes:
            was = before.get((route.origin, route.destination))
            if was is not None and was.latency_hours <= target:
                compliant_seen += 1
                assert route.cycle_hours == was.cycle_hours, (
                    f"route {route.origin}->{route.destination} already met the "
                    f"{target:.0f}h target at {was.latency_hours:.1f}h but was "
                    f"still shortened to a {route.cycle_hours}h cycle"
                )
        assert compliant_seen, "fixture produced no compliant routes; test proves nothing"


class TestCropRelay:
    """Relay exists to buy feasibility, never to save merchants.

    Merchant cost is essentially cargo x round_trip / capacity -- linear in both
    -- so consolidating n shipments into one long haul costs the same on the long
    leg and adds the collection legs on top. Measured across every cluster
    geometry, relay is at best break-even on total merchants. Its real use is to
    move commitment off a village that cannot staff its own haul.
    """

    def _stranded_account(self):
        """A far village that cannot afford its own haul, plus a midpoint hub.

        The midpoint grows and ships crop of its own, so it is part of the crop
        network and therefore eligible as a hub. That matters: a village with no
        crop flow at all is excluded on purpose, so that founding a village
        cannot silently reshuffle routes it has nothing to do with.
        """
        return {
            1: VillageState(1, 0, 0, merchant_count=20, trade_office_level=15),  # capital
            2: VillageState(2, 60, 0, merchant_count=20, trade_office_level=10),  # midpoint
            3: VillageState(3, 120, 0, merchant_count=6, trade_office_level=10),  # stranded
        }, {
            Resource.CROP: resolve_resource(
                Resource.CROP,
                {1: 0.0, 2: 1200.0, 3: 9000.0},
                {
                    1: Allocation(AllocationMode.REMAINDER),
                    2: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    3: Allocation(AllocationMode.ABSOLUTE, 0.0),
                },
            )
        }

    def test_relay_lifts_load_off_a_village_that_cannot_staff_its_haul(self):
        villages, plans = self._stranded_account()

        plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

        direct = _route_for_pair(
            3, 1, {Resource.CROP: 9000.0}, villages, GEOMETRY, MODEL, DAILY_BEAT_CYCLES
        )
        assert direct.merchants_committed > villages[3].spare_merchants(), (
            "fixture must be one the stranded village genuinely cannot afford"
        )
        # The stranded village now ships only as far as the hub, so its own
        # commitment drops below what the direct haul demanded.
        assert plan.merchants_committed[3] < direct.merchants_committed
        assert any(r.origin == 3 and r.destination == 2 for r in plan.routes), (
            "expected a short collection leg 3 -> 2"
        )
        assert any(r.origin == 2 and r.destination == 1 for r in plan.routes), (
            "expected the hub to forward on to the capital"
        )

    def test_the_relay_hub_keeps_none_of_what_it_forwards(self):
        """Pure pass-through, stated on the net.

        Gross inflow and outflow do NOT match: the hub still ships its own
        surplus on top of what it forwards. What must hold is that relaying
        leaves its net crop exactly where its allocation put it -- it keeps none
        of the relayed cargo and funds none of it from its own granary.
        """
        villages, plans = self._stranded_account()

        plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

        inflow = sum(
            r.cargo_per_hour.get(Resource.CROP, 0.0) for r in plan.routes if r.destination == 2
        )
        outflow = sum(
            r.cargo_per_hour.get(Resource.CROP, 0.0) for r in plan.routes if r.origin == 2
        )
        own = next(v for v in plans[Resource.CROP].villages if v.village_id == 2)

        assert inflow > 0, "the hub is not relaying anything; test proves nothing"
        assert inflow - outflow == pytest.approx(own.ship_per_hour), (
            f"hub nets {inflow - outflow:+.0f}/h against an allocation of "
            f"{own.ship_per_hour:+.0f}/h -- it kept or funded some of the relay"
        )

    @pytest.mark.parametrize("village_count", [5, 12, 22, 40])
    def test_relay_never_makes_a_plan_worse(self, village_count):
        """Relay is adopted only when it strictly lowers the objective.

        Note it is NOT purely a feasibility move, which is what the first draft
        of this test wrongly assumed: relay also wins on integer rounding, since
        ``ceil(a/c) + ceil(b/c) >= ceil((a+b)/c)`` means consolidating two part
        loads onto one leg can shed a merchant even when nothing is over budget.
        Both are genuine improvements, so the property worth pinning is not *when*
        relay fires but that firing never costs anything: measured against the
        same plan built with the move disabled.
        """
        villages = make_account(village_count, seed=village_count + 255)
        plans, _ = make_plans(villages, seed=village_count + 255)

        with_relay = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
        without = build_plan(
            villages, plans, GEOMETRY, MODEL, max_latency_hours=None, max_relay_hops=0
        )

        def objective(plan):
            return (sum(o.excess for o in plan.over_budget), plan.total_merchants)

        assert objective(with_relay) <= objective(without), (
            f"relay made the plan worse: {objective(with_relay)} vs {objective(without)}"
        )


class TestRelayGraphStaysShallow:
    """Relay must produce single hops, not a waterfall or a loop.

    The beat's collect-then-ship ordering assumes it can place a hub's inbound
    before its outbound. A longer chain makes that a topological problem, and a
    two-way pair makes it unsatisfiable outright -- each end would have to ship
    after the other. Guarding only the relayed flow's ORIGIN was not enough:
    relaying a leg that ended at an existing hub extended the chain.
    """

    @pytest.mark.parametrize("village_count", [5, 12, 22, 40])
    def test_crop_never_forms_a_chain_or_a_two_way_pair(self, village_count):
        villages = make_account(village_count, seed=village_count + 301)
        plans, _ = make_plans(villages, seed=village_count + 301)

        plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

        edges = {
            (r.origin, r.destination) for r in plan.routes if Resource.CROP in r.cargo_per_hour
        }
        for origin, destination in edges:
            assert (destination, origin) not in edges, (
                f"crop ships both ways between {origin} and {destination}; "
                f"collect-then-ship cannot be satisfied at either end"
            )
        senders = {o for o, _ in edges}
        receivers = {d for _, d in edges}
        hubs = senders & receivers
        for hub in hubs:
            upstream = {o for o, d in edges if d == hub}
            downstream = {d for o, d in edges if o == hub}
            assert not (upstream & hubs), (
                f"village {hub} forwards crop it received from another hub {upstream & hubs}; "
                f"that is a relay chain, not a single hop"
            )
            assert not (downstream & hubs), (
                f"village {hub} forwards crop on to another hub {downstream & hubs}; "
                f"that is a relay chain, not a single hop"
            )


class TestCrossResourceBundling:
    def test_cargo_riding_an_existing_pair_is_preferred_to_opening_a_new_one(self):
        """Merchant cost is charged on the MERGED pair cargo, so moving a
        resource onto a pair another resource already uses can be free, while
        opening a fresh pair always costs at least one merchant. The search must
        see that -- costing resources independently would miss it entirely."""
        # Two senders, two receivers. 1 and 2 sit together; 3 and 4 sit together.
        villages = {
            1: VillageState(1, 0, 0, merchant_count=20, trade_office_level=20),
            2: VillageState(2, 1, 0, merchant_count=20, trade_office_level=20),
            3: VillageState(3, 30, 0, merchant_count=20, trade_office_level=20),
            4: VillageState(4, 31, 0, merchant_count=20, trade_office_level=20),
        }
        plans = {}
        for resource in (Resource.LUMBER, Resource.CLAY):
            plans[resource] = resolve_resource(
                resource,
                {1: 900.0, 2: 900.0, 3: 0.0, 4: 0.0},
                {
                    1: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    2: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    3: Allocation(AllocationMode.ABSOLUTE, 900.0),
                    4: Allocation(AllocationMode.ABSOLUTE, 900.0),
                },
            )

        plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

        # Whatever pairing it picks, both resources must ride the same pairs
        # rather than each sender opening a separate route per resource.
        assert len(plan.routes) == 2, f"expected 2 bundled routes, got {len(plan.routes)}"
        for route in plan.routes:
            assert set(route.cargo_per_hour) == {Resource.LUMBER, Resource.CLAY}, (
                "each route should carry both resources; costing them separately "
                "would have opened four single-resource routes"
            )


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
    def test_every_village_nets_exactly_what_its_allocation_asked_for(self, village_count):
        """Inflow minus outflow equals the allocation, for EVERY village.

        This supersedes the older pair of one-directional checks ("a receiver
        gets what it asked", "a sender never exceeds its surplus"), which both
        assumed a village is only ever one or the other. A crop relay hub is
        both: it receives cargo it does not keep and forwards cargo it did not
        grow, so its gross flows exceed its own allocation in both directions
        while the *net* is untouched. Netting is the property that actually
        matters -- nothing may be created or destroyed at any village -- and it
        holds whether or not relay fires, so it is the stronger statement.
        """
        villages = make_account(village_count, seed=village_count + 71)
        plans, _ = make_plans(villages, seed=village_count + 71)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        for resource, resource_plan in plans.items():
            inflow: dict[int, float] = {}
            outflow: dict[int, float] = {}
            for route in plan.routes:
                amount = route.cargo_per_hour.get(resource)
                if amount:
                    outflow[route.origin] = outflow.get(route.origin, 0.0) + amount
                    inflow[route.destination] = inflow.get(route.destination, 0.0) + amount
            missing = {s.village_id: s.per_hour for s in plan.shortfalls if s.resource is resource}
            for village in resource_plan.villages:
                vid = village.village_id
                net = inflow.get(vid, 0.0) - outflow.get(vid, 0.0)
                gap = missing.get(vid, 0.0)
                assert net + gap == pytest.approx(village.ship_per_hour, rel=1e-6, abs=1e-6), (
                    f"village {vid} nets {net:+.1f}/h of {resource.value} against an "
                    f"allocation of {village.ship_per_hour:+.1f}/h (shortfall {gap:.1f})"
                )

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
    def test_no_village_sends_more_material_than_its_surplus(self, village_count):
        """For W/C/I gross outflow is still capped by the village's own surplus:
        those never relay, so there is no forwarded cargo to account for. Crop is
        covered by the net-flow invariant above instead."""
        villages = make_account(village_count, seed=village_count + 81)
        plans, _ = make_plans(villages, seed=village_count + 81)

        plan = build_plan(villages, plans, GEOMETRY, MODEL)

        for resource in (Resource.LUMBER, Resource.CLAY, Resource.IRON):
            resource_plan = plans[resource]
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
