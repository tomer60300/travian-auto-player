"""Golden regression for the flow optimizer, frozen on a real 22-village account.

Profile Appendix A asks for exactly this: a frozen snapshot — production, coords,
Trade Office levels and allocation targets in; route set and per-village merchant
pool out — that pins the optimizer against silent regressions. It is deliberately
*not* a live read (the account has already gone 20 -> 22 -> 23 villages; a golden
test that reads current state stops being a regression test), and it captures the
case that motivated the merchant-aware improvement pass: two geographically
stranded villages (15 at 121|52, 21 at 7|35) whose crop surplus cannot be shipped
within their merchant budget at any Trade Office level.

On the assertions: the merchant-minimal figures are upper *bounds* (``<=``), not
equalities, so a genuine search improvement passes rather than failing as a
"regression". Equalities were tried first and were actively harmful — they made
these two integers the only coverage for the fill floor, the pass cap and
cross-resource bundling, so any legitimate re-baselining would have silently
deleted that coverage. Those behaviours are now pinned directly in
``test_distribution_optimizer.py``; what remains here is the ratchet.

The latency plan is deliberately NOT bounded above by merchants: that pass exists
to spend idle merchants on speed, so a better one may legitimately spend more.
It is bounded by the properties that must hold instead — within budget, faster
than the merchant-minimal plan, no new over-budget village.
"""

import json
import statistics
from pathlib import Path

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
    resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import DAILY_BEAT_CYCLES, MerchantModel
from travian_api.services.distribution.optimizer import (
    VillageState,
    _flows_for_resource,
    _route_for_pair,
    build_plan,
)

FIXTURE = Path(__file__).parent / "fixtures" / "distribution_real_account.json"

# The greedy seed's numbers on this account, measured before the improvement pass
# existed. The improved optimizer must never do worse than these.
SEED_TOTAL_MERCHANTS = 163

# Ratchet on the merchant-minimal plan (latency pass off): the optimizer must do
# at least this well. Tighten these when an improvement lands; never loosen them
# without saying why.
MAX_TOTAL_MERCHANTS = 152
MAX_OVER_BUDGET_EXCESS = 17

# Villages whose crop cannot be shipped within their merchant budget on this
# snapshot. They are NOT interchangeable, and the distinction is the point:
#   20015 ("15", 121|52) is distance-bound -- 100+ fields from everything, so no
#       Trade Office level shrinks the haul and the report says so.
#   81449 ("21", 7|35) is capacity-bound -- it has a concrete TO+7 fix, so a
#       future improvement may legitimately route it within budget.
# Hence a subset assertion for the set and an exact assertion only for 20015.
DISTANCE_BOUND_VILLAGE = 20015
CAPACITY_BOUND_VILLAGE = 81449


def _median_latency(plan):
    return statistics.median(route.latency_hours for route in plan.routes)


def _load_case():
    body = json.loads(FIXTURE.read_text())
    snap = body["snapshot"]
    trade_office = {c["village_id"]: c["trade_office_level"] for c in body["config"]}
    villages = {
        v["village_id"]: VillageState(
            village_id=v["village_id"],
            x=v["x"],
            y=v["y"],
            merchant_count=v["merchants_total"],
            trade_office_level=trade_office.get(v["village_id"], 0),
            name=v["name"],
        )
        for v in snap
    }
    rate_field = {
        Resource.LUMBER: "lumber_per_hour",
        Resource.CLAY: "clay_per_hour",
        Resource.IRON: "iron_per_hour",
        Resource.CROP: "crop_per_hour",
    }
    productions = {}
    for resource, field_name in rate_field.items():
        rates = {
            v["village_id"]: float(v[field_name]) for v in snap if v.get(field_name) is not None
        }
        if rates:
            productions[resource] = rates
    allocations = {
        Resource(res): {
            int(vid): Allocation(mode=AllocationMode(item["mode"]), value=item["value"])
            for vid, item in per.items()
            if item["mode"] != "keep"
        }
        for res, per in body["allocations"].items()
    }
    plans = {
        res: resolve_resource(res, productions[res], allocations.get(res, {}))
        for res in productions
    }
    geometry = MapGeometry(
        span=body["map_span"], speed_fields_per_hour=body["speed_fields_per_hour"]
    )
    model = MerchantModel(
        base_capacity=body["merchant_base_capacity"],
        bonus_per_trade_office_level=body["trade_office_bonus_per_level"],
    )
    return villages, plans, geometry, model


def _seed_total_merchants(villages, plans, geometry, model):
    """Merchants the greedy seed alone would commit — the number to beat."""
    pair: dict[tuple[int, int], dict[Resource, float]] = {}
    for resource, resource_plan in plans.items():
        flows, _ = _flows_for_resource(resource_plan, villages, geometry)
        for key, amount in flows.items():
            pair.setdefault(key, {})[resource] = amount
    total = 0
    for (origin, destination), cargo in pair.items():
        route = _route_for_pair(
            origin, destination, cargo, villages, geometry, model, DAILY_BEAT_CYCLES
        )
        total += route.merchants_committed
    return total


def test_the_improved_plan_never_costs_more_than_the_greedy_seed():
    villages, plans, geometry, model = _load_case()

    seed = _seed_total_merchants(villages, plans, geometry, model)
    # Latency pass off: this property is about the merchant-minimal stage.
    plan = build_plan(villages, plans, geometry, model, max_latency_hours=None)

    assert seed == SEED_TOTAL_MERCHANTS
    assert plan.total_merchants <= seed


def test_the_optimizer_holds_its_ratchet():
    villages, plans, geometry, model = _load_case()

    plan = build_plan(villages, plans, geometry, model, max_latency_hours=None)

    assert plan.total_merchants <= MAX_TOTAL_MERCHANTS
    assert sum(o.excess for o in plan.over_budget) <= MAX_OVER_BUDGET_EXCESS
    assert not plan.shortfalls
    # A finished search, so the over-budget figures below are trustworthy.
    assert not [w for w in plan.warnings if "improvement passes" in w]


def test_the_latency_pass_trades_idle_merchants_for_speed():
    """With a latency target, otherwise-idle merchants shorten cycles: travel
    time falls, spending stays inside every village's budget, and no village that
    was within budget is pushed over it.

    Deliberately no upper bound on merchants here -- see the module docstring.
    """
    villages, plans, geometry, model = _load_case()

    minimal = build_plan(villages, plans, geometry, model, max_latency_hours=None)
    fast = build_plan(villages, plans, geometry, model, max_latency_hours=2.0)

    assert _median_latency(fast) < _median_latency(minimal)
    # Feasibility is unchanged: no village that was within budget is pushed over.
    minimal_over = {o.village_id for o in minimal.over_budget}
    assert {o.village_id for o in fast.over_budget} <= minimal_over
    for vid, used in fast.merchants_committed.items():
        if vid not in minimal_over:
            assert used <= villages[vid].spare_merchants()


def test_an_unroutable_village_is_reported_not_hidden():
    """Village 15 cannot ship its crop within budget at any Trade Office level;
    that must surface as an honest over-budget report saying no upgrade helps,
    never as a silently trimmed plan."""
    villages, plans, geometry, model = _load_case()

    plan = build_plan(villages, plans, geometry, model)

    over = {o.village_id: o for o in plan.over_budget}
    assert DISTANCE_BOUND_VILLAGE in over
    assert over[DISTANCE_BOUND_VILLAGE].excess > 0
    # 100+ fields from everything: the trip, not the load, is the cost.
    assert over[DISTANCE_BOUND_VILLAGE].trade_office_levels_needed is None

    # The capacity-bound village may or may not still be over budget -- a better
    # search could route it -- but while it is, it must carry its concrete fix.
    capacity_bound = over.get(CAPACITY_BOUND_VILLAGE)
    if capacity_bound is not None:
        assert capacity_bound.trade_office_levels_needed is not None


def test_golden_plan_is_deterministic():
    villages, plans, geometry, model = _load_case()

    first = build_plan(villages, plans, geometry, model)
    second = build_plan(villages, plans, geometry, model)

    assert first.routes == second.routes
    assert first.merchants_committed == second.merchants_committed


def test_golden_plan_respects_the_structural_invariants():
    villages, plans, geometry, model = _load_case()

    plan = build_plan(villages, plans, geometry, model)

    # No two-way pair and no relay of the same resource (issues #2 and the
    # no-waterfall rule), checked on the real account rather than only on
    # generated ones.
    sends: dict[int, set] = {}
    receives: dict[int, set] = {}
    carried: dict[tuple[int, int], set] = {}
    for route in plan.routes:
        carried[(route.origin, route.destination)] = set(route.cargo_per_hour)
        sends.setdefault(route.origin, set()).update(route.cargo_per_hour)
        receives.setdefault(route.destination, set()).update(route.cargo_per_hour)
    for (origin, destination), resources in carried.items():
        assert not (resources & carried.get((destination, origin), set()))
    for vid in villages:
        assert not (sends.get(vid, set()) & receives.get(vid, set()))

    # Conservation: no village ships more of a resource than its surplus.
    for resource, resource_plan in plans.items():
        surplus = {v.village_id: -v.ship_per_hour for v in resource_plan.senders}
        sent: dict[int, float] = {}
        for route in plan.routes:
            if resource in route.cargo_per_hour:
                sent[route.origin] = sent.get(route.origin, 0.0) + route.cargo_per_hour[resource]
        for vid, amount in sent.items():
            assert amount <= surplus.get(vid, 0.0) + 1e-6
