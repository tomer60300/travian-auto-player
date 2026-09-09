"""Offline optimality audit. Exhaustive tiny transports; no game/session imports.

Run: .venv/Scripts/python.exe scripts/audit_planner_optimality.py
The hourly-only family exhausts integer cargo solutions, exactly matching the
game's integer cargo at a one-hour cycle. The all-cycle family is a restricted
flow-grid benchmark: a better witness disproves optimality, a tie proves nothing
about fractional-rate solutions outside that grid.
"""
from __future__ import annotations

import itertools
import json
import math
import random
from pathlib import Path

from travian_api.services.distribution.allocation import (
    Allocation, AllocationMode, Resource, resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import DAILY_BEAT_CYCLES, MerchantModel, cheapest_cycle
from travian_api.services.distribution.optimizer import (
    VillageState, Route, build_plan, _spend_idle_merchants_on_latency,
)
from travian_api.services.distribution.findings import Category


def matrices():
    """Every 3x3 nonnegative integer matrix with all margins equal to two."""
    rows = [p for p in itertools.product(range(3), repeat=3) if sum(p) == 2]
    for a in rows:
        for b in rows:
            c = tuple(2 - a[j] - b[j] for j in range(3))
            if min(c) >= 0 and sum(c) == 2:
                yield (a, b, c)


def independent_cost(rate, origin, destination, villages, cycles, capacity):
    # Independent arithmetic; deliberately not route_cost/cheapest_cycle.
    a, b = villages[origin], villages[destination]
    dx, dy = abs(a.x - b.x), abs(a.y - b.y)
    rt = 2 * math.hypot(min(dx, 401 - dx), min(dy, 401 - dy)) / 12
    choices = [(math.ceil(rate * h / capacity) * math.ceil(rt / h), h) for h in cycles]
    return min(choices), rt * 60


def score(flows, villages, cycles, capacity):
    occupied = dict.fromkeys(range(1, 4), 0)
    haul = 0.0
    for (o, d), rate in flows.items():
        (merchants, _), rt = independent_cost(rate, o, d, villages, cycles, capacity)
        occupied[o] += merchants
        haul += rate * rt
    return (sum(max(0, n - 20) for n in occupied.values()),
            sum(occupied.values()), len(flows), round(haul, 6))


def family(cycles, samples=300):
    candidates = list(matrices())
    counts = {"samples": samples, "strictly_better_merchant_or_route_witness": 0,
              "strictly_better_merchant_witness": 0, "false_infeasible": 0}
    examples = []
    for seed in range(samples):
        rng = random.Random(seed)
        coords = rng.sample(list(itertools.product(range(-18, 19), repeat=2)), 6)
        capacity = rng.choice([1, 2, 3, 4])
        villages = {i + 1: VillageState(i + 1, x, y, 20) for i, (x, y) in enumerate(coords)}
        resource = Resource.LUMBER
        production = {i: 2 if i <= 3 else 0 for i in villages}
        allocations = {i: Allocation(AllocationMode.ABSOLUTE, 0 if i <= 3 else 2)
                       for i in villages}
        resolved = resolve_resource(resource, production, allocations)
        plan = build_plan(villages, {resource: resolved}, MapGeometry(401, 12),
                          MerchantModel(capacity, 0), merchant_reserve=0,
                          merchant_headroom=0, cycles=cycles,
                          max_latency_hours=None, max_relay_hops=0)
        actual = {(r.origin, r.destination): r.hourly_total for r in plan.routes}
        actual_score = score(actual, villages, cycles, capacity)
        oracle = []
        for matrix in candidates:
            flows = {(o + 1, d + 4): matrix[o][d] for o in range(3)
                     for d in range(3) if matrix[o][d]}
            oracle.append((score(flows, villages, cycles, capacity), flows))
        best_score, best = min(oracle, key=lambda p: p[0])
        # Avoid interpreting the rounded haul tie-break as an exact shared
        # objective: production rounds move deltas, not the final total.
        if best_score[:3] < actual_score[:3]:
            counts["strictly_better_merchant_or_route_witness"] += 1
            if best_score[:2] < actual_score[:2]:
                counts["strictly_better_merchant_witness"] += 1
            if best_score[0] == 0 and actual_score[0] > 0:
                counts["false_infeasible"] += 1
            if len(examples) < 5:
                examples.append({"seed": seed, "capacity": capacity,
                                 "coordinates": coords,
                                 "actual_score": actual_score,
                                 "witness_score": best_score,
                                 "actual": [[o, d, f] for (o, d), f in actual.items()],
                                 "witness": [[o, d, f] for (o, d), f in best.items()],
                                 "search_truncated": any(f.category is Category.SEARCH_TRUNCATED
                                                         for f in plan.findings)})
    return {**counts, "matrices_per_case": len(candidates), "examples": examples}


def exclusion_witness():
    villages = {i: VillageState(i, x, 0, 20)
                for i, x in [(1, 0), (2, 10), (3, 1), (4, -1)]}
    resource = Resource.LUMBER
    resolved = resolve_resource(resource, {1: 100, 2: 100, 3: 0, 4: 0},
                                {i: Allocation(AllocationMode.ABSOLUTE, t)
                                 for i, t in [(1, 0), (2, 0), (3, 120), (4, 80)]})
    plan = build_plan(villages, {resource: resolved}, MapGeometry(401, 12),
                      MerchantModel(2500, 0.2), excluded_origins_by_destination={4: {2}})
    return {"feasible": plan.is_feasible,
            "shortfalls": [[s.village_id, s.per_hour] for s in plan.shortfalls],
            "valid_witness": [[2, 3, 100], [1, 3, 20], [1, 4, 80]]}


def latency_family(samples=300):
    """Fixed routes; exact finite cycle combinations under a stated metric.

    Metric is number of target violations, then sum of worst-case route latency,
    then total merchants. This is an audit metric, not a claimed product contract.
    """
    examples = []
    worse = 0
    for seed in range(samples):
        rng = random.Random(seed)
        villages = {i: VillageState(i, i, 0, 20) for i in range(1, 5)}
        model = MerchantModel(1000, 0)
        rates = [rng.randrange(1, 21) * 100 for _ in range(3)]
        trips = [rng.randrange(1, 37) * 5 for _ in range(3)]
        routes = []
        choices = []
        for d, rate, trip in zip(range(2, 5), rates, trips):
            cost = cheapest_cycle(rate, 2 * trip, 1000)
            routes.append(Route(1, d, {Resource.LUMBER: rate}, cost.cycle_hours,
                                cost.merchants_per_send, cost.sets_in_flight, trip))
            allowed = []
            for h in DAILY_BEAT_CYCLES:
                send = math.ceil(rate * h / 1000)
                committed = send * math.ceil(2 * trip / (60 * h))
                # Match the pass's fill requirement for changed choices.
                if h <= cost.cycle_hours and (h == cost.cycle_hours or rate * h >= .1 * send * 1000):
                    allowed.append((h, committed, h + trip / 60))
            choices.append(allowed)
        baseline = sum(r.merchants_committed for r in routes)
        if baseline > 20:
            continue
        target = 2
        actual = _spend_idle_merchants_on_latency(routes, villages, model,
                                                DAILY_BEAT_CYCLES, {1: 20}, target, .1)
        def metric(combo):
            return (sum(latency > target for _, _, latency in combo),
                    round(sum(latency for _, _, latency in combo), 8),
                    sum(m for _, m, _ in combo))
        options = [c for c in itertools.product(*choices) if sum(v[1] for v in c) <= 20]
        best = min(options, key=metric)
        actual_combo = [(r.cycle_hours, r.merchants_committed, r.latency_hours) for r in actual]
        if metric(best) < metric(actual_combo):
            worse += 1
            if len(examples) < 3:
                examples.append({"seed": seed, "rates": rates, "one_way_minutes": trips,
                                 "actual": actual_combo, "witness": best,
                                 "actual_metric": metric(actual_combo), "witness_metric": metric(best)})
    return {"generated": samples, "worse_on_stated_metric": worse, "examples": examples}


if __name__ == "__main__":
    output = {"hourly_exact_integer_cargo": family((1,)),
              "all_cycles_grid_witnesses": family(DAILY_BEAT_CYCLES),
              "exclusion_witness": exclusion_witness(),
              "latency_cycle_combinations": latency_family()}
    path = Path(__file__).resolve().parents[1] / "docs" / "planner-optimality-audit-results.json"
    path.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))
