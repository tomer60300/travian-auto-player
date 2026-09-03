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

from .allocation import EPSILON, Allocation, Resource, ResourcePlan, resolve_resource, village_label
from .findings import Category, Finding, Severity
from .geometry import MapGeometry
from .merchants import CEIL_DUST_TOLERANCE, DAILY_BEAT_CYCLES, MerchantModel
from .night_profile import is_night_window
from .npc import NpcPolicy, NpcReserve, derive_reserves, draw_allowance
from .optimizer import (
    DEFAULT_MERCHANT_HEADROOM,
    DEFAULT_MERCHANT_RESERVE,
    MAX_IMPROVE_PASSES,
    MAX_RELAY_HOPS,
    MIN_SEND_FILL,
    OverBudget,
    Plan,
    RelayHub,
    Shortfall,
    VillageState,
    build_plan,
    merchant_ceiling_clause,
    relay_findings,
)
from .rounding import round_preserving_total
from .schedule import DEFAULT_MIN_ARRIVAL_GAP_MINUTES, Beat, build_beat, time_relays


@dataclass(frozen=True)
class PlannerConfig:
    """Everything tunable, in one place. No account facts belong here."""

    geometry: MapGeometry
    merchant_model: MerchantModel
    merchant_reserve: int = DEFAULT_MERCHANT_RESERVE
    merchant_headroom: float = DEFAULT_MERCHANT_HEADROOM
    """Fraction of each village's merchant budget to aim to leave uncommitted, so
    load spreads rather than piling onto whichever village is cheapest to ship
    from. Soft: exceeding it is reported, never fatal."""
    cycles: Sequence[int] = DAILY_BEAT_CYCLES
    max_latency_hours: float | None = 2.0
    min_arrival_gap_minutes: int = DEFAULT_MIN_ARRIVAL_GAP_MINUTES
    reserved_window: tuple[int, int] | None = None
    dispatch_window: tuple[int, int] | None = None
    """Hours of the day this route set actually runs, when it is one allocation
    profile's rather than the whole day's. Sends are phased into it; left None
    the beat may use any minute, which is what a round-the-clock set wants."""
    excluded_origins_by_destination: Mapping[int, set[int]] = field(default_factory=dict)
    """Villages that must not supply a given destination, by destination id.

    A denylist rather than a distance rule, because any distance threshold would
    be arbitrary and the villages worth excluding are the ones whose merchants are
    wanted elsewhere -- which distance cannot tell you. Needed once a destination
    has a cadence: an hourly cycle commits one merchant per send in flight, so a
    supplier eight hours away spends nine merchants on that route however little
    it carries, and the optimiser has no way to know that is a bad trade."""
    max_cycle_by_destination: Mapping[int, int] = field(default_factory=dict)
    """Longest cycle a route to this destination may use, by destination id.

    The optimiser satisfies a RATE: 47,167 crop an hour is met by 47,167 hourly
    and equally by 377,336 every eight hours, and it prefers the latter because
    it commits fewer merchants. For a store those are the same; for an ally being
    fed they are not, and nothing else could say so.

    Bounding the candidate cycles is enough, because every piece of cycle
    machinery reads its candidates from one sequence. Cadence is bought with
    merchants -- a one-hour cycle over a seven-hour round trip keeps seven sends
    in the air where an eight-hour cycle keeps one -- so this makes that trade
    explicit rather than hiding it."""
    prune_to_window: bool = False
    """Whether the executor will delete the rows that depart outside the window.

    A plan-time input because it changes what the plan MEANS. Travian fans a
    repeat interval across the whole day and offers nothing to confine it, so
    without pruning a windowed profile ships roughly a day of cargo through an
    eight-hour window -- a critical finding. With pruning those rows are deleted
    after creation, the window is genuinely enforced, and the same fact becomes a
    note about a dependency rather than a defect."""
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
    """The BUDGET each village was planned to, per :meth:`VillageState.merchant_budget`
    -- its fleet less the account reserve, or its own ``max_busy_merchants``
    where the operator set a lower one. Named for the fleet figure it used to be
    and kept that way because it is what the response field is called; read it
    as the ceiling, not as merchants standing idle in the marketplace."""
    resource_plans: Mapping[Resource, ResourcePlan] = field(default_factory=dict)
    routing: Plan = field(default_factory=Plan)
    beat: Beat = field(default_factory=Beat)
    findings: tuple[Finding, ...] = ()
    relays: tuple[RelayHub, ...] = ()
    """Villages the plan routes crop THROUGH, which the sheet's rows cannot show.
    Timed from the beat, so these figures are what the schedule will really do."""
    npc: Mapping[int, NpcReserve] = field(default_factory=dict)
    """Section 7's conversion budget per village, as the two-pass solve sized it.

    Carried on the plan because everything downstream needs the SAME figure the
    allocation layer was given: the storage replay tops stores up out of it, the
    trigger report measures against its floor, and the response exposes it. A
    second derivation could disagree with the first."""

    @property
    def warnings(self) -> tuple[str, ...]:
        """The findings as the flat prose list every caller has always read."""
        return tuple(f.message for f in self.findings)

    @property
    def is_feasible(self) -> bool:
        """Whether this sheet can be carried out. NOT whether it is a good idea.

        Routing feasibility is not enough: an allocation that over-claims the
        account drives the remainder village's target negative, which the
        optimizer will faithfully route as if the village could ship more than
        it produces. That sheet is unsustainable and must not read as feasible.

        What this deliberately does NOT weigh is anything about the *outcome* --
        stores overflowing, granaries running dry, a tribute going unpaid. Those
        are facts about the account the plan is being asked to run on, and a plan
        that leaves them in place is still perfectly executable. Vetoing on them
        would break a deliberate stockpile. See :func:`assess`, which says out
        loud both what this weighed and what it did not.
        """
        return self.routing.is_feasible and not self.over_allocated and not self.npc_short

    @property
    def npc_short(self) -> tuple[Finding, ...]:
        """Villages whose cargo needs more NPC conversion than they can fund.

        Weighed by :attr:`is_feasible` rather than left to the operator, because
        section 7's rule is to fail loudly: those routes WILL arrive short, and
        unlike an overflow that is not a fact about the account the plan is
        allowed to leave in place -- it is the plan promising cargo that does
        not exist. Kept separate from :attr:`over_allocated` because a resource
        with no remainder village over-claims without that reporting it.
        """
        return tuple(f for f in self.findings if f.category is Category.NPC_CAPACITY_SHORT)

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


# What ``is_feasible`` weighs, in the operator's words. Carried on every verdict
# so a green light cannot be read as "and nothing else is wrong": these three
# things, and only these three.
FEASIBILITY_COVERS: tuple[str, ...] = (
    "every origin stays inside its merchant budget",
    "every receiver's demand can be supplied by some village",
    "no resource is allocated beyond what the account produces",
    "no village is asked for more NPC conversion than its feedstock can fund",
)

# The critical categories ``is_feasible`` has an opinion about. Every other
# critical finding is real AND unweighed: a plan can destroy resources all day
# and still be perfectly executable, so those belong in `unweighed` rather than
# in a veto. These two are not about the account's outcome but about the sheet
# promising cargo that does not exist.
_WEIGHED_CRITICALS: frozenset[Category] = frozenset(
    {Category.OVER_ALLOCATED, Category.NPC_CAPACITY_SHORT}
)


@dataclass(frozen=True)
class Verdict:
    """``is_feasible``, with its own limits stated.

    ``executable`` is the old boolean unchanged -- and it is still what
    ``/execute`` gates on, because everything it weighs genuinely prevents the
    sheet from working. ``clean`` is the separate question the page was silently
    answering with it: nothing critical is outstanding either.
    """

    executable: bool
    blockers: tuple[str, ...] = ()
    """Why not, in full. Empty when ``executable``."""

    covers: tuple[str, ...] = FEASIBILITY_COVERS
    unweighed: tuple[Category, ...] = ()
    """Critical findings this answer did not consider, worst-ranked first."""

    critical_findings: int = 0
    """How many, not how many kinds: ``unweighed`` is deduplicated, this is not."""

    @property
    def clean(self) -> bool:
        """Executable, and nothing critical left outside what was weighed.

        The only condition that earns a green light. A plan losing 2.4M/day is
        executable and not clean, which is exactly the distinction the single
        boolean could not draw.
        """
        return self.executable and not self.unweighed


def blockers(plan: DistributionPlan, names: Mapping[int, str] | None = None) -> tuple[str, ...]:
    """Every reason this plan cannot be carried out, named. Empty when it can.

    Separate from :func:`assess` because ``/execute`` needs exactly this and
    nothing else: it has no reason to build a verdict whose `unweighed` and
    `critical_findings` it cannot populate honestly.
    """
    reasons: list[str] = []
    for over in plan.over_budget:
        # Whose ceiling it is, where the record knows: "its budget allows 8" of
        # a 19-merchant village is a figure the operator can find nowhere in
        # the game, and this tuple is what /execute refuses with. Same clause
        # as the budget explanation, from the same helper.
        clause = merchant_ceiling_clause(over.max_busy, over.fleet_spare)
        said = clause if clause is not None else f"its budget allows {over.available}"
        reasons.append(
            f"{village_label(over.village_id, names)} commits {over.committed} merchants but {said}"
        )
    for short in plan.shortfalls:
        # `short.reason`, not a second hardcoded "no village has spare": the
        # optimizer distinguishes a genuine lack of surplus from an exclusion
        # list that put the surplus out of reach, and restating only the first
        # sends the operator hunting production they already have.
        reasons.append(
            f"{village_label(short.village_id, names)} needs {short.per_hour:,.0f} "
            f"{short.resource.value}/h: {short.reason}"
        )
    for resource in plan.over_allocated:
        reasons.append(
            f"{resource.value} allocations claim more than the account produces, so the "
            f"remainder village would have to ship what it does not have"
        )
    # The finding's own message, not a second phrasing of it: it already names
    # the village, the resource and the gap, and /execute refuses with this
    # tuple. Two wordings of one refusal is how they come to disagree.
    reasons.extend(finding.message for finding in plan.npc_short)
    return tuple(reasons)


def assess(
    plan: DistributionPlan,
    findings: Sequence[Finding],
    names: Mapping[int, str] | None = None,
) -> Verdict:
    """Say what feasibility decided, and what it left to the operator.

    ``findings`` is the COMPLETE list, including the ones computed outside
    :func:`craft_plan` -- overflow, starvation, busy merchants. Those are exactly
    the findings the gate does not weigh, so a verdict built from
    ``plan.findings`` alone would report an empty ``unweighed`` and be the same
    lie in a longer sentence. Checked rather than trusted, because passing the
    short list is the one mistake that makes this function assert the opposite of
    the truth -- a destructive plan reported as clean.
    """
    absent = sum(1 for finding in plan.findings if finding not in findings)
    if absent:
        raise ValueError(
            f"assess() needs the complete finding list; {absent} of the plan's own "
            f"findings are missing from it, and the ones computed outside craft_plan "
            f"(overflow, starvation, busy merchants) are exactly what `unweighed` reports"
        )
    criticals = [f for f in findings if f.severity is Severity.CRITICAL]
    # dict.fromkeys keeps first-seen order, which is producer order -- the same
    # order summarise() ranks by, so the worst reads first here too.
    unweighed = tuple(
        dict.fromkeys(f.category for f in criticals if f.category not in _WEIGHED_CRITICALS)
    )

    return Verdict(
        executable=plan.is_feasible,
        blockers=blockers(plan, names),
        unweighed=unweighed,
        critical_findings=len(criticals),
    )


def craft_plan(
    villages: Mapping[int, VillageState],
    productions: Mapping[Resource, Mapping[int, float]],
    allocations: Mapping[Resource, Mapping[int, Allocation]],
    config: PlannerConfig,
    npc: NpcPolicy | None = None,
    consumption: Mapping[Resource, Mapping[int, float]] | None = None,
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
        npc: section 7's balancing declaration -- which villages hold a buffer
            level, whether the operator is at the marketplace during these
            hours, and any explicit feedstock override. Account state, not a
            tunable, which is why it sits here and not on
            :class:`PlannerConfig`. The caller supplies the buffer LEVEL (only
            it knows the warehouse capacity); the conversion RATE is derived
            here, from what the first pass says each village retains.
        consumption: per resource, village id -> what the village SPENDS per
            hour. Account state for the same reason the supplement is, and the
            operator's own figure: nothing in the game reports it, because the
            statistics page shows materials GROSS. It moves each village's
            ``net_per_hour`` and nothing else -- not the targets, not the
            remainder, not the cargo.

    Returns:
        A :class:`DistributionPlan` carrying the setup sheet, the merchant
        budget per village, and every warning, shortfall and over-budget village
        raised along the way. Nothing is dropped to make the plan look clean.
    """
    findings: list[Finding] = []
    # Warnings are read by a person, so they name villages the way that person
    # does. Derived here rather than passed in: the caller already gave us the
    # villages, and a second source of names could disagree with the first.
    names = {vid: village.name for vid, village in villages.items() if village.name}

    spend = consumption or {}
    ordered = sorted(productions, key=lambda r: r.value)

    def solve(
        allowances: Mapping[Resource, Mapping[int, float]],
    ) -> dict[Resource, ResourcePlan]:
        return {
            resource: resolve_resource(
                resource,
                productions[resource],
                allocations.get(resource, {}),
                names,
                npc_allowance=allowances.get(resource),
                consumption=spend.get(resource),
            )
            for resource in ordered
        }

    # Section 7 needs two passes, and there is no way around it: how much a
    # village can convert INTO wood is what it retains of clay and crop, and
    # what it retains of clay and crop is an output of the solve. So pass one
    # runs with no conversion at all, its retention sizes each village's budget,
    # and pass two spends that budget against the demand pass one could not
    # meet. Measured at ~1.6s -> 3.2s on 40 villages, and accepted: the
    # alternative is deriving the budget from a store LEVEL, which is the model
    # that made a shorter window raise the claim.
    #
    # Only when a floor is actually declared. With none, `solve({})` is the
    # single pass the planner has always been.
    policy = npc or NpcPolicy()
    reserves: dict[int, NpcReserve] = {}
    if policy.is_declared:
        first = solve({})
        retention = {
            resource: {v.village_id: v.target_per_hour for v in plan.villages}
            for resource, plan in first.items()
        }
        reserves, npc_findings = derive_reserves(policy, retention, names)
        findings.extend(npc_findings)
        # Every floored village gets an entry under every material so the
        # declaration is visible to the solve -- but a village whose rate for a
        # material could not be read is not in that resource's production map at
        # all, and `resolve_resource` rightly refuses an allowance for a village
        # it is not planning. Filtered here, where both maps are in hand, rather
        # than by loosening that refusal.
        allowances = {
            resource: {
                vid: cap for vid, cap in per_village.items() if vid in productions.get(resource, {})
            }
            for resource, per_village in draw_allowance(reserves, retention).items()
        }
        resource_plans = solve(allowances)
    else:
        resource_plans = solve({})
    for resource in ordered:
        findings.extend(resource_plans[resource].findings)

    # Section 6: the standing latency target is a DAY rule and does not apply
    # inside the night window. Overnight nothing is spent, so a delivery nobody
    # is waiting for costs nothing by being six hours old -- and the pass that
    # enforces the target buys speed with merchants (shorter cycles, more sets
    # in flight), which is the opposite of what the night needs: every merchant
    # has to be home by 07:00. What bounds a night cycle instead is getting home
    # and not overflowing, both of which the beat and the replay measure.
    #
    # None is exactly "no target": it also skips `_spend_idle_merchants_on_latency`
    # and the LATENCY findings, which is the whole of what the rule is.
    latency_target = None if is_night_window(config.dispatch_window) else config.max_latency_hours

    routing = build_plan(
        villages,
        resource_plans,
        config.geometry,
        config.merchant_model,
        merchant_reserve=config.merchant_reserve,
        merchant_headroom=config.merchant_headroom,
        cycles=config.cycles,
        max_latency_hours=latency_target,
        min_send_fill=config.min_send_fill,
        max_improve_passes=config.max_improve_passes,
        max_relay_hops=config.max_relay_hops,
        max_cycle_by_destination=config.max_cycle_by_destination,
        excluded_origins_by_destination=config.excluded_origins_by_destination,
    )
    findings.extend(routing.findings)

    beat = build_beat(
        routing.routes,
        min_arrival_gap_minutes=config.min_arrival_gap_minutes,
        reserved_window=config.reserved_window,
        names=names,
        dispatch_window=config.dispatch_window,
        prune_to_window=config.prune_to_window,
    )
    findings.extend(beat.findings)

    # Re-timed against the schedule that was actually built: `relay_chains` can
    # only estimate a leg's wait from its cycle length, which is wrong by up to
    # most of a day inside a profile window (the beat drops firings outside it).
    #
    # The window is passed ONLY when the executor will prune to it, the same
    # gate `_storage_findings` applies to the same schedule. Travian fans a
    # repeat interval across the whole day and offers nothing to confine it, so
    # without pruning every firing outside the window really happens -- and
    # `time_relays` filters each route's sends to the window, dropping exactly
    # the firings that make the wait short. Measured on a two-leg relay through
    # a one-hour window: 53h of relay latency reported where the truthful worst
    # case is a couple of hours, which is advice to buy merchants for a wait
    # that is not there.
    relays = time_relays(
        beat, routing.relays, config.dispatch_window if config.prune_to_window else None
    )
    # The same target, so the end-to-end relay figure cannot be judged against a
    # rule the legs themselves were exempt from.
    findings.extend(relay_findings(relays, names, latency_target, villages))

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
                # Resources are not interchangeable and this cargo repeats
                # unchanged on every departure, so a share lost to rounding is
                # lost forever rather than averaged away. See round_preserving_total.
                min_each=1,
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
        # The budget the plan was actually built to, so `free_merchants` and the
        # sheet's spare column cannot report room the optimizer was forbidden to
        # use. Where the operator has capped a village this is that cap.
        spare_merchants={
            vid: village.merchant_budget(config.merchant_reserve)
            for vid, village in villages.items()
        },
        resource_plans=resource_plans,
        routing=routing,
        beat=beat,
        findings=tuple(findings),
        relays=relays,
        npc=reserves,
    )
