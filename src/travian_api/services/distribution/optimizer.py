"""Turn per-village shipping rates into a concrete set of trade routes.

Written for an account of **any size**. Nothing here knows how many villages
exist, which ones are hubs, or what any of them are called: village count grows
as the account expands and every figure changes between runs, so the optimizer
takes whatever state was fetched and works from that. A village that appears for
the first time simply has no allocation yet and generates no routes.

Two of the profile's known issues are already impossible by the time we get
here, because :mod:`.allocation` nets each village to a single figure per
resource:

* **#2, two-way pairs** -- a village cannot be both sender and receiver of the
  same resource, so ``A -> B`` and ``B -> A`` for iron cannot both exist.
* **waterfall for W/C/I** -- for the same reason a village that nets as a
  receiver of lumber cannot also send lumber, so ``A -> B -> C`` cannot form.

Both are asserted as invariants in the tests rather than defended with runtime
checks, because the property comes from the data model, not from vigilance here.

Two stages, per profile section 14 (``cluster -> assign -> improve``):

1. **Greedy seed** -- :func:`_flows_for_resource` matches each receiver to its
   nearest senders, largest demand first. Deterministic and explainable, but
   order-dependent and blind to merchant cost.
2. **Merchant-aware local search** -- :func:`_improve_flows` reassigns that seed
   with 2x2 swaps, keeping only moves that strictly lower the lexicographic
   objective ``(over_budget_excess, total_merchants, route_count)``. It never
   returns a plan worse than the seed, and cross-resource bundling falls out of
   costing the *merged* pair cargo.

What it still does *not* do is claim global optimality (the problem is NP-hard,
section 14) or relay crop through a sub-hub (single-leg flows only). A village
over its merchant budget is reported, never hidden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .allocation import EPSILON, Resource, ResourcePlan
from .geometry import MapGeometry
from .merchants import DAILY_BEAT_CYCLES, MerchantModel, cheapest_cycle

DEFAULT_MERCHANT_RESERVE = 2


@dataclass(frozen=True)
class VillageState:
    """Everything the optimizer needs about one village, all of it fetched.

    ``trade_office_level`` defaults to 0 because an unknown level must round
    *down*: understating it over-provisions merchants, which is wasteful but
    safe, while overstating it breaches the merchant budget invisibly.
    """

    village_id: int
    x: int
    y: int
    merchant_count: int
    trade_office_level: int = 0
    name: str = ""

    @property
    def coords(self) -> tuple[int, int]:
        return (self.x, self.y)

    def spare_merchants(self, reserve: int = DEFAULT_MERCHANT_RESERVE) -> int:
        return max(0, self.merchant_count - reserve)


@dataclass(frozen=True)
class Route:
    """One trade route: a sender, a receiver, and mixed cargo per send."""

    origin: int
    destination: int
    cargo_per_hour: Mapping[Resource, float]
    cycle_hours: int
    merchants_per_send: int
    sets_in_flight: int
    one_way_minutes: float

    @property
    def merchants_committed(self) -> int:
        return self.merchants_per_send * self.sets_in_flight

    @property
    def hourly_total(self) -> float:
        """Merchant capacity is a total across resources, not per resource."""
        return sum(self.cargo_per_hour.values())

    @property
    def batch_per_resource(self) -> dict[Resource, float]:
        """Cargo carried on a single send, before integer rounding."""
        return {r: amount * self.cycle_hours for r, amount in self.cargo_per_hour.items()}

    @property
    def latency_hours(self) -> float:
        """Worst-case wait from production to arrival: one cycle plus the trip."""
        return self.cycle_hours + self.one_way_minutes / 60.0


@dataclass(frozen=True)
class Shortfall:
    """Demand that could not be routed, and why."""

    village_id: int
    resource: Resource
    per_hour: float
    reason: str


MAX_TRADE_OFFICE_LEVEL = 20


@dataclass(frozen=True)
class OverBudget:
    """A village asked to staff more merchants than it has.

    ``trade_office_levels_needed`` is escalation step 4 of profile section 8.4:
    the smallest Trade Office upgrade that would make this village's own routes
    fit, or None when even the maximum level cannot. It is a recommendation
    about the *current* route set -- upgrading changes merchant capacity, so the
    plan should be re-run afterwards rather than assumed.
    """

    village_id: int
    committed: int
    available: int
    trade_office_levels_needed: int | None = None

    @property
    def excess(self) -> int:
        return self.committed - self.available


def _trade_office_levels_needed(
    village: VillageState,
    routes_from: Sequence[Route],
    merchant_model: MerchantModel,
    budget: int,
    cycles: Sequence[int],
) -> int | None:
    """Smallest Trade Office increase that brings *village* within *budget*."""
    for delta in range(1, MAX_TRADE_OFFICE_LEVEL - village.trade_office_level + 1):
        capacity = merchant_model.capacity(village.trade_office_level + delta)
        needed = sum(
            cheapest_cycle(
                route.hourly_total, 2.0 * route.one_way_minutes, capacity, cycles
            ).merchants_committed
            for route in routes_from
        )
        if needed <= budget:
            return delta
    return None


@dataclass(frozen=True)
class Plan:
    """A routing plan, plus everything wrong with it."""

    routes: tuple[Route, ...] = ()
    merchants_committed: Mapping[int, int] = field(default_factory=dict)
    shortfalls: tuple[Shortfall, ...] = ()
    over_budget: tuple[OverBudget, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_feasible(self) -> bool:
        return not self.over_budget and not self.shortfalls

    @property
    def total_merchants(self) -> int:
        return sum(self.merchants_committed.values())


def _flows_for_resource(
    plan: ResourcePlan,
    villages: Mapping[int, VillageState],
    geometry: MapGeometry,
) -> tuple[dict[tuple[int, int], float], list[Shortfall]]:
    """Match receivers to their nearest senders, largest demand first.

    Largest-first keeps the awkward villages from being left with only distant
    surplus, and iterating in a sorted order makes the result deterministic --
    a re-plan on unchanged input must produce an identical route set, or the
    diff against the live configuration is meaningless.
    """
    surplus: dict[int, float] = {
        v.village_id: -v.ship_per_hour for v in plan.senders if v.village_id in villages
    }
    demand = sorted(
        (v for v in plan.receivers if v.village_id in villages),
        key=lambda v: (-v.ship_per_hour, v.village_id),
    )

    flows: dict[tuple[int, int], float] = {}
    shortfalls: list[Shortfall] = []

    for receiver in demand:
        remaining = receiver.ship_per_hour
        candidates = sorted(
            (vid for vid, left in surplus.items() if left > EPSILON and vid != receiver.village_id),
            key=lambda vid: (
                geometry.distance(villages[vid].coords, villages[receiver.village_id].coords),
                vid,
            ),
        )
        for origin in candidates:
            if remaining <= EPSILON:
                break
            taken = min(surplus[origin], remaining)
            flows[(origin, receiver.village_id)] = (
                flows.get((origin, receiver.village_id), 0.0) + taken
            )
            surplus[origin] -= taken
            remaining -= taken

        if remaining > EPSILON:
            shortfalls.append(
                Shortfall(
                    village_id=receiver.village_id,
                    resource=plan.resource,
                    per_hour=remaining,
                    reason="no village has surplus left to cover this demand",
                )
            )
    return flows, shortfalls


# ---------------------------------------------------------------------------
# Merchant-aware improvement: seed with the greedy flows, then local search
# ---------------------------------------------------------------------------

# A directed flow: (origin, destination) -> rate, one map per resource.
FlowKey = tuple[int, int]
Assignment = dict[Resource, dict[FlowKey, float]]

# Backstop against a pathological non-converging sweep. Each accepted swap
# strictly lowers the integer objective, so convergence is guaranteed well
# inside this on any real account; the cap only bounds the worst case.
MAX_IMPROVE_PASSES = 200


def _merge_pair_cargo(assignment: Assignment) -> dict[FlowKey, dict[Resource, float]]:
    """Collapse the per-resource flows into one mixed cargo per village pair.

    A Travian route carries all four resources together, so merchant cost is a
    function of the *combined* tonnage on a pair — never the per-resource legs.
    """
    pair: dict[FlowKey, dict[Resource, float]] = {}
    for resource, flows in assignment.items():
        for key, amount in flows.items():
            if amount > EPSILON:
                pair.setdefault(key, {})[resource] = pair.get(key, {}).get(resource, 0.0) + amount
    return pair


def _pair_merchants(
    origin: int,
    destination: int,
    cargo: Mapping[Resource, float],
    villages: Mapping[int, VillageState],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    cycles: Sequence[int],
) -> int:
    """Merchants one pair commits at its cheapest cycle. Zero for empty cargo."""
    hourly_total = sum(cargo.values())
    if hourly_total <= EPSILON:
        return 0
    one_way = geometry.one_way_minutes(villages[origin].coords, villages[destination].coords)
    capacity = merchant_model.capacity(villages[origin].trade_office_level)
    return cheapest_cycle(hourly_total, 2.0 * one_way, capacity, cycles).merchants_committed


def _route_for_pair(
    origin: int,
    destination: int,
    cargo: Mapping[Resource, float],
    villages: Mapping[int, VillageState],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    cycles: Sequence[int],
) -> Route:
    """Build the concrete :class:`Route` for one merged pair."""
    hourly_total = sum(cargo.values())
    one_way = geometry.one_way_minutes(villages[origin].coords, villages[destination].coords)
    capacity = merchant_model.capacity(villages[origin].trade_office_level)
    cost = cheapest_cycle(hourly_total, 2.0 * one_way, capacity, cycles)
    return Route(
        origin=origin,
        destination=destination,
        cargo_per_hour=dict(sorted(cargo.items(), key=lambda kv: kv[0].value)),
        cycle_hours=cost.cycle_hours,
        merchants_per_send=cost.merchants_per_send,
        sets_in_flight=cost.sets_in_flight,
        one_way_minutes=one_way,
    )


def _improve_flows(
    assignment: Assignment,
    villages: Mapping[int, VillageState],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    cycles: Sequence[int],
    budgets: Mapping[int, int],
) -> Assignment:
    """Lower merchant commitment by reassigning flow, seeded by the greedy plan.

    The greedy seed matches each receiver to its nearest senders, which is
    order-dependent and blind to merchant cost: a remote village's surplus can
    land on far-flung leftover receivers and blow its merchant budget. This is
    the "local improvement" pass the profile (§14) always intended.

    The one move is a **2x2 swap** within a single resource: two flows
    ``o1->d1`` and ``o2->d2`` become ``o1->d2`` and ``o2->d1``, shifting the
    same rate ``t = min(both)``. It preserves every origin's total outflow and
    every destination's total inflow, so conservation, the surplus ceiling, the
    no-two-way-pair rule and the no-waterfall rule all survive untouched — a
    sender stays a sender, a receiver stays a receiver. Cross-resource bundling
    falls out for free: cost is measured on the *merged* pair cargo, so a swap
    that lands a resource on a pair another resource already uses is rewarded.

    The objective is lexicographic ``(over_budget_excess, total_merchants,
    route_count)`` — feasibility first, then merchants (§8.3 objective 1), then
    route count (objective 4). A swap is applied only when it strictly lowers
    that tuple, so the result is deterministic and never worse than the seed.
    """
    # Working state, kept in sync: per-resource flows, merged pair cargo, the
    # merchant cost of each pair, and the running per-origin commitment.
    flows: Assignment = {resource: dict(legs) for resource, legs in assignment.items()}
    pair = _merge_pair_cargo(flows)
    pair_merch: dict[FlowKey, int] = {
        key: _pair_merchants(key[0], key[1], cargo, villages, geometry, merchant_model, cycles)
        for key, cargo in pair.items()
    }
    committed: dict[int, int] = {}
    for (origin, _destination), merchants in pair_merch.items():
        committed[origin] = committed.get(origin, 0) + merchants

    def excess(origin: int, count: int) -> int:
        return max(0, count - budgets.get(origin, 0))

    for _pass in range(MAX_IMPROVE_PASSES):
        applied = False
        for resource in sorted(flows, key=lambda r: r.value):
            legs = flows[resource]
            edges = sorted(key for key, amount in legs.items() if amount > EPSILON)
            for i, (o1, d1) in enumerate(edges):
                for o2, d2 in edges[i + 1 :]:
                    if o1 == o2 or d1 == d2:
                        continue
                    t = min(legs[(o1, d1)], legs[(o2, d2)])
                    if t <= EPSILON:
                        continue

                    # Cargo of the four pairs the swap touches, after the move.
                    moved = [((o1, d1), -t), ((o2, d2), -t), ((o1, d2), t), ((o2, d1), t)]
                    new_cargo: dict[FlowKey, dict[Resource, float]] = {}
                    for key, delta in moved:
                        cargo = dict(pair.get(key, {}))
                        updated = cargo.get(resource, 0.0) + delta
                        if updated > EPSILON:
                            cargo[resource] = updated
                        else:
                            cargo.pop(resource, None)
                        new_cargo[key] = cargo
                    new_merch = {
                        key: _pair_merchants(
                            key[0], key[1], cargo, villages, geometry, merchant_model, cycles
                        )
                        for key, cargo in new_cargo.items()
                    }

                    d_o1 = (new_merch[(o1, d1)] - pair_merch.get((o1, d1), 0)) + (
                        new_merch[(o1, d2)] - pair_merch.get((o1, d2), 0)
                    )
                    d_o2 = (new_merch[(o2, d2)] - pair_merch.get((o2, d2), 0)) + (
                        new_merch[(o2, d1)] - pair_merch.get((o2, d1), 0)
                    )
                    new_o1, new_o2 = committed.get(o1, 0) + d_o1, committed.get(o2, 0) + d_o2

                    over_delta = (
                        excess(o1, new_o1)
                        - excess(o1, committed.get(o1, 0))
                        + excess(o2, new_o2)
                        - excess(o2, committed.get(o2, 0))
                    )
                    total_delta = sum(new_merch.values()) - sum(
                        pair_merch.get(key, 0) for key, _ in moved
                    )
                    rc_delta = sum(1 for key, _ in moved if new_merch[key] > 0) - sum(
                        1 for key, _ in moved if pair_merch.get(key, 0) > 0
                    )

                    # The whole objective only shifts by these deltas, so the
                    # full tuple improves exactly when the delta tuple is < 0.
                    if (over_delta, total_delta, rc_delta) >= (0, 0, 0):
                        continue

                    for key, cargo in new_cargo.items():
                        if cargo:
                            pair[key] = cargo
                        else:
                            pair.pop(key, None)
                        if new_merch[key] > 0:
                            pair_merch[key] = new_merch[key]
                        else:
                            pair_merch.pop(key, None)
                    committed[o1], committed[o2] = new_o1, new_o2
                    legs[(o1, d1)] -= t
                    legs[(o2, d2)] -= t
                    legs[(o1, d2)] = legs.get((o1, d2), 0.0) + t
                    legs[(o2, d1)] = legs.get((o2, d1), 0.0) + t
                    for key in ((o1, d1), (o2, d2)):
                        if legs[key] <= EPSILON:
                            del legs[key]
                    applied = True
                    break
                if applied:
                    break
            if applied:
                break
        if not applied:
            break
    return flows


def build_plan(
    villages: Mapping[int, VillageState],
    resource_plans: Mapping[Resource, ResourcePlan],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    *,
    merchant_reserve: int = DEFAULT_MERCHANT_RESERVE,
    cycles: Sequence[int] = DAILY_BEAT_CYCLES,
    max_latency_hours: float | None = 2.0,
) -> Plan:
    """Build a route set from fetched state. Works for any village count.

    Flows for the four resources are merged per ``(origin, destination)`` pair
    into one route, because a Travian route carries all four together and its
    merchant cost is driven by the *combined* tonnage.

    Args:
        villages: fetched state, keyed by village id. Villages referenced by an
            allocation but absent here are skipped and reported.
        resource_plans: output of :func:`~.allocation.resolve_resource`.
        geometry: distances and travel times for this server.
        merchant_model: capacity per Trade Office level.
        merchant_reserve: merchants to leave idle per village.
        cycles: candidate cycle lengths. Defaults to those dividing 24h so the
            schedule has a daily period.
        max_latency_hours: soft target; exceeding it warns rather than fails,
            because geometry can make it impossible.

    Returns:
        A :class:`Plan`. Over-budget villages and unroutable demand are reported
        in the plan, never silently dropped or quietly trimmed to fit.
    """
    warnings: list[str] = []
    assignment: Assignment = {}
    shortfalls: list[Shortfall] = []

    for resource in sorted(resource_plans, key=lambda r: r.value):
        plan = resource_plans[resource]
        missing = {v.village_id for v in plan.villages} - set(villages)
        if missing:
            warnings.append(
                f"{resource.value}: no fetched state for village(s) "
                f"{sorted(missing)}; excluded from routing"
            )
        flows, resource_shortfalls = _flows_for_resource(plan, villages, geometry)
        shortfalls.extend(resource_shortfalls)
        assignment[resource] = {key: amount for key, amount in flows.items() if amount > EPSILON}

    # Reassign the greedy seed to cut merchants and relieve over-budget villages
    # wherever a cheaper routing exists; never worse than the seed (§8.3, §14).
    budgets = {vid: villages[vid].spare_merchants(merchant_reserve) for vid in villages}
    assignment = _improve_flows(assignment, villages, geometry, merchant_model, cycles, budgets)
    pair_cargo = _merge_pair_cargo(assignment)

    routes: list[Route] = []
    committed: dict[int, int] = {vid: 0 for vid in villages}

    for origin, destination in sorted(pair_cargo):
        cargo = pair_cargo[(origin, destination)]
        if sum(cargo.values()) <= EPSILON:
            continue

        route = _route_for_pair(
            origin, destination, cargo, villages, geometry, merchant_model, cycles
        )
        routes.append(route)
        committed[origin] += route.merchants_committed

        if max_latency_hours is not None and route.latency_hours > max_latency_hours:
            warnings.append(
                f"route {origin} -> {destination} has {route.latency_hours:.1f}h "
                f"latency against a {max_latency_hours:.0f}h target; geometry may "
                f"forbid better"
            )

    routes_by_origin: dict[int, list[Route]] = {}
    for route in routes:
        routes_by_origin.setdefault(route.origin, []).append(route)

    over_budget = tuple(
        OverBudget(
            village_id=vid,
            committed=used,
            available=villages[vid].spare_merchants(merchant_reserve),
            trade_office_levels_needed=_trade_office_levels_needed(
                villages[vid],
                routes_by_origin.get(vid, []),
                merchant_model,
                villages[vid].spare_merchants(merchant_reserve),
                cycles,
            ),
        )
        for vid, used in sorted(committed.items())
        if used > villages[vid].spare_merchants(merchant_reserve)
    )

    return Plan(
        routes=tuple(routes),
        merchants_committed=committed,
        shortfalls=tuple(shortfalls),
        over_budget=over_budget,
        warnings=tuple(warnings),
    )
