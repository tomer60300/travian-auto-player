"""Counterexamples from the independent planner review, without game I/O."""

from itertools import combinations, product
from random import Random

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
    resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import MerchantModel, cheapest_cycle
from travian_api.services.distribution.optimizer import (
    Route,
    VillageState,
    _flows_for_resource,
    _improve_flows,
    _spend_idle_merchants_on_latency,
    build_plan,
)


def test_exclusions_reassign_flexible_demand_before_reporting_shortage():
    villages = {i: VillageState(i, x, 0, 20) for i, x in [(1, 0), (2, 10), (3, 1), (4, -1)]}
    resource = Resource.LUMBER
    allocation = resolve_resource(
        resource,
        {1: 100, 2: 100, 3: 0, 4: 0},
        {
            i: Allocation(AllocationMode.ABSOLUTE, amount)
            for i, amount in [(1, 0), (2, 0), (3, 120), (4, 80)]
        },
    )
    plan = build_plan(
        villages,
        {resource: allocation},
        MapGeometry(401, 12),
        MerchantModel(2500, 0.2),
        excluded_origins_by_destination={4: {2}},
    )
    assert plan.is_feasible
    assert sum(r.hourly_total for r in plan.routes if r.destination == 4) == 80
    assert not any(r.origin == 2 and r.destination == 4 for r in plan.routes)


@pytest.mark.parametrize(
    ("coords", "capacity", "cycles", "expected"),
    [
        ([(6, 17), (14, -5), (6, -12), (7, -13), (-16, -7), (10, -2)], 4000, (1,), 8),
        (
            [(-12, -8), (-9, 11), (9, 6), (-2, 6), (14, -2), (8, 12)],
            3000,
            (1, 2, 3, 4, 6, 8, 12, 24),
            6,
        ),
    ],
)
def test_three_route_rotation_escapes_two_route_local_optimum(coords, capacity, cycles, expected):
    villages = {i + 1: VillageState(i + 1, x, y, 20) for i, (x, y) in enumerate(coords)}
    resource = Resource.LUMBER
    allocation = resolve_resource(
        resource,
        {i: 2000 if i <= 3 else 0 for i in villages},
        {i: Allocation(AllocationMode.ABSOLUTE, 0 if i <= 3 else 2000) for i in villages},
    )
    plan = build_plan(
        villages,
        {resource: allocation},
        MapGeometry(401, 12),
        MerchantModel(capacity, 0),
        merchant_reserve=0,
        merchant_headroom=0,
        cycles=cycles,
        max_latency_hours=None,
        max_relay_hops=0,
    )
    assert plan.total_merchants == expected
    assert plan.is_feasible


def test_rotation_work_limit_is_reported(monkeypatch):
    from travian_api.services.distribution import optimizer
    from travian_api.services.distribution.findings import Category

    monkeypatch.setattr(optimizer, "MAX_ROTATION_CANDIDATES", 0)
    coords = [(6, 17), (14, -5), (6, -12), (7, -13), (-16, -7), (10, -2)]
    villages = {i + 1: VillageState(i + 1, x, y, 20) for i, (x, y) in enumerate(coords)}
    allocation = resolve_resource(
        Resource.LUMBER,
        {i: 2000 if i <= 3 else 0 for i in villages},
        {i: Allocation(AllocationMode.ABSOLUTE, 0 if i <= 3 else 2000) for i in villages},
    )
    plan = build_plan(
        villages,
        {Resource.LUMBER: allocation},
        MapGeometry(401, 12),
        MerchantModel(4000, 0),
        merchant_reserve=0,
        merchant_headroom=0,
        cycles=(1,),
        max_latency_hours=None,
        max_relay_hops=0,
    )
    assert plan.total_merchants == 10
    assert any(f.category is Category.SEARCH_TRUNCATED for f in plan.findings)


def test_latency_chooses_best_combination_within_budget():
    villages = {i: VillageState(i, x, 0, 20) for i, x in [(1, 0), (2, 32), (3, 22), (4, 3)]}
    routes = []
    for destination, rate, trip in [(2, 650, 160), (3, 550, 110), (4, 900, 15)]:
        cost = cheapest_cycle(rate, trip * 2, 1000)
        routes.append(
            Route(
                1,
                destination,
                {Resource.LUMBER: rate},
                cost.cycle_hours,
                cost.merchants_per_send,
                cost.sets_in_flight,
                trip,
            )
        )
    result = _spend_idle_merchants_on_latency(
        routes, villages, MerchantModel(1000, 0), (1, 2, 3, 4, 6, 8, 12, 24), {1: 10}, 2, 0.6
    )
    assert [r.cycle_hours for r in result] == [1, 4, 1]
    assert sum(r.merchants_committed for r in result) == 10


def test_residual_repair_matches_every_three_by_three_exclusion_cut():
    """Independent min-cut oracle: all 512 allowed-edge graphs, uneven supply."""
    supply = {1: 2, 2: 1, 3: 2}
    demand = {4: 1, 5: 2, 6: 2}
    villages = {i: VillageState(i, i, 0, 20) for i in range(1, 7)}
    allocation = resolve_resource(
        Resource.LUMBER,
        {**supply, **dict.fromkeys(demand, 0)},
        {i: Allocation(AllocationMode.ABSOLUTE, demand.get(i, 0)) for i in villages},
    )
    edges = list(product(supply, demand))
    for mask in range(1 << len(edges)):
        allowed = {e for j, e in enumerate(edges) if mask & (1 << j)}
        excluded = {d: {o for o in supply if (o, d) not in allowed} for d in demand}
        flows, shortfalls = _flows_for_resource(
            allocation,
            villages,
            MapGeometry(401, 12),
            excluded=excluded,
        )
        cuts = []
        for n in range(4):
            for subset in combinations(demand, n):
                neighbors = {o for o, d in allowed if d in subset}
                cuts.append(
                    sum(supply[o] for o in neighbors)
                    + sum(q for d, q in demand.items() if d not in subset)
                )
        assert sum(flows.values()) == pytest.approx(min(cuts)), mask
        assert sum(s.per_hour for s in shortfalls) == pytest.approx(5 - min(cuts)), mask
        assert set(flows) <= allowed
        for origin, amount in supply.items():
            assert sum(q for (o, _), q in flows.items() if o == origin) <= amount
        for destination, amount in demand.items():
            assert sum(q for (_, d), q in flows.items() if d == destination) <= amount


def test_fixed_relay_cargo_is_priced_but_never_reassigned():
    villages = {
        i: VillageState(i, x, 0, 20) for i, x in [(1, 0), (2, -10), (3, 1), (4, 10), (5, -1)]
    }
    resource = Resource.LUMBER
    assignment = {resource: {(1, 4): 100, (2, 3): 100}}
    fixed = {resource: {(1, 5): 100}}
    result, converged = _improve_flows(
        assignment,
        villages,
        MapGeometry(401, 12),
        MerchantModel(1000, 0),
        (1,),
        {1: 2, 2: 10},
        100,
        0,
        fixed_assignment=fixed,
    )
    assert converged
    assert result == {resource: {(1, 3): 100, (2, 4): 100}}
    assert assignment == {resource: {(1, 4): 100, (2, 3): 100}}
    assert fixed == {resource: {(1, 5): 100}}


def test_latency_dynamic_program_matches_exhaustive_combinations():
    rng = Random(906)
    cycles = (1, 2, 3, 4)
    villages = {i: VillageState(i, i, 0, 20) for i in range(4)}
    model = MerchantModel(1000, 0)
    for _ in range(75):
        routes = []
        options = []
        for destination in range(1, 4):
            rate, trip = rng.randint(100, 1500), rng.randint(1, 180)
            cost = cheapest_cycle(rate, 2 * trip, 1000, cycles)
            route = Route(
                0,
                destination,
                {Resource.LUMBER: rate},
                cost.cycle_hours,
                cost.merchants_per_send,
                cost.sets_in_flight,
                trip,
            )
            routes.append(route)
            candidates = [route]
            for cycle in cycles:
                if cycle >= route.cycle_hours:
                    continue
                # Price each fixed cycle independently, not via the DP's sweep.
                c = cheapest_cycle(rate, 2 * trip, 1000, (cycle,))
                if c.batch >= 0.6 * c.merchants_per_send * 1000:
                    candidates.append(
                        Route(
                            0,
                            destination,
                            route.cargo_per_hour,
                            cycle,
                            c.merchants_per_send,
                            c.sets_in_flight,
                            trip,
                        )
                    )
            options.append(candidates)
        budget = sum(r.merchants_committed for r in routes) + rng.randint(0, 5)
        target = rng.choice((2, 3, 4))

        def objective(candidate, target=target):
            return (
                sum(r.latency_hours > target for r in candidate),
                sum(r.cycle_hours for r in candidate),
                sum(r.merchants_committed for r in candidate),
            )

        oracle = min(objective(c) for c in product(*options) if objective(c)[2] <= budget)
        result = _spend_idle_merchants_on_latency(
            routes,
            villages,
            model,
            cycles,
            {0: budget},
            target,
            0.6,
        )
        assert objective(result) == oracle
