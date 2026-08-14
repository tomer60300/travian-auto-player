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

First pass, per profile section 14: greedy nearest-sender matching, no hub
consolidation and no local improvement. It is deterministic, explainable, and a
baseline the later hub pass has to beat. What it does *not* do is claim
optimality -- a village over its merchant budget is reported, never hidden.
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


@dataclass(frozen=True)
class OverBudget:
    """A village asked to staff more merchants than it has."""

    village_id: int
    committed: int
    available: int

    @property
    def excess(self) -> int:
        return self.committed - self.available


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
    pair_cargo: dict[tuple[int, int], dict[Resource, float]] = {}
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
        for pair, amount in flows.items():
            pair_cargo.setdefault(pair, {})[resource] = amount

    routes: list[Route] = []
    committed: dict[int, int] = {vid: 0 for vid in villages}

    for origin, destination in sorted(pair_cargo):
        cargo = pair_cargo[(origin, destination)]
        hourly_total = sum(cargo.values())
        if hourly_total <= EPSILON:
            continue

        sender = villages[origin]
        one_way = geometry.one_way_minutes(sender.coords, villages[destination].coords)
        capacity = merchant_model.capacity(sender.trade_office_level)
        cost = cheapest_cycle(hourly_total, 2.0 * one_way, capacity, cycles)

        route = Route(
            origin=origin,
            destination=destination,
            cargo_per_hour=dict(sorted(cargo.items(), key=lambda kv: kv[0].value)),
            cycle_hours=cost.cycle_hours,
            merchants_per_send=cost.merchants_per_send,
            sets_in_flight=cost.sets_in_flight,
            one_way_minutes=one_way,
        )
        routes.append(route)
        committed[origin] += route.merchants_committed

        if max_latency_hours is not None and route.latency_hours > max_latency_hours:
            warnings.append(
                f"route {origin} -> {destination} has {route.latency_hours:.1f}h "
                f"latency against a {max_latency_hours:.0f}h target; geometry may "
                f"forbid better"
            )

    over_budget = tuple(
        OverBudget(
            village_id=vid,
            committed=used,
            available=villages[vid].spare_merchants(merchant_reserve),
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
