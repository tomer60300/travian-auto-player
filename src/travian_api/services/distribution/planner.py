"""Orchestration: fetched account state in, setup sheet out.

This is the only module that knows the order of operations. Everything it calls
is pure, so a plan is reproducible from its inputs and none of it needs a
session:

    state + targets -> allocation -> routing -> beat -> integer cargo -> sheet

The account is a moving target -- villages are added, production changes between
runs -- so nothing is cached across calls and no village list is assumed. Every
run plans from the snapshot it is given.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .allocation import EPSILON, Allocation, Resource, ResourcePlan, resolve_resource
from .geometry import MapGeometry
from .merchants import CEIL_DUST_TOLERANCE, DAILY_BEAT_CYCLES, MerchantModel
from .optimizer import (
    DEFAULT_MERCHANT_RESERVE,
    MAX_IMPROVE_PASSES,
    MAX_RELAY_HOPS,
    MIN_SEND_FILL,
    OverBudget,
    Plan,
    Shortfall,
    VillageState,
    build_plan,
)
from .rounding import round_preserving_total
from .schedule import DEFAULT_MIN_ARRIVAL_GAP_MINUTES, Beat, build_beat


@dataclass(frozen=True)
class PlannerConfig:
    """Everything tunable, in one place. No account facts belong here."""

    geometry: MapGeometry
    merchant_model: MerchantModel
    merchant_reserve: int = DEFAULT_MERCHANT_RESERVE
    cycles: Sequence[int] = DAILY_BEAT_CYCLES
    max_latency_hours: float | None = 2.0
    min_arrival_gap_minutes: int = DEFAULT_MIN_ARRIVAL_GAP_MINUTES
    reserved_window: tuple[int, int] | None = None
    dispatch_window: tuple[int, int] | None = None
    """Hours of the day this route set actually runs, when it is one allocation
    profile's rather than the whole day's. Sends are phased into it; left None
    the beat may use any minute, which is what a round-the-clock set wants."""
    min_send_fill: float = MIN_SEND_FILL
    """How full a merchant must stay when idle merchants are spent on speed.

    The latency/fill trade-off dial: lower it and routes get faster on emptier
    merchants, raise it and they stay full but slower. It materially moves the
    plan, so it belongs here rather than buried as a module constant.
    """
    max_improve_passes: int = MAX_IMPROVE_PASSES
    max_relay_hops: int = MAX_RELAY_HOPS
    """Levels of crop relay allowed; 0 ships everything direct."""


@dataclass(frozen=True)
class SheetRow:
    """One line of the setup sheet, ready to copy into the game."""

    origin: int
    destination: int
    cargo: Mapping[Resource, int]
    cycle_hours: int
    dispatch_minute: int
    arrival_minute: int
    merchants: int
    one_way_minutes: float = 0.0

    @property
    def total_cargo(self) -> int:
        return sum(self.cargo.values())

    @property
    def first_delivery_hours(self) -> float:
        """WORST-CASE hours from creating the route to its first delivery landing.

        A Gold Club route sends at its scheduled ``dispatch_minute`` ("Send at")
        time, so the first delivery lands at the next occurrence of that time plus
        travel. The longest that can be — the route created just after its send
        time — is one whole cycle plus travel, which is what this returns. It is
        an upper bound on the manual-coverage window, not a fixed startup: create
        the route shortly before its send time and the first crop lands in roughly
        travel time. The steady-state figures elsewhere on the sheet assume a full
        batch already waiting and merchants on the road, neither true on day one.
        """
        return self.cycle_hours + self.one_way_minutes / 60.0

    def dispatch_clock(self) -> str:
        return f"{self.dispatch_minute // 60:02d}:{self.dispatch_minute % 60:02d}"

    def arrival_clock(self) -> str:
        return f"{self.arrival_minute // 60:02d}:{self.arrival_minute % 60:02d}"


@dataclass(frozen=True)
class DistributionPlan:
    """The complete result of a planning run."""

    rows: tuple[SheetRow, ...] = ()
    merchants_committed: Mapping[int, int] = field(default_factory=dict)
    spare_merchants: Mapping[int, int] = field(default_factory=dict)
    resource_plans: Mapping[Resource, ResourcePlan] = field(default_factory=dict)
    routing: Plan = field(default_factory=Plan)
    beat: Beat = field(default_factory=Beat)
    warnings: tuple[str, ...] = ()

    @property
    def is_feasible(self) -> bool:
        # Routing feasibility is not enough: an allocation that over-claims the
        # account drives the remainder village's target negative, which the
        # optimizer will faithfully route as if the village could ship more than
        # it produces. That sheet is unsustainable and must not read as feasible.
        return self.routing.is_feasible and not self.over_allocated

    @property
    def over_allocated(self) -> tuple[Resource, ...]:
        """Resources whose explicit allocations exceed production (the remainder
        village would have to ship more than it makes). Diagnostic-only rows may
        still be shown, but the plan is not executable."""
        return tuple(
            resource
            for resource, rp in self.resource_plans.items()
            if rp.remainder_village_id is not None and rp.unallocated < -EPSILON
        )

    @property
    def over_budget(self) -> tuple[OverBudget, ...]:
        return self.routing.over_budget

    @property
    def shortfalls(self) -> tuple[Shortfall, ...]:
        return self.routing.shortfalls

    @property
    def total_merchants(self) -> int:
        return sum(self.merchants_committed.values())

    def free_merchants(self, village_id: int) -> int:
        return self.spare_merchants.get(village_id, 0) - self.merchants_committed.get(village_id, 0)


def craft_plan(
    villages: Mapping[int, VillageState],
    productions: Mapping[Resource, Mapping[int, float]],
    allocations: Mapping[Resource, Mapping[int, Allocation]],
    config: PlannerConfig,
) -> DistributionPlan:
    """Plan resource distribution for whatever account state is supplied.

    Args:
        villages: fetched per-village state, keyed by village id.
        productions: per resource, village id -> own net production per hour.
            Crop may be negative.
        allocations: per resource, village id -> allocation. Resources absent
            here are simply not planned; villages absent within a resource keep
            what they produce.
        config: tunables.

    Returns:
        A :class:`DistributionPlan` carrying the setup sheet, the merchant
        budget per village, and every warning, shortfall and over-budget village
        raised along the way. Nothing is dropped to make the plan look clean.
    """
    resource_plans: dict[Resource, ResourcePlan] = {}
    warnings: list[str] = []
    # Warnings are read by a person, so they name villages the way that person
    # does. Derived here rather than passed in: the caller already gave us the
    # villages, and a second source of names could disagree with the first.
    names = {vid: village.name for vid, village in villages.items() if village.name}

    for resource in sorted(productions, key=lambda r: r.value):
        plan = resolve_resource(
            resource, productions[resource], allocations.get(resource, {}), names
        )
        resource_plans[resource] = plan
        warnings.extend(plan.warnings)

    routing = build_plan(
        villages,
        resource_plans,
        config.geometry,
        config.merchant_model,
        merchant_reserve=config.merchant_reserve,
        cycles=config.cycles,
        max_latency_hours=config.max_latency_hours,
        min_send_fill=config.min_send_fill,
        max_improve_passes=config.max_improve_passes,
        max_relay_hops=config.max_relay_hops,
    )
    warnings.extend(routing.warnings)

    beat = build_beat(
        routing.routes,
        min_arrival_gap_minutes=config.min_arrival_gap_minutes,
        reserved_window=config.reserved_window,
        names=names,
        dispatch_window=config.dispatch_window,
    )
    warnings.extend(beat.warnings)

    rows = tuple(
        SheetRow(
            origin=scheduled.route.origin,
            destination=scheduled.route.destination,
            # Integer cargo, summing to the same ceil(batch) the merchant
            # budget in route_cost was sized for — round(sum) would ship one
            # resource less than budgeted on every cycle when the fraction
            # falls below .5.
            cargo=round_preserving_total(
                scheduled.route.batch_per_resource,
                target_total=math.ceil(
                    sum(scheduled.route.batch_per_resource.values()) - CEIL_DUST_TOLERANCE
                ),
            ),
            cycle_hours=scheduled.route.cycle_hours,
            dispatch_minute=scheduled.dispatch_minute,
            arrival_minute=scheduled.first_arrival_minute,
            # Per SEND: the row describes one Gold Club route definition. The
            # total commitment (x sets in flight) lives in the budget section.
            merchants=scheduled.route.merchants_per_send,
            one_way_minutes=scheduled.route.one_way_minutes,
        )
        for scheduled in beat.routes
    )

    return DistributionPlan(
        rows=rows,
        merchants_committed=dict(routing.merchants_committed),
        spare_merchants={
            vid: village.spare_merchants(config.merchant_reserve)
            for vid, village in villages.items()
        },
        resource_plans=resource_plans,
        routing=routing,
        beat=beat,
        warnings=tuple(warnings),
    )
