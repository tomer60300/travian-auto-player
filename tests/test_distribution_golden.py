"""Golden regression for the flow optimizer, frozen on a real 22-village account.

Profile Appendix A asks for exactly this: a frozen snapshot — production, coords,
Trade Office levels and allocation targets in; route set and per-village merchant
pool out — that pins the optimizer against silent regressions. It is deliberately
*not* a live read (the account has already gone 20 -> 22 -> 23 villages; a golden
test that reads current state stops being a regression test), and it captures the
case that motivated the merchant-aware improvement pass: two geographically
stranded villages (15 at 121|52, 21 at 7|35) whose crop surplus cannot be shipped
within their merchant budget at any Trade Office level.

The pinned totals track the current optimizer. Lowering them when a future search
improvement does better is expected — raising them is a regression.
"""

import json
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
SEED_OVER_BUDGET_EXCESS = 22

# What the current merchant-aware optimizer achieves. Pinned so an accidental
# regression is caught; expected to only ever drop.
EXPECTED_TOTAL_MERCHANTS = 152
EXPECTED_OVER_BUDGET_EXCESS = 17
# Both remote and crop-heavy; no Trade Office upgrade shrinks village 15's haul.
STRANDED_VILLAGES = {20015, 81449}


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
    plan = build_plan(villages, plans, geometry, model)

    assert seed == SEED_TOTAL_MERCHANTS
    assert plan.total_merchants <= seed


def test_golden_totals_are_pinned():
    villages, plans, geometry, model = _load_case()

    plan = build_plan(villages, plans, geometry, model)

    assert plan.total_merchants == EXPECTED_TOTAL_MERCHANTS
    assert sum(o.excess for o in plan.over_budget) == EXPECTED_OVER_BUDGET_EXCESS
    assert sum(o.excess for o in plan.over_budget) <= SEED_OVER_BUDGET_EXCESS
    assert not plan.shortfalls


def test_the_stranded_villages_are_reported_not_hidden():
    """15 and 21 cannot ship their crop within budget; that must surface as an
    honest over-budget report, and village 15 as unfixable by any upgrade."""
    villages, plans, geometry, model = _load_case()

    plan = build_plan(villages, plans, geometry, model)

    assert {o.village_id for o in plan.over_budget} == STRANDED_VILLAGES
    fifteen = next(o for o in plan.over_budget if o.village_id == 20015)
    assert fifteen.trade_office_levels_needed is None  # 100+ fields out; distance-bound


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
