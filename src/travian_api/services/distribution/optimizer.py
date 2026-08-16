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
  Crop is deliberately exempt: relay through a sub-hub is permitted there, and
  restricting the relay move to crop is what keeps this true for the rest.

Both are asserted as invariants in the tests rather than defended with runtime
checks, because the property comes from the data model, not from vigilance here.

Three stages, per profile section 14 (``cluster -> assign -> improve``):

1. **Greedy seed** -- :func:`_flows_for_resource` matches each receiver to its
   nearest senders, largest demand first. Deterministic and explainable, but
   order-dependent and blind to merchant cost.
2. **Merchant-aware local search** -- :func:`_improve_flows` reassigns that seed
   with 2x2 swaps, keeping only moves that strictly lower the lexicographic
   objective ``(over_budget_excess, total_merchants, route_count)``. It never
   returns a plan worse than the seed, and cross-resource bundling falls out of
   costing the *merged* pair cargo.
3. **Latency pass** -- :func:`_spend_idle_merchants_on_latency` then hands each
   village's *idle* merchants (those the budget allows but the merchant-minimal
   plan left unused) to the routes furthest over the latency target, shortening
   their cycles while keeping merchants full (:data:`MIN_SEND_FILL`). It spends
   strictly within budget, so feasibility never regresses, and runs only when a
   target is set. Its reach is bounded by geometry: on a spread-out account the
   one-way trip dwarfs the cycle wait, so cycle choice can only do so much --
   assignment and a crop sub-hub are what shorten the trip itself.

4. **Crop relay** -- :func:`_improve_flows` may also reroute a crop flow through
   an intermediate village that forwards it (profile section 3.5 permits this for
   crop, never for materials). Relay cannot lower total merchants -- cost is
   essentially ``cargo x round_trip / capacity``, linear in both, so the long haul
   costs the same consolidated as split and the collection legs are pure addition
   -- but it *moves* commitment off a village that cannot staff its own haul, and
   occasionally sheds a merchant to integer rounding. Because excess is the first
   objective key, it is adopted exactly when it pays and never merely to shuffle.

What it still does *not* do is claim global optimality (the problem is NP-hard,
section 14) or relay materials. A village over its merchant budget is reported,
never hidden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .allocation import EPSILON, Resource, ResourcePlan
from .geometry import MapGeometry
from .merchants import DAILY_BEAT_CYCLES, MerchantModel, cheapest_cycle, cycle_sweep

DEFAULT_MERCHANT_RESERVE = 2

# Floor on how full a merchant must stay when idle merchants are spent to cut
# latency. Shortening a cycle shrinks each batch, so without a floor the latency
# pass runs half-empty merchants just to go faster — trading merchant fill (its
# own optimisation axis) for speed. 0.6 keeps sends reasonably full while still
# letting routes speed up where the trip, not the load, is the cost.
MIN_SEND_FILL = 0.6


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

# Ceiling on improvement passes. Termination does not depend on it -- each
# accepted swap strictly lowers an integer objective -- so this exists only to
# bound worst-case runtime, and hitting it is reported rather than hidden
# (a truncated search overstates over_budget_excess, which becomes user-facing
# Trade Office advice). Sized from measurement, not guessed: 200 truncated from
# ~45 villages upward, which is an ordinary account, and at 100 villages that
# cost 86 merchants of phantom excess. 1000 converges every account size
# measured up to 120 villages; a 22-village account finishes in ~11 passes and
# never approaches it. Tunable via PlannerConfig.max_improve_passes.
MAX_IMPROVE_PASSES = 1000

# Levels of crop relay permitted. 1 allows village -> sub-hub -> destination;
# 0 disables relay entirely. Chains beyond one level would make the beat's
# collect-then-ship ordering much harder to reason about, so deeper relay is
# a deliberate future decision rather than an accident of the search.
MAX_RELAY_HOPS = 1


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


def _spend_idle_merchants_on_latency(
    routes: Sequence[Route],
    villages: Mapping[int, VillageState],
    merchant_model: MerchantModel,
    cycles: Sequence[int],
    budgets: Mapping[int, int],
    latency_target: float,
    min_send_fill: float = MIN_SEND_FILL,
) -> list[Route]:
    """Shorten over-target routes by spending each village's idle merchants.

    Merchant minimisation drives routes to long cycles (a 24h cycle can save one
    merchant over a 3h one), so the cheapest plan is also the slowest — measured
    at a median 5.6h against a 2h target. Yet villages carry idle merchants the
    budget already allows. This pass hands those idle merchants to the routes
    that most need speed: for each village it repeatedly picks the affordable
    shorter cycle giving the best latency cut per merchant, spending strictly
    within ``budget - already_committed`` so a village can never be pushed over
    its cap. Compliant routes (<= target) are left alone rather than
    over-shortened, and a route whose one-way trip alone exceeds the target is
    still sped up as far as the spare budget reaches.

    Runs only when a latency target is set; with ``None`` the plan stays purely
    merchant-minimal.
    """
    result = list(routes)
    by_origin: dict[int, list[int]] = {}
    for index, route in enumerate(result):
        by_origin.setdefault(route.origin, []).append(index)

    for origin in sorted(by_origin):
        capacity = merchant_model.capacity(villages[origin].trade_office_level)
        indices = by_origin[origin]
        spare = budgets.get(origin, 0) - sum(result[i].merchants_committed for i in indices)
        while spare > 0:
            best: tuple[tuple[int, float, int, int], int, int, int, int] | None = None
            for i in indices:
                route = result[i]
                if route.latency_hours <= latency_target:
                    continue  # already fast enough; do not waste merchants on it
                one_way = route.one_way_minutes
                for cost in cycle_sweep(route.hourly_total, 2.0 * one_way, capacity, cycles):
                    if cost.cycle_hours >= route.cycle_hours:
                        continue  # only a shorter cycle lowers latency
                    delta = cost.merchants_committed - route.merchants_committed
                    if delta <= 0 or delta > spare:
                        continue
                    new_latency = cost.cycle_hours + one_way / 60.0
                    if new_latency >= route.latency_hours:
                        continue
                    # Don't buy speed with half-empty merchants (axis 2).
                    if cost.batch < min_send_fill * cost.merchants_per_send * capacity:
                        continue
                    compliant = int(new_latency <= latency_target)
                    per_merchant = (route.latency_hours - new_latency) / delta
                    key = (compliant, per_merchant, -delta, -cost.cycle_hours)
                    if best is None or key > best[0]:
                        best = (
                            key,
                            i,
                            cost.cycle_hours,
                            cost.merchants_per_send,
                            cost.sets_in_flight,
                        )
                        best_delta = delta
            if best is None:
                break
            _, i, cycle_hours, merchants_per_send, sets_in_flight = best
            route = result[i]
            result[i] = Route(
                origin=route.origin,
                destination=route.destination,
                cargo_per_hour=route.cargo_per_hour,
                cycle_hours=cycle_hours,
                merchants_per_send=merchants_per_send,
                sets_in_flight=sets_in_flight,
                one_way_minutes=route.one_way_minutes,
            )
            spare -= best_delta
    return result


def _improve_flows(
    assignment: Assignment,
    villages: Mapping[int, VillageState],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    cycles: Sequence[int],
    budgets: Mapping[int, int],
    max_passes: int = MAX_IMPROVE_PASSES,
    max_relay_hops: int = MAX_RELAY_HOPS,
) -> tuple[Assignment, bool]:
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

    Returns:
        ``(flows, converged)``. ``converged`` is False when ``max_passes`` ran
        out with improvements still available — the caller must surface that,
        because a truncated search overstates ``over_budget_excess`` (the *first*
        objective key), and that number drives both the over-budget report and
        the Trade Office upgrade advice built from it.
    """
    # Working state, kept in sync: per-resource flows, merged pair cargo, the
    # merchant cost of each pair, and the running per-origin commitment.
    flows: Assignment = {resource: dict(legs) for resource, legs in assignment.items()}

    # Distance and capacity are fixed for the whole search but were being
    # recomputed inside the innermost loop, millions of times on a large account.
    capacities = {vid: merchant_model.capacity(v.trade_office_level) for vid, v in villages.items()}
    one_way_cache: dict[FlowKey, float] = {}

    def merchants_for(origin: int, destination: int, cargo: Mapping[Resource, float]) -> int:
        hourly_total = sum(cargo.values())
        if hourly_total <= EPSILON:
            return 0
        one_way = one_way_cache.get((origin, destination))
        if one_way is None:
            one_way = geometry.one_way_minutes(
                villages[origin].coords, villages[destination].coords
            )
            one_way_cache[(origin, destination)] = one_way
        return cheapest_cycle(
            hourly_total, 2.0 * one_way, capacities[origin], cycles
        ).merchants_committed

    pair = _merge_pair_cargo(flows)
    pair_merch: dict[FlowKey, int] = {
        key: merchants_for(key[0], key[1], cargo) for key, cargo in pair.items()
    }
    committed: dict[int, int] = {}
    for (origin, _destination), merchants in pair_merch.items():
        committed[origin] = committed.get(origin, 0) + merchants

    def excess(origin: int, count: int) -> int:
        return max(0, count - budgets.get(origin, 0))

    # Pairs proven non-improving and not invalidated by a swap since. Purely a
    # memo -- it never changes which swap is chosen, only how much work is
    # repeated to find it.
    settled: set[tuple[Resource, FlowKey, FlowKey]] = set()

    # Villages currently forwarding relayed crop. Relaying a flow whose origin is
    # already a hub would build a chain (o -> h1 -> h2 -> d); one level keeps the
    # collect-then-ship ordering in the beat analysable.
    relay_hubs: set[int] = set()

    def _apply_changes(changes: Sequence[tuple[FlowKey, Resource, float]]) -> bool:
        """Evaluate a set of pair-cargo deltas and apply them if they improve.

        Shared by the relay move. Returns True when the objective strictly fell.
        """
        touched_keys = {key for key, _r, _d in changes}
        new_cargo: dict[FlowKey, dict[Resource, float]] = {
            key: dict(pair.get(key, {})) for key in touched_keys
        }
        for key, resource, delta in changes:
            updated = new_cargo[key].get(resource, 0.0) + delta
            if updated > EPSILON:
                new_cargo[key][resource] = updated
            else:
                new_cargo[key].pop(resource, None)
        new_merch = {key: merchants_for(key[0], key[1], cargo) for key, cargo in new_cargo.items()}

        per_origin: dict[int, int] = {}
        for key in touched_keys:
            per_origin[key[0]] = per_origin.get(key[0], 0) + (
                new_merch[key] - pair_merch.get(key, 0)
            )
        over_delta = sum(
            excess(origin, committed.get(origin, 0) + delta)
            - excess(origin, committed.get(origin, 0))
            for origin, delta in per_origin.items()
        )
        total_delta = sum(new_merch.values()) - sum(pair_merch.get(key, 0) for key in touched_keys)
        rc_delta = sum(1 for key in touched_keys if new_merch[key] > 0) - sum(
            1 for key in touched_keys if pair_merch.get(key, 0) > 0
        )
        if (over_delta, total_delta, rc_delta) >= (0, 0, 0):
            return False

        for key in touched_keys:
            if new_cargo[key]:
                pair[key] = new_cargo[key]
            else:
                pair.pop(key, None)
            if new_merch[key] > 0:
                pair_merch[key] = new_merch[key]
            else:
                pair_merch.pop(key, None)
        for origin, delta in per_origin.items():
            committed[origin] = committed.get(origin, 0) + delta
        for key, resource, delta in changes:
            legs = flows[resource]
            legs[key] = legs.get(key, 0.0) + delta
            if legs[key] <= EPSILON:
                legs.pop(key, None)
        touched_villages = {v for key in touched_keys for v in key}
        settled.difference_update(
            [
                entry
                for entry in settled
                if touched_villages & {entry[1][0], entry[1][1], entry[2][0], entry[2][1]}
            ]
        )
        return True

    def _relay_scan() -> bool:
        """Reroute a crop flow through an intermediate village that forwards it.

        Relay can never lower TOTAL merchants: cost is essentially
        ``cargo x round_trip / capacity``, linear in both, so the long haul costs
        the same consolidated as split and the collection legs are pure addition
        (measured across every cluster geometry: best case break-even). What it
        does is *move* commitment off a village that cannot staff its own haul
        onto one with spare capacity. Since ``over_budget_excess`` is the first
        objective key and merchants only the second, the accept test below adopts
        relay exactly when it buys feasibility and never merely to shuffle load.

        Crop only, per profile section 3.5: materials must not chain A->B->C, and
        restricting relay to crop keeps that no-waterfall rule true by
        construction rather than by a runtime check.
        """
        if max_relay_hops < 1:
            return False
        legs = flows.get(Resource.CROP)
        if not legs:
            return False
        # Only villages already carrying crop may act as hubs. A village with no
        # crop allocation is typically one that has just been founded, and
        # conscripting it as infrastructure would both surprise the operator and
        # break the idempotent re-plan that route diffing depends on (known issue
        # #10): merely adding a village would reshuffle routes that have nothing
        # to do with it. Computed once per scan, not per edge.
        hubs = sorted({v for key in legs for v in key})
        for origin, destination in sorted(key for key, amount in legs.items() if amount > EPSILON):
            if origin in relay_hubs:
                continue  # would build a second relay level
            amount = legs.get((origin, destination), 0.0)
            if amount <= EPSILON:
                continue
            for hub in hubs:
                if hub in (origin, destination):
                    continue
                if _apply_changes(
                    [
                        ((origin, destination), Resource.CROP, -amount),
                        ((origin, hub), Resource.CROP, amount),
                        ((hub, destination), Resource.CROP, amount),
                    ]
                ):
                    relay_hubs.add(hub)
                    return True
        return False

    converged = False
    for _pass in range(max_passes):
        applied = False
        for resource in sorted(flows, key=lambda r: r.value):
            legs = flows[resource]
            edges = sorted(key for key, amount in legs.items() if amount > EPSILON)
            for i, (o1, d1) in enumerate(edges):
                for o2, d2 in edges[i + 1 :]:
                    # Skip pairs already proven non-improving that no swap since
                    # has touched. This is the whole speedup: it preserves the
                    # first-improving-in-sorted-order trajectory exactly (and so
                    # the resulting plan), while avoiding the re-evaluation of
                    # every untouched pair after each swap.
                    if (resource, (o1, d1), (o2, d2)) in settled:
                        continue
                    # o1 == d2 (or o2 == d1) would create a self-loop, whose
                    # zero travel time makes it cost zero merchants -- the most
                    # attractive move there is, silently deleting real delivery.
                    # Unreachable while a village is either sender or receiver of
                    # a resource but never both, but this must not depend on an
                    # invariant owned by another module to stay correct.
                    if o1 in (o2, d2) or d1 in (d2, o2):
                        continue
                    t = min(legs.get((o1, d1), 0.0), legs.get((o2, d2), 0.0))
                    if t <= EPSILON:
                        continue
                    candidate = (resource, (o1, d1), (o2, d2))

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
                        key: merchants_for(key[0], key[1], cargo)
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
                        settled.add(candidate)
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

                    # This swap moved cargo on four pairs and changed the
                    # commitment of two origins, so any cached verdict involving
                    # those villages is stale. Everything else is untouched and
                    # stays settled -- that is what makes the rescan cheap.
                    touched = {o1, o2, d1, d2}
                    settled.difference_update(
                        [
                            entry
                            for entry in settled
                            if touched & {entry[1][0], entry[1][1], entry[2][0], entry[2][1]}
                        ]
                    )
                    applied = True
                    break
                if applied:
                    break
            if applied:
                break
        if not applied:
            # Swaps are exhausted. Relay is tried only now, so a plan with no
            # over-budget village -- where relay can never improve anything --
            # comes out identical to one produced without this move at all.
            applied = _relay_scan()
        if not applied:
            converged = True
            break
    return flows, converged


def build_plan(
    villages: Mapping[int, VillageState],
    resource_plans: Mapping[Resource, ResourcePlan],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    *,
    merchant_reserve: int = DEFAULT_MERCHANT_RESERVE,
    cycles: Sequence[int] = DAILY_BEAT_CYCLES,
    max_latency_hours: float | None = 2.0,
    min_send_fill: float = MIN_SEND_FILL,
    max_improve_passes: int = MAX_IMPROVE_PASSES,
    max_relay_hops: int = MAX_RELAY_HOPS,
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
    assignment, converged = _improve_flows(
        assignment,
        villages,
        geometry,
        merchant_model,
        cycles,
        budgets,
        max_improve_passes,
        max_relay_hops,
    )
    if not converged:
        # Never let a truncated search masquerade as a converged one: it inflates
        # over_budget_excess, so villages get reported over budget -- and handed
        # Trade Office upgrade advice -- that a finished search would not flag.
        warnings.append(
            f"route search stopped after {max_improve_passes} improvement passes with "
            f"better assignments still available; the over-budget figures below may "
            f"overstate the real shortfall. Raise max_improve_passes to finish the search."
        )
    pair_cargo = _merge_pair_cargo(assignment)

    routes: list[Route] = [
        _route_for_pair(
            origin,
            destination,
            pair_cargo[(origin, destination)],
            villages,
            geometry,
            merchant_model,
            cycles,
        )
        for origin, destination in sorted(pair_cargo)
        if sum(pair_cargo[(origin, destination)].values()) > EPSILON
    ]

    # Spend each village's idle merchants (within budget) to shorten the routes
    # furthest over the latency target; skipped entirely when no target is set,
    # leaving the plan purely merchant-minimal.
    if max_latency_hours is not None:
        routes = _spend_idle_merchants_on_latency(
            routes, villages, merchant_model, cycles, budgets, max_latency_hours, min_send_fill
        )

    committed: dict[int, int] = {vid: 0 for vid in villages}
    for route in routes:
        committed[route.origin] += route.merchants_committed
        if max_latency_hours is not None and route.latency_hours > max_latency_hours:
            warnings.append(
                f"route {route.origin} -> {route.destination} has "
                f"{route.latency_hours:.1f}h latency against a {max_latency_hours:.0f}h "
                f"target; geometry or the merchant budget may forbid better"
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
