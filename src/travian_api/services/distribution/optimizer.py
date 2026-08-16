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
   crop, never for materials). Its main use is *moving* commitment off a village
   that cannot staff its own haul, but it can also genuinely lower the total:
   the cost ``ceil(batch/capacity) x sets_in_flight`` multiplies each send's
   partial-vehicle waste by every set in flight, and pooling several part-loads
   onto one trunk removes that waste (five 900/h flows over a 1,200-min round
   trip at capacity 1,000 cost 100 vehicles direct, 95 pooled). The continuous
   relaxation -- where cost is linear in cargo x distance and relay can never
   win -- only approximates the regime where batches are large relative to
   capacity. Because excess is the first objective key, relay is adopted exactly
   when it pays. NOTE: single-flow moves harvest pooling only partially; fully
   pooling several senders needs a compound move each step of which is
   break-even, which first-improvement cannot cross.

What it still does *not* do is claim global optimality (the problem is NP-hard,
section 14) or relay materials. A village over its merchant budget is reported,
never hidden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .allocation import EPSILON, Resource, ResourcePlan, village_label
from .geometry import MapGeometry
from .merchants import DAILY_BEAT_CYCLES, MerchantModel, cheapest_cycle, cycle_sweep

DEFAULT_MERCHANT_RESERVE = 2

# Floor on how full a merchant must stay when idle merchants are spent to cut
# latency. Shortening a cycle shrinks each batch, so without a floor the latency
# pass runs half-empty merchants just to go faster — trading merchant fill (its
# own optimisation axis) for speed. 0.6 keeps sends reasonably full while still
# letting routes speed up where the trip, not the load, is the cost.
MIN_SEND_FILL = 0.6

# How much a route into a foreign tribute counts for, against the route-count
# term. A tribute split across several villages is several routes to create and
# watch for one obligation, so consolidating one is worth more than shedding an
# ordinary route. Sinks are identified structurally -- they can receive but have
# no merchants to send with -- rather than by any flag.
SINK_ROUTE_WEIGHT = 4


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
            best: tuple[tuple[int, int, float, int, int], int, int, int, int] | None = None
            for i in indices:
                route = result[i]
                # Every route is a candidate, not only the ones over target.
                # A 1h cycle really is better than a 3h one, so once the routes
                # that breach the target have been dealt with, leftover idle
                # merchants keep buying speed on the rest. Routes that already
                # comply are simply ranked last, via `urgency` below.
                urgency = int(route.latency_hours > latency_target)
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
                    # Fix what breaches the target first, then buy the biggest
                    # remaining latency cut per merchant spent.
                    key = (urgency, compliant, per_merchant, -delta, -cost.cycle_hours)
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


# How many transfer sizes the refinement sweep tries per swap. Boundaries are
# deduped and taken largest-first; the cap bounds refinement cost without
# silently dropping the full transfer, which is always included.
MAX_BREAKPOINT_CANDIDATES = 12


def breakpoint_candidates(
    grows: Sequence[tuple[float, int]],
    shrinks: Sequence[tuple[float, int]],
    t_full: float,
    cycles: Sequence[int],
) -> list[float]:
    """Transfer sizes worth trying besides all-of-min.

    Vehicle cost is a staircase, so the best transfer is often a breakpoint
    BELOW ``min(both)``: the size that lands some pair's batch exactly on a
    multiple of its origin's capacity, wasting nothing. All-of-min restricts the
    search to transportation-polytope vertices; these interior points are where
    the ceiling-waste savings live.

    Boundaries come from all FOUR touched pairs, not only the growing two. The
    first version generated candidates only where a growing pair's batch hit a
    boundary, and Codex review produced the counterexample: with the saving on a
    SHRINKING leg (200/h over a 1,800-minute round trip dropping to the
    166.67/h boundary at a 6h cycle), the only improving transfer is one this
    function never emitted, and the search declared convergence with an
    improvement still on the table. Both directions now contribute, across every
    cycle, one boundary each (the nearest below the pair's post-transfer load).

    Args:
        grows: ``(current_rate, origin_capacity)`` for the two pairs gaining t.
        shrinks: same, for the two pairs losing t.
        t_full: the all-of-min transfer; always included.
        cycles: candidate cycle lengths.

    Returns:
        Distinct sizes in (0, t_full], largest first, capped at
        :data:`MAX_BREAKPOINT_CANDIDATES`.
    """
    candidates = {t_full}
    for rate, capacity in grows:
        for cycle in cycles:
            step = capacity / cycle  # rate that fills one vehicle exactly
            k = int((rate + t_full) // step)
            if k >= 1:
                t = k * step - rate
                if EPSILON < t < t_full - EPSILON:
                    candidates.add(t)
    for rate, capacity in shrinks:
        for cycle in cycles:
            step = capacity / cycle
            k = int((rate - EPSILON) // step)
            if k >= 1:
                t = rate - k * step
                if EPSILON < t < t_full - EPSILON:
                    candidates.add(t)
    return sorted(candidates, reverse=True)[:MAX_BREAKPOINT_CANDIDATES]


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
    route_count, cargo_weighted_round_trip)`` — feasibility first, then
    merchants (§8.3 objective 1), then route count (objective 4), then shorter
    hauls as the tie-break so equal-cost plans prefer nearer assignments. A move
    is applied only when it strictly lowers that tuple, so the result is
    deterministic and never worse than the seed.

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

    def _one_way(key: FlowKey) -> float:
        cached = one_way_cache.get(key)
        if cached is None:
            cached = geometry.one_way_minutes(villages[key[0]].coords, villages[key[1]].coords)
            one_way_cache[key] = cached
        return cached

    pair = _merge_pair_cargo(flows)
    pair_merch: dict[FlowKey, int] = {
        key: merchants_for(key[0], key[1], cargo) for key, cargo in pair.items()
    }
    committed: dict[int, int] = {}
    for (origin, _destination), merchants in pair_merch.items():
        committed[origin] = committed.get(origin, 0) + merchants

    def excess(origin: int, count: int) -> int:
        return max(0, count - budgets.get(origin, 0))

    # A village with no merchants cannot ship, so it is a pure sink: a foreign
    # tribute. Splitting one across suppliers is harder to run than an ordinary
    # split, so each route into it weighs more in the route-count term.
    sinks = {vid for vid, village in villages.items() if village.merchant_count == 0}

    def route_weight(key: FlowKey) -> int:
        return SINK_ROUTE_WEIGHT if key[1] in sinks else 1

    # Pairs proven non-improving and not invalidated by a swap since. Purely a
    # memo -- it never changes which swap is chosen, only how much work is
    # repeated to find it.
    settled: set[tuple[Resource, FlowKey, FlowKey]] = set()

    # Villages currently forwarding relayed crop. Relaying a flow whose origin is
    # already a hub would build a chain (o -> h1 -> h2 -> d); one level keeps the
    # collect-then-ship ordering in the beat analysable.
    relay_hubs: set[int] = set()

    def _crop_shape_ok(edges: set[FlowKey]) -> bool:
        """Would this crop graph still be a set of single hops?

        Asked of a PROSPECTIVE edge set, before anything is committed. It cannot
        be asked afterwards and undone: :func:`_apply_changes` only applies moves
        that improve the objective, so a revert -- which by definition worsens it
        -- is silently refused and the bad shape stays.

        Guarding the relay move alone is also not enough. Once a hub exists an
        ordinary 2x2 swap can rewire its legs into a longer chain, and swaps know
        nothing about relay, so both movers consult this.
        """
        senders = {origin for origin, _ in edges}
        receivers = {destination for _, destination in edges}
        hubs = senders & receivers
        for origin, destination in edges:
            if (destination, origin) in edges:
                return False  # a two-way pair: ship-after-collect is unsatisfiable
            if origin in hubs and destination in hubs:
                return False  # hub feeding a hub: a chain, not a single hop
        return True

    def _crop_edges() -> set[FlowKey]:
        return {key for key, amount in flows.get(Resource.CROP, {}).items() if amount > EPSILON}

    def _score_changes(changes: Sequence[tuple[FlowKey, Resource, float]]):
        """Cost a set of pair-cargo deltas WITHOUT applying them.

        Split out from applying so a mover can compare candidates before
        committing to one. Relay used to take the first hub that merely
        improved, and since hubs are iterated in village-id order that made the
        choice arbitrary: on a real account it routed a 96-field haul through
        the lowest-numbered village while four nearer ones sat unused.

        Returns ``(delta, state)`` where delta is the lexicographic objective
        change -- lower is better, negative means an improvement -- and state is
        what :func:`_commit_changes` needs to apply it.
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
        rc_delta = sum(route_weight(key) for key in touched_keys if new_merch[key] > 0) - sum(
            route_weight(key) for key in touched_keys if pair_merch.get(key, 0) > 0
        )
        # Cargo-weighted round-trip, the final tie-break. Plans that tie on
        # excess, merchants and route count used to be tie-broken by whatever
        # move happened to be scanned first, which left the search actively
        # latency-blind: two equal-cost assignments where one hauls the cargo
        # twice as far were interchangeable. Rounded to an integer so the
        # objective stays a strictly-decreasing bounded integer tuple and
        # termination remains guaranteed.
        rt_delta = round(
            sum(
                (sum(new_cargo[key].values()) - sum(pair.get(key, {}).values()))
                * 2.0
                * _one_way(key)
                for key in touched_keys
            )
        )
        return (over_delta, total_delta, rc_delta, rt_delta), (
            touched_keys,
            new_cargo,
            new_merch,
            per_origin,
        )

    def _commit_changes(changes, state) -> None:
        touched_keys, new_cargo, new_merch, per_origin = state
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

    def _relay_scan() -> bool:
        """Reroute a crop flow through an intermediate village that forwards it.

        Two ways relay pays. It *moves* commitment off a village that cannot
        staff its own haul onto one with spare capacity -- the feasibility case,
        first key of the objective. And in the ceiling-waste regime it lowers
        the TOTAL: each send's partial-vehicle waste ``batch mod capacity`` is
        multiplied by ``sets_in_flight``, and pooling part-loads onto one trunk
        eliminates it. An earlier version of this docstring claimed the second
        case was impossible, from a linearity argument that only holds when
        batches dwarf capacity; a sweep confined to that regime then "confirmed"
        it. The acceptance test below never depended on the claim -- moves are
        adopted whenever the objective strictly falls, whichever key improves.

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
            # Both ends must be outside the relay graph, not just the origin.
            # Guarding only the origin let a leg that *ends* at an existing hub
            # extend the chain, producing depth-3 waterfalls like 2 -> 6 -> 1 -> 3
            # that the beat's collect-then-ship ordering was never designed for.
            if origin in relay_hubs or destination in relay_hubs:
                continue
            amount = legs.get((origin, destination), 0.0)
            if amount <= EPSILON:
                continue
            # Score every candidate hub and take the best one. Accepting the
            # first that merely improves picks by village id, which is arbitrary
            # and measurably bad: it sent a 96-field haul through the
            # lowest-numbered village when four nearer ones would have done.
            best = None
            for hub in hubs:
                if hub in (origin, destination):
                    continue
                # Never create a two-way crop pair. A 2-cycle is not a relay: it
                # makes "ship after you collect" unsatisfiable at both ends
                # simultaneously, so no schedule can honour it.
                if (hub, origin) in legs or (destination, hub) in legs:
                    continue
                # Relay moves the whole flow, so (origin, destination) goes away.
                prospective = (_crop_edges() - {(origin, destination)}) | {
                    (origin, hub),
                    (hub, destination),
                }
                if not _crop_shape_ok(prospective):
                    continue
                changes = [
                    ((origin, destination), Resource.CROP, -amount),
                    ((origin, hub), Resource.CROP, amount),
                    ((hub, destination), Resource.CROP, amount),
                ]
                delta, state = _score_changes(changes)
                if delta >= (0, 0, 0, 0):
                    continue
                # Tie-break on the collection leg: when two hubs cost the same,
                # the nearer one is the better place to send cargo, and it keeps
                # the choice stable rather than falling back on village id.
                key = (delta, one_way_cache.get((origin, hub), 0.0), hub)
                if best is None or key < best[0]:
                    best = (key, hub, changes, state)
            if best is not None:
                _, hub, changes, state = best
                _commit_changes(changes, state)
                relay_hubs.add(hub)
                return True
        return False

    def _swap_changes(resource, o1, d1, o2, d2, t):
        return [
            ((o1, d1), resource, -t),
            ((o2, d2), resource, -t),
            ((o1, d2), resource, t),
            ((o2, d1), resource, t),
        ]

    def _swap_shape_ok(resource, legs, o1, d1, o2, d2, t) -> bool:
        # Swaps are blind to relay: rewiring a hub's legs can lengthen the chain
        # or close a loop, either of which the beat cannot then schedule.
        # Checked on the prospective edge set, never applied-then-undone.
        if resource is not Resource.CROP or not relay_hubs:
            return True
        prospective = _crop_edges() | {(o1, d2), (o2, d1)}
        for key in ((o1, d1), (o2, d2)):
            if legs.get(key, 0.0) - t <= EPSILON:
                prospective.discard(key)
        return _crop_shape_ok(prospective)

    def _breakpoint_ts(resource, o1, d1, o2, d2, t_full):
        grows = [
            (sum(pair.get(key, {}).values()), capacities[key[0]]) for key in ((o1, d2), (o2, d1))
        ]
        shrinks = [
            (sum(pair.get(key, {}).values()), capacities[key[0]]) for key in ((o1, d1), (o2, d2))
        ]
        return breakpoint_candidates(grows, shrinks, t_full, cycles)

    def _best_swap(refinement: bool):
        """One full sweep; return the single best improving swap, or None.

        Best-improvement, not first-improvement. An order-perturbation audit
        (reversed plus two seeded shuffles over 30 accounts) showed the previous
        first-improving-in-sorted-order scan was beaten by some other order in
        24 of 30 cases -- the scan order, which follows village ids, was quietly
        deciding which local optimum the search landed in. Scanning everything
        and taking the best removes that positional bias; ties break on the
        sorted candidate tuple, so determinism is preserved.

        ``refinement`` widens the neighbourhood to breakpoint transfer sizes.
        It ignores ``settled``, whose entries only assert that the FULL transfer
        does not improve, and it never adds to it for the same reason.
        """
        best = None
        for resource in sorted(flows, key=lambda r: r.value):
            legs = flows[resource]
            edges = sorted(key for key, amount in legs.items() if amount > EPSILON)
            for i, (o1, d1) in enumerate(edges):
                for o2, d2 in edges[i + 1 :]:
                    if not refinement and (resource, (o1, d1), (o2, d2)) in settled:
                        continue
                    # o1 == d2 (or o2 == d1) would create a self-loop, whose zero
                    # travel time makes it cost zero merchants -- the most
                    # attractive move there is, silently deleting real delivery.
                    if o1 in (o2, d2) or d1 in (d2, o2):
                        continue
                    t_full = min(legs.get((o1, d1), 0.0), legs.get((o2, d2), 0.0))
                    if t_full <= EPSILON:
                        continue
                    ts = (
                        _breakpoint_ts(resource, o1, d1, o2, d2, t_full)
                        if refinement
                        else (t_full,)
                    )
                    improving = None
                    for t in ts:
                        changes = _swap_changes(resource, o1, d1, o2, d2, t)
                        delta, state = _score_changes(changes)
                        if delta >= (0, 0, 0, 0):
                            continue
                        if not _swap_shape_ok(resource, legs, o1, d1, o2, d2, t):
                            continue
                        if improving is None or delta < improving[0]:
                            improving = (delta, changes, state)
                    if improving is None:
                        if not refinement:
                            settled.add((resource, (o1, d1), (o2, d2)))
                        continue
                    # Deterministic tie-break: the sorted scan order itself.
                    if best is None or improving[0] < best[0]:
                        best = improving
        return best

    converged = False
    for _pass in range(max_passes):
        move = _best_swap(refinement=False)
        if move is not None:
            _delta, changes, state = move
            _commit_changes(changes, state)
            continue
        # Swaps are exhausted. Relay is tried only now, so a plan with no
        # over-budget village and no ceiling waste comes out identical to one
        # produced without the move at all.
        if _relay_scan():
            continue
        # Both exhausted at full transfer sizes: widen to breakpoint transfers.
        move = _best_swap(refinement=True)
        if move is not None:
            _delta, changes, state = move
            _commit_changes(changes, state)
            continue
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
    # Every message below names villages the way the operator does.
    names = {vid: village.name for vid, village in villages.items() if village.name}
    assignment: Assignment = {}
    shortfalls: list[Shortfall] = []

    for resource in sorted(resource_plans, key=lambda r: r.value):
        plan = resource_plans[resource]
        missing = {v.village_id for v in plan.villages} - set(villages)
        if missing:
            warnings.append(
                f"{resource.value}: no fetched state for "
                + ", ".join(village_label(vid, names) for vid in sorted(missing))
                + "; excluded from routing"
            )
        flows, resource_shortfalls = _flows_for_resource(plan, villages, geometry)
        shortfalls.extend(resource_shortfalls)
        assignment[resource] = {key: amount for key, amount in flows.items() if amount > EPSILON}

    # Reassign the greedy seed to cut merchants and relieve over-budget villages
    # wherever a cheaper routing exists. Never worse than the seed on
    # (excess, merchants) AT THIS STAGE -- the latency pass afterwards spends
    # idle merchants on speed deliberately, so the end-to-end guarantee is
    # per-phase: excess never rises anywhere; the merchant total is minimal here
    # and may rise later, strictly within per-village budgets (§8.3, §14).
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
                f"route {village_label(route.origin, names)} -> "
                f"{village_label(route.destination, names)} has "
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
