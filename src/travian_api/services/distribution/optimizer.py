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
  receiver of lumber cannot also send lumber, so ``A -> B -> C`` cannot form
  *by netting alone*. Crop is deliberately exempt: relay through a sub-hub is
  permitted there, and restricting the relay MOVE to crop is what keeps the
  searched plan free of material chains.

  **Amended for profile section 5's declared tier.** The rule is now: no
  material village both sends and receives EXCEPT a village the operator
  DECLARED as a relay (``VillageState.relay_for``), and no relay feeds a relay.
  Section 5 states that 02 holds the reserved wood, may reach only its own
  neighbours, and hands the onward distribution to a tier drawn from that
  neighbour set -- so the one hop is the operator's instruction, not a search
  result, and :func:`_relay_tier_flows` builds its two legs by construction
  outside the improvement search. Where nothing is declared the old rule holds
  unchanged and for the old reason.

Both are asserted as invariants in the tests rather than defended with runtime
checks, because the property comes from the data model, not from vigilance here
-- and the declared exception is enforced at the schema, where the operator can
be told which village and which role made their declaration impossible.

Three stages, per profile section 14 (``cluster -> assign -> improve``):

1. **Greedy seed** -- :func:`_flows_for_resource` matches each receiver to its
   nearest senders, largest demand first. Deterministic and explainable, but
   order-dependent and blind to merchant cost.
2. **Merchant-aware local search** -- :func:`_improve_flows` reassigns that seed
   with 2x2 swaps, keeping only moves that strictly lower the lexicographic
   objective ``(over_budget_excess, total_merchants + SOFT_BUDGET_PRICE x
   soft_excess, route_count)``: hard feasibility first, then the merchant total
   with crowding past each village's soft cap priced into it, so spreading load
   is worth a bounded number of merchants and never a feasible plan. It never
   returns a plan worse than the seed, and cross-resource bundling falls out of
   costing the *merged* pair cargo.
3. **Latency pass** -- :func:`_spend_idle_merchants_on_latency` then hands each
   village's *idle* merchants (those the SOFT budget allows but the
   merchant-minimal plan left unused -- never the headroom reserve) to the
   routes furthest over the latency target, shortening
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

5. **Declared material relay** -- :func:`_relay_tier_flows`, which is not part
   of the search at all. Whatever the direct pass could not reach is served over
   two legs through the villages the operator named, and the result is merged
   into the assignment after :func:`_improve_flows` has finished. It runs on
   materials only, because crop already has the searched relay above.

What it still does *not* do is claim global optimality (the problem is NP-hard,
section 14) or DISCOVER a material relay -- a material hop has to be declared.
A village over its merchant budget is reported, never hidden.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from .allocation import EPSILON, MATERIALS, AllocationMode, Resource, ResourcePlan, village_label
from .findings import Category, Finding
from .geometry import MapGeometry
from .merchants import DAILY_BEAT_CYCLES, MerchantModel, cheapest_cycle, cycle_sweep
from .roles import Role, default_may_relay

DEFAULT_MERCHANT_RESERVE = 2

# Fraction of each village's merchant budget the plan aims to leave uncommitted.
#
# The reserve above is a flat count held back before planning starts; this is a
# proportion held back *by the objective*, and the two answer different
# questions. The reserve says "never plan with the last two merchants". This says
# "do not run any single village at the redline while its neighbours idle".
#
# Why a soft cap rather than a smaller budget: exceeding it must not make a plan
# infeasible. A village genuinely can be the only one placed to serve a receiver,
# and refusing that plan outright would be worse than running it hot.
#
# 0.10 comes from a sweep over five real payloads (headroom x price, recorded
# in scratchpad/sweep_headroom.py), re-run after the latency pass was confined
# to the soft budget. Across those accounts it SAVES 6 merchants net while
# cutting villages at 90%-or-more from 11 to 3, worst single account +2.
# Headroom saving merchants is not a paradox: the redline villages were the
# mispriced, over-committed ones, and load moved off them is load costed
# correctly. Larger settings are not reliably better -- 0.15 had one account pay
# +9 (+7.5%), the local search landing in a worse optimum rather than a real
# price. On an 18-merchant budget 0.10 holds back 2, which together with the
# flat reserve leaves 4 idle -- one ordinary extra route.
DEFAULT_MERCHANT_HEADROOM = 0.10

# What one merchant of over-the-soft-cap load is worth, in merchants of total
# fleet. The soft cap is folded into the merchant term at this rate rather than
# ranked above it, because ranking it above makes the price unbounded: measured
# across five real payloads, an unbounded soft key paid +8 merchants (+5.0%) on
# one account for no reduction in peak utilisation at all, the villages above the
# cap being structurally forced there.
#
# 1 is the unbiased rate: a merchant above the soft cap simply counts twice, once
# in the fleet total and once as crowding. The sweep found no reason to pay more
# -- at 2 and 3 the same headroom cost strictly more merchants on four of five
# accounts (night_payload: +2 at price 1, +4 at 2, +8 at 3, with identical peak
# utilisation), which is the search buying headroom it cannot actually deliver.
SOFT_BUDGET_PRICE = 1

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
    # The village's OWN crop production, before any allocation. Carried solely
    # so relay can refuse a hub that cannot survive its inbound leg slipping,
    # WHERE NOTHING WAS DECLARED: a village whose troops eat more than it grows
    # drains while it forwards, and a relay leg turns a slow loss into a dead
    # village. ``None`` means the rate could not be read, which is NOT the same
    # as zero. Superseded by ``role`` once there is one -- see
    # :func:`_may_relay_through`.
    crop_per_hour: float | None = None

    role: Role | None = None
    """What this village is FOR, when the operator has said. ``None`` is the
    account nobody has described yet, and every decision that reads this must
    keep working on it -- see :func:`_may_relay_through`."""

    may_relay: bool | None = None
    """Whether this village may forward someone else's cargo, overriding both
    its role's default and the crop-sign inference. ``None`` takes
    :func:`~.roles.default_may_relay` where there is a role, so a role village
    need not restate what its role already says, and leaves the crop sign to
    decide where there is not."""

    relay_for: tuple[int, ...] | None = None
    """Villages this one forwards the capital's MATERIAL on to (section 5's tier).

    Declared, never discovered. Section 5 does not ask the planner to find a
    relay tier; it says one exists, constrains where it is drawn from (02's own
    neighbour set) and forbids a role village from being in it. So this is the
    operator's sentence, and the planner builds the two legs by construction --
    ``source -> this village`` sized to the sum of these downstreams' unmet
    material demand, and ``this village -> each downstream`` sized to that
    downstream's own gap. Nothing searches for a material hub.

    ``None`` -- every account today -- plans exactly as before, and the
    no-waterfall rule holds for materials by construction as it always did. A
    tuple rather than a list because :class:`VillageState` is frozen and hashed.
    """

    max_busy_merchants: int | None = None
    """The most merchants this village may have underway or returning at once.

    The operator's own ceiling (profile section 5: "maximum 8 busy at 02",
    counting the relay leg), which nothing in the game states. It is measured in
    the unit the plan already commits merchants in -- section 8's
    ``merchants_per_send x sets_in_flight``, which is
    :attr:`Route.merchants_committed` -- so it needs no arithmetic of its own,
    only somewhere to be said.

    A CAP, not a reserve, and the two differ wherever the fleet is not a full
    20: a village with 19 merchants capped at 8 busy is not the same village as
    one holding 12 back. ``None`` is every account that has not said, and plans
    exactly as before."""

    @property
    def coords(self) -> tuple[int, int]:
        return (self.x, self.y)

    def spare_merchants(self, reserve: int = DEFAULT_MERCHANT_RESERVE) -> int:
        """What the FLEET can field: the merchants it has, less the reserve."""
        return max(0, self.merchant_count - reserve)

    def merchant_budget(self, reserve: int = DEFAULT_MERCHANT_RESERVE) -> int:
        """What the PLAN may commit here: the fleet, or the operator's cap.

        The tighter of the two, always. A cap above the fleet is not a promise
        of merchants the village does not have, and the reserve keeps applying
        underneath one -- so a 20-merchant village capped at 20 still plans on
        18.
        """
        spare = self.spare_merchants(reserve)
        if self.max_busy_merchants is None:
            return spare
        return min(spare, self.max_busy_merchants)


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
class RelayHub:
    """A village the plan routes a resource THROUGH, and what that costs in time.

    The unit is the hub, not a path. Given two origins shipping crop in and two
    destinations being forwarded to, the finished flow graph proves that the hub
    forwards crop -- it does not prove which origin's crop reaches which
    destination, because the cargo is pooled in the hub's granary. Reporting the
    four origin->hub->destination combinations as four deliveries invents a
    provenance the plan never chose, and on one audited account turned 6 real
    hubs into 41 claimed chains.

    Derived from the finished route set rather than recorded while the search
    runs, and deliberately so: a hub can also be created by a swap rewiring an
    existing leg, so the search's own bookkeeping is not the whole truth. A
    village that both receives and sends crop IS a hub, whatever produced it.

    Only two-hop relays are modelled, which is all the optimizer can currently
    build: ``_crop_shape_ok`` forbids an edge between two hubs, so no
    A->B->C->D waterfall can form regardless of ``max_relay_hops``. If that ever
    changes, a deeper chain would surface here as one hub appearing among
    another's origins, which the tests assert against.
    """

    hub: int
    origins: tuple[int, ...]
    """Villages shipping the resource into the hub."""

    destinations: tuple[int, ...]
    """Villages the hub forwards it on to."""

    collect_hours: float
    """Longest from production at any origin to arrival at the hub."""

    forward_hours: float
    """Longest from arrival at the hub to arrival at any destination."""

    resource: Resource = Resource.CROP
    """Which resource is being relayed through this hub.

    Crop by default because for a long time crop was the only answer: netting
    left every village a sender or a receiver of a material and never both, so
    a material hub could not be expressed. Section 5's DECLARED tier is the
    exception, and a village can be a hub for a material and for crop at once --
    two different pools of cargo, two different waits -- so the resource is part
    of the hub's identity rather than a property of the village.
    """

    @property
    def end_to_end_hours(self) -> float:
        """Worst case for crop passing through this hub: both waits in turn.

        The cargo sits at its origin until that route's next send, travels, then
        sits in the hub's granary until the forwarding route's next send, and
        travels again. Checking the legs separately -- which is all the plan used
        to do -- can pass two 1.5h legs and never mention the 3h delivery.

        An upper bound over every path through the hub, like every other latency
        figure on the sheet. It is a real schedule's worst case rather than a
        phase-independent one, because :func:`~.schedule.time_relays` re-derives
        both waits from the beat that was actually built.
        """
        return self.collect_hours + self.forward_hours


def _cycles_for(
    destination: int,
    cycles: Sequence[int],
    max_cycle: Mapping[int, int] | None,
) -> Sequence[int]:
    """Candidate cycles for a route to *destination*, honouring its cadence cap.

    Filtering here rather than at every decision point is deliberate: both the
    initial choice and the idle-merchant latency pass read their candidates from
    one sequence, so bounding the sequence bounds every cycle this destination can
    ever be given.

    A cap that excludes everything is ignored rather than obeyed. Returning an
    empty sequence would make cheapest_cycle raise mid-plan, and a cadence
    preference is not worth failing a whole plan for -- the route simply keeps the
    shortest cycle available.
    """
    if not max_cycle:
        return cycles
    cap = max_cycle.get(destination)
    if cap is None:
        return cycles
    allowed = [c for c in cycles if c <= cap]
    return allowed or [min(cycles)]


def relay_hubs(
    routes: Iterable[Route], *, material_relays: Collection[int] = ()
) -> tuple[RelayHub, ...]:
    """The relay hubs in *routes*, worst end-to-end first.

    Crop always, because the optimizer may reroute a crop flow through a sub-hub
    (profile section 3.5). Materials only at a village the operator DECLARED as
    a relay, which is what ``material_relays`` carries: absent it, a material
    arriving somewhere that also ships that material out is two independent
    flows, and reporting it as a relay would invent a dependency the plan does
    not have. That is not merely a display choice -- the beat phases a hub's
    forward sends after its collecting arrivals, and doing that to two unrelated
    flows would constrain a schedule for no reason.

    So ``material_relays`` empty reproduces the crop-only behaviour exactly,
    which is every account that declares no tier.

    The hours here are estimated from cycle lengths, which assumes a route fires
    all day. :func:`~.schedule.time_relays` replaces them with what the finished
    beat will really do -- and must, because inside a profile window it will not.
    """
    declared = frozenset(material_relays)
    hubs = []
    # Crop first, then the materials in the game's order, so the pre-tier output
    # is untouched and a declared tier's rows sort after it inside a tie.
    for resource in (Resource.CROP, *MATERIALS):
        carrying = [
            r
            for r in routes
            if r.cargo_per_hour.get(resource, 0.0) > EPSILON and r.origin != r.destination
        ]
        inbound: dict[int, list[Route]] = {}
        outbound: dict[int, list[Route]] = {}
        for route in carrying:
            inbound.setdefault(route.destination, []).append(route)
            outbound.setdefault(route.origin, []).append(route)

        for hub in sorted(set(inbound) & set(outbound)):
            if resource is not Resource.CROP and hub not in declared:
                continue
            collecting = inbound[hub]
            feeders = {route.origin for route in collecting}
            forwarding = [route for route in outbound[hub] if route.destination not in feeders]
            if not forwarding:
                # Every onward leg goes straight back to a village that feeds
                # this one. That is a two-way pair, not a relay: no schedule can
                # satisfy "ship after you collect" at both ends at once, and the
                # optimizer refuses to create one. It holds for a declared
                # material tier identically -- see `_relay_tier_flows`, which
                # will not name a downstream that supplies its own relay.
                continue
            hubs.append(
                RelayHub(
                    hub=hub,
                    origins=tuple(sorted(feeders)),
                    destinations=tuple(sorted({route.destination for route in forwarding})),
                    collect_hours=max(route.latency_hours for route in collecting),
                    forward_hours=max(route.latency_hours for route in forwarding),
                    resource=resource,
                )
            )
    # Worst first, as every other finding list is ordered. The village id and
    # then the resource only break exact ties, and only for display: nothing
    # about the plan depends on this order.
    return tuple(
        sorted(hubs, key=lambda relay: (-relay.end_to_end_hours, relay.hub, relay.resource.value))
    )


def _named(village_ids: Iterable[int], names: Mapping[int, str]) -> str:
    """Village names for a message, abridged once the list stops being readable."""
    labels = [village_label(vid, names) for vid in village_ids]
    if len(labels) <= 3:
        return ", ".join(labels)
    return ", ".join(labels[:3]) + f" and {len(labels) - 3} more"


def relay_findings(
    hubs: Iterable[RelayHub],
    names: Mapping[int, str],
    max_latency_hours: float | None,
    villages: Mapping[int, VillageState] | None = None,
) -> list[Finding]:
    """Report every relay hub: as a warning when the delivery misses the target.

    A compliant relay is still reported, as a note. Two rows on the sheet that
    are really one delivery is a fact about the plan an operator typing them into
    the game needs, target or no target -- and on the account that motivated
    this, 45 of 66 plans used a relay and not one line of output said so.

    With ``villages`` the hub's own crop production is named too. Relay refuses a
    hub below zero outright, but a hub at +1/h passes that floor and is nothing
    like one at +4,500/h: it breaks even on paper and recovers from a missed
    refill at a crawl. The floor is not a judgement about margin, so the margin is
    shown rather than silently accepted.
    """
    findings: list[Finding] = []
    for relay in hubs:
        hub = village_label(relay.hub, names)
        origins = _named(relay.origins, names)
        destinations = _named(relay.destinations, names)
        # Named only when known, and only for CROP: the figure is the hub's own
        # crop balance, which is what the relay-solvency floor is about. Printing
        # it beside a lumber pass-through would answer a question nobody asked
        # -- a declared material relay's own risk is its WAREHOUSE holding the
        # pass-through, which is `RELAY_BUFFER`, not its granary.
        own = None if villages is None else villages.get(relay.hub)
        margin = (
            ""
            if relay.resource is not Resource.CROP or own is None or own.crop_per_hour is None
            else f", growing {own.crop_per_hour:+,.0f}/h of its own"
        )
        over_target = (
            max_latency_hours is not None and relay.end_to_end_hours > max_latency_hours + EPSILON
        )
        if over_target:
            message = (
                f"{relay.resource.value} relayed through {hub} takes up to "
                f"{relay.end_to_end_hours:.1f}h "
                f"end-to-end against a {max_latency_hours:.0f}h target: at worst "
                f"{relay.collect_hours:.1f}h in from {origins}, then "
                f"{relay.forward_hours:.1f}h waiting there and travelling on to "
                f"{destinations}{margin}"
            )
        else:
            message = (
                f"{hub} relays {relay.resource.value}: it forwards to {destinations} what it "
                f"collects from {origins}, so those rows are legs of one delivery taking up "
                f"to {relay.end_to_end_hours:.1f}h{margin}"
            )
        findings.append(
            Finding(
                category=Category.RELAY_LATENCY if over_target else Category.RELAY,
                detail=f"via {hub} — {relay.end_to_end_hours:.1f}h",
                message=message,
                # Grouped by resource, so a declared material tier and a crop
                # sub-hub read as two subjects rather than one mixed count.
                resource=relay.resource,
                village=hub,
            )
        )
    return findings


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

    max_busy: int | None = None
    """The operator's own ceiling here, where one was set.

    Carried on the record rather than left to each reader to recompute, because
    two surfaces explain a breach -- `/plan`'s per-village explanation and the
    refusal `blockers` renders into `/execute`'s 422 body -- and ``available``
    alone cannot say whose number it is. "its budget allows 8" of a village
    fielding 19 merchants names a figure nothing in the game reports."""

    fleet_spare: int | None = None
    """What the FLEET could have fielded: its merchants less the account
    reserve. The cap is only worth naming against this. ``None`` where nothing
    recorded it."""

    @property
    def excess(self) -> int:
        return self.committed - self.available


def merchant_ceiling_clause(max_busy: int | None, fleet_spare: int | None) -> str | None:
    """ "you capped it at N busy at once; its fleet could otherwise spare M".

    ``None`` when the cap is not what binds -- unset, or no tighter than what
    the fleet could field, in which case the fleet is the story and saying
    otherwise blames the operator for the geometry.

    STRICTLY tighter, because on equality the two ceilings are the same number:
    20 merchants, a reserve of 8 and a cap of 12 produced "you capped it at 12
    busy at once; its fleet could otherwise spare 12", which blames the
    operator for a limit the reserve imposed.

    One function because two surfaces render it: the budget explanation on
    `/plan` and the blockers `/execute` refuses with. A cap explained two ways
    is a cap the operator has to reconcile.
    """
    if max_busy is None or fleet_spare is None or max_busy >= fleet_spare:
        return None
    return (
        f"you capped it at {max_busy} busy at once; its fleet could otherwise spare {fleet_spare}"
    )


def _trade_office_levels_needed(
    village: VillageState,
    routes_from: Sequence[Route],
    merchant_model: MerchantModel,
    budget: int,
    cycles: Sequence[int],
    max_cycle: Mapping[int, int] | None = None,
) -> int | None:
    """Smallest Trade Office increase that brings *village* within *budget*.

    Each route is re-costed under its OWN destination's cadence cap. Advising
    from the unrestricted cycle set let the advice quietly switch a 1h-capped
    route to a 2h cycle nobody is allowed to create: on a 3,000/h haul with a
    one-hour trip and a 1h cap, two merchant sets are needed at ANY Trade Office
    level, and the unrestricted maths still recommended +5.
    """
    for delta in range(1, MAX_TRADE_OFFICE_LEVEL - village.trade_office_level + 1):
        capacity = merchant_model.capacity(village.trade_office_level + delta)
        needed = sum(
            cheapest_cycle(
                route.hourly_total,
                2.0 * route.one_way_minutes,
                capacity,
                _cycles_for(route.destination, cycles, max_cycle),
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
    findings: tuple[Finding, ...] = ()
    # The relay hubs hiding in `routes`. Carried explicitly because a caller
    # rendering the sheet cannot see them: their legs are ordinary rows.
    relays: tuple[RelayHub, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        """The findings as the flat prose list every caller has always read."""
        return tuple(f.message for f in self.findings)

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
    *,
    names: Mapping[int, str] | None = None,
    excluded: Mapping[int, set[int]] | None = None,
) -> tuple[dict[tuple[int, int], float], list[Shortfall]]:
    """Match receivers to their nearest senders, largest demand first.

    Largest-first keeps the awkward villages from being left with only distant
    surplus, and iterating in a sorted order makes the result deterministic --
    a re-plan on unchanged input must produce an identical route set, or the
    diff against the live configuration is meaningless.

    `names` and `excluded` are keyword-only because `names` was added AHEAD of
    the older `excluded`: both are optional mappings keyed by village id, so an
    old-shape call passing the exclusion map fourth bound it to `names` and
    routed as if nothing were excluded -- no error, just a plan that quietly
    ignores the operator's list. Keyword-only makes that a TypeError.
    """
    # Merchant-capable only. A route needs merchants at its origin, and Travian
    # grants those through the Marketplace -- so a freshly settled village, or one
    # whose Marketplace is not built, has a real surplus and no way to move any of
    # it. Excluding it here is what keeps the invariant asserted before the plan
    # is emitted actually true; without this filter such a village reached route
    # construction and raised, which is a 500 to the operator instead of a plan
    # that reports the receiver as short.
    surplus: dict[int, float] = {
        v.village_id: -v.ship_per_hour
        for v in plan.senders
        if v.village_id in villages and villages[v.village_id].merchant_count > 0
    }
    demand = sorted(
        (v for v in plan.receivers if v.village_id in villages),
        key=lambda v: (-v.ship_per_hour, villages[v.village_id].coords),
    )

    flows: dict[tuple[int, int], float] = {}
    shortfalls: list[Shortfall] = []

    for receiver in demand:
        remaining = receiver.ship_per_hour
        # Suppliers the operator ruled out for THIS destination. Nothing here can
        # work out that nine merchants in flight to move 3,930 crop an hour is a
        # bad trade -- it is minimising merchants across the whole plan and has no
        # way to know those nine are wanted elsewhere. That judgement belongs to
        # whoever runs the account.
        banned = (excluded or {}).get(receiver.village_id, frozenset())
        candidates = sorted(
            (
                vid
                for vid, left in surplus.items()
                if left > EPSILON and vid != receiver.village_id and vid not in banned
            ),
            key=lambda vid: (
                geometry.distance(villages[vid].coords, villages[receiver.village_id].coords),
                villages[vid].coords,
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
            # WHY it could not be routed, not just that it could not. The loop
            # above ran out of candidates, and there are two quite different
            # reasons for that with two different fixes: the account genuinely
            # has no surplus left, or it has some and the operator's own lists
            # put it out of reach. Told only the first, an operator whose
            # `ship_only_to` or `exclude_origins` caused the shortfall goes
            # looking for production they already have. `banned` was computed
            # and discarded; this keeps the distinction.
            #
            # Keyed off `banned` and not off "somebody still has surplus":
            # a village filtered out for having no Marketplace is nobody's
            # exclusion and must not send the operator to edit a list.
            withheld = [
                vid
                for vid in banned
                if surplus.get(vid, 0.0) > EPSILON and vid != receiver.village_id
            ]
            reason = (
                "no village has surplus left to cover this demand"
                if not withheld
                else (
                    "every village with surplus left is excluded from this destination "
                    "by ship_only_to or exclude_origins: "
                    + ", ".join(village_label(vid, names) for vid in sorted(withheld))
                )
            )
            shortfalls.append(
                Shortfall(
                    village_id=receiver.village_id,
                    resource=plan.resource,
                    per_hour=remaining,
                    reason=reason,
                )
            )
    return flows, shortfalls


# ---------------------------------------------------------------------------
# The DECLARED material relay tier (profile section 5)
# ---------------------------------------------------------------------------


def _relay_tier_flows(
    plan: ResourcePlan,
    flows: Mapping[tuple[int, int], float],
    shortfalls: Sequence[Shortfall],
    relay_for: Mapping[int, Sequence[int]],
    villages: Mapping[int, VillageState],
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    budgets: Mapping[int, int],
    cycles: Sequence[int],
    *,
    names: Mapping[int, str] | None = None,
    excluded: Mapping[int, set[int]] | None = None,
    max_cycle: Mapping[int, int] | None = None,
) -> tuple[dict[tuple[int, int], float], list[Shortfall]]:
    """Build section 5's two legs for whatever the direct pass could not reach.

    Returns ``(relay_flows, shortfalls)`` -- the tier's own edges, and the
    shortfall list with everything the tier now covers removed. Both are empty
    of change when nothing was declared or nothing was short, which is what
    keeps an undeclared account byte-identical.

    **Sized by construction, never searched.** The forward leg to each
    downstream carries that downstream's own unmet gap; the collecting leg into
    each relay carries the sum of its downstreams' gaps. There is no hub search
    and no objective term: section 5 already decided the shape, and the only
    open question -- which relay serves which defensive village -- is answered
    by the operator's own ``relay_for`` lists.

    **Each gap is sized ONCE.** A village named twice in one list, or claimed by
    two different relays, is still one village with one gap: ``unmet`` is
    decremented as the forward loop runs and the downstream set is a set. Both
    are refused at the schema too (see
    ``_relay_tier_is_one_hop_of_non_role_villages``), so this is the layer that
    keeps the arithmetic honest rather than the layer that tells the operator.
    Without it the collecting leg was drawn from an inflated total, the repeated
    village was shipped its whole target once per mention, and the downstream it
    displaced was reported unreachable with the whitelist as the reason.

    **The source is chosen the way every other origin is.** A relay becomes an
    ordinary receiver of the aggregated demand and the greedy rule picks its
    nearest sender with surplus left, honouring ``ship_only_to`` /
    ``exclude_origins`` through *excluded*. Hard-coding "the capital" was the
    alternative and is worse twice over: it needs a role to be declared before a
    tier can work, and it would ship from a village that has nothing left.

    **...but it prices the merchants first, because nothing downstream will.**
    Distance alone put a capped village over its budget while an affordable
    source of the same cargo stood one field further out -- measured at 4
    merchants committed against a cap of 2, with the plan reported infeasible.
    Every other flow in the plan gets a second chance at that: ``over_delta`` is
    the first key :func:`_improve_flows` minimises. The tier's legs are merged
    in AFTER the search, deliberately, so this is the only place the cap can be
    consulted at all.

    So the sort key is ``(merchants over budget, distance, coordinates)``.
    Distance still decides between two sources that can both afford the leg,
    which is what keeps the operator's own tier building the same legs it did.
    The excess is priced with :func:`_route_for_pair` -- the plan's own
    arithmetic, not a second formula -- against what this resource's direct
    legs already spend at that village. That is an UNDER-estimate on a
    multi-resource account, because pair-merging is not done yet and the other
    resources' legs are not visible from inside the per-resource loop. It is a
    preference and not a guarantee: every candidate still contributes its
    surplus until the demand is met, so this can only reorder candidates and
    never create a shortfall. A breach that survives it is still reported by
    ``over_budget``, which remains the authority.

    **Surplus is what the direct pass did not spend.** Recomputed here from the
    resource plan and the seeded flows rather than threaded out of
    :func:`_flows_for_resource`, so that function's signature -- and the audit's
    mutation stub that mirrors it -- stay as they are.

    Three things it will not do, each of which would break the one-hop rule the
    schema also enforces (belt and braces: the schema refuses the declaration,
    this refuses the edge):

    * draw a collecting leg from a village that is itself a declared relay --
      that is a relay feeding a relay;
    * name a downstream that is a declared relay, for the same reason;
    * create a two-way pair, where the collect leg's source is also a
      downstream. "Ship after you collect" is then unsatisfiable at both ends
      at once, which is the same refusal :func:`relay_hubs` and
      ``_crop_shape_ok`` make on the crop side.
    """
    relays = frozenset(relay_for)
    unmet = {
        s.village_id: s.per_hour
        for s in shortfalls
        if s.resource is plan.resource and s.per_hour > EPSILON
    }
    if not unmet:
        return {}, list(shortfalls)

    # What the direct pass left on the shelf, per sender. Merchant-capable only,
    # for the reason `_flows_for_resource` filters on it: a village with no
    # Marketplace has a real surplus and no way to move any of it.
    surplus: dict[int, float] = {
        v.village_id: -v.ship_per_hour
        for v in plan.senders
        if v.village_id in villages and villages[v.village_id].merchant_count > 0
    }
    for (origin, _destination), amount in flows.items():
        if origin in surplus:
            surplus[origin] -= amount

    # What this resource's direct legs already bill each candidate source, so a
    # collecting leg is weighed against the room actually left rather than
    # against the whole budget. Once per resource, not once per relay: the
    # direct pass is finished by the time this runs and nothing below changes
    # it.
    def leg_merchants(origin: int, destination: int, rate: float) -> int:
        return _route_for_pair(
            origin,
            destination,
            {plan.resource: rate},
            villages,
            geometry,
            merchant_model,
            cycles,
            max_cycle,
        ).merchants_committed

    spent: dict[int, int] = {}
    for (origin, destination), amount in flows.items():
        if origin in surplus and amount > EPSILON:
            spent[origin] = spent.get(origin, 0) + leg_merchants(origin, destination, amount)

    relay_flows: dict[tuple[int, int], float] = {}
    covered: dict[int, float] = {}
    for relay in sorted(relays):
        if relay not in villages or villages[relay].merchant_count <= 0:
            continue
        # Largest gap first, then coordinates, exactly as the direct pass orders
        # its receivers -- so a relay that cannot collect enough serves the
        # village in most need, and the choice does not depend on village ids.
        #
        # A SET, and filtered on what is still unmet rather than on membership:
        # both halves of "sized once" live here. A village named twice in one
        # list is one downstream, and `unmet` is decremented as the forward loop
        # runs, so a village an earlier relay already covered no longer asks for
        # anything. Without them `wanted` is inflated by every repeat, the
        # collecting leg is drawn that much bigger, and the forward loop hands
        # the same village its whole target once per mention.
        downstream = sorted(
            {
                vid
                for vid in relay_for[relay]
                if unmet.get(vid, 0.0) > EPSILON and vid not in relays
            },
            key=lambda vid: (-unmet[vid], villages[vid].coords),
        )
        wanted = sum(unmet[vid] for vid in downstream)
        if wanted <= EPSILON:
            continue
        banned = (excluded or {}).get(relay, frozenset())

        def over_budget(vid: int, relay: int = relay, wanted: float = wanted) -> int:
            """Merchants this source would commit beyond its remaining budget.

            Priced on what it could contribute if it went first, which is the
            only size that is defined before the order is known. Zero for every
            source that can afford the leg, so distance decides between them.
            """
            room = budgets.get(vid, 0) - spent.get(vid, 0)
            cost = leg_merchants(vid, relay, min(surplus[vid], wanted))
            return max(0, cost - room)

        sources = sorted(
            (
                vid
                for vid, left in surplus.items()
                if left > EPSILON
                and vid != relay
                and vid not in banned
                and vid not in relays
                and vid not in downstream
            ),
            key=lambda vid: (
                over_budget(vid),
                geometry.distance(villages[vid].coords, villages[relay].coords),
                villages[vid].coords,
            ),
        )
        collected = 0.0
        for source in sources:
            if collected >= wanted - EPSILON:
                break
            taken = min(surplus[source], wanted - collected)
            relay_flows[(source, relay)] = relay_flows.get((source, relay), 0.0) + taken
            surplus[source] -= taken
            collected += taken
        # Hand on exactly what arrived, in need order. A relay keeps nothing:
        # anything it banked would be an allocation nobody gave it.
        for vid in downstream:
            if collected <= EPSILON:
                break
            forwarded = min(unmet[vid], collected)
            relay_flows[(relay, vid)] = relay_flows.get((relay, vid), 0.0) + forwarded
            collected -= forwarded
            unmet[vid] -= forwarded
            covered[vid] = covered.get(vid, 0.0) + forwarded
        # The "keeps nothing" rule, enforced rather than described. `wanted` is
        # the sum of exactly these downstreams' remaining gaps and the loop
        # above hands each one `min(gap, collected)`, so everything collected
        # goes out again -- and if some later change breaks that agreement the
        # relay would quietly accumulate an allocation nobody gave it, which is
        # invisible in the route list and shows up days later as a full
        # warehouse. Cheap: once per relay per resource.
        if collected > EPSILON:
            raise AssertionError(
                f"relay {relay} collected {collected:,.0f}/h of {plan.resource.value} more "
                f"than its downstreams can take; the tier's collecting leg is sized from "
                f"gaps it cannot forward"
            )

    if not covered:
        return {}, list(shortfalls)

    # A partly-served shortfall shrinks to what is still missing and keeps its
    # original reason: the whitelist really is still why the rest cannot land.
    remaining: list[Shortfall] = []
    for s in shortfalls:
        if s.resource is not plan.resource or s.village_id not in covered:
            remaining.append(s)
            continue
        left = s.per_hour - covered[s.village_id]
        if left > EPSILON:
            remaining.append(
                Shortfall(
                    village_id=s.village_id,
                    resource=s.resource,
                    per_hour=left,
                    reason=s.reason,
                )
            )
    return relay_flows, remaining


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


# How idle a village must be before it counts as an alternative worth naming.
# Half its own soft cap: enough slack to take real work, not a village that is
# merely slightly less busy than the one straining.
_IDLE_SHARE = 0.5


def _crowding_findings(
    villages: Mapping[int, VillageState],
    routes: Sequence[Route],
    committed: Mapping[int, int],
    budgets: Mapping[int, int],
    soft_budgets: Mapping[int, int],
    geometry: MapGeometry,
    names: Mapping[int, str],
) -> list[Finding]:
    """Name each village over its headroom, and a better-placed idle village.

    "Better placed" means no further from the receiver the crowded village is
    straining on. That avoids inventing a distance threshold: a village with
    spare merchants that is no further from the work is genuinely an alternative,
    and one on the far side of the map is not, without anyone having to pick a
    number of fields.

    Reported, never acted on. The optimizer has already tried to spread this load
    and priced the attempt; these are the cases it could not fix, and hiding them
    is what let a village sit at 100% while its neighbours ran at 6%.
    """
    outbound: dict[int, list[Route]] = {}
    for route in routes:
        outbound.setdefault(route.origin, []).append(route)

    findings: list[Finding] = []
    for vid in sorted(committed):
        used = committed[vid]
        hard = budgets.get(vid, 0)
        soft = soft_budgets.get(vid, hard)
        # An empty band (headroom 0, or a budget too small to hold anything
        # back) means anything past the cap is already reported by OverBudget --
        # repeating it here as crowding is the duplication findings.py exists to
        # prevent.
        if soft <= 0 or soft >= hard or used <= soft:
            continue
        legs = outbound.get(vid)
        if not legs:
            continue
        # The leg it is straining on: the one holding the most merchants.
        worst = max(legs, key=lambda r: r.merchants_committed)
        strained = worst.destination
        if strained not in villages:
            continue  # a foreign tribute has no coordinates we can compare against
        own_reach = geometry.one_way_minutes(villages[vid].coords, villages[strained].coords)

        best: tuple[float, int] | None = None
        for other, other_used in committed.items():
            if other == vid or other not in villages:
                continue
            other_budget = budgets.get(other, 0)
            if other_budget <= 0:
                continue
            if other_used > _IDLE_SHARE * soft_budgets.get(other, other_budget):
                continue
            if not outbound.get(other):
                continue  # a pure receiver has nothing to ship; not an alternative
            reach = geometry.one_way_minutes(villages[other].coords, villages[strained].coords)
            if reach > own_reach:
                continue
            share = other_used / other_budget
            if best is None or share < best[0]:
                best = (share, other)
        if best is None:
            continue

        _, idle = best
        # "of its 8 merchants" claims the village HAS 8. Where the operator's
        # own cap is what set the budget it is their figure, not the fleet's --
        # village 26 fields 19 and was told it commits 12 "of its 8". On
        # `/day-check`, which carries no `budgets` field, this warning is the
        # only surface the breach has, so the number has to say whose it is.
        cap = villages[vid].max_busy_merchants
        ceiling = (
            f"the {hard} you allow" if cap is not None and cap == hard else f"its {hard} merchants"
        )
        findings.append(
            Finding(
                category=Category.MERCHANTS_CROWDED,
                message=(
                    f"{village_label(vid, names)} commits {used} of {ceiling}, past the "
                    f"{soft} it should keep clear, while "
                    f"{village_label(idle, names)} uses {committed[idle]} of "
                    f"{budgets.get(idle, 0)} and is no further from "
                    f"{village_label(strained, names)}"
                ),
                detail=(
                    f"{village_label(vid, names)} {used}/{hard}"
                    + (" (your cap)" if cap is not None and cap == hard else "")
                    + f" vs {village_label(idle, names)} {committed[idle]}/"
                    f"{budgets.get(idle, 0)}"
                ),
                village=village_label(vid, names),
            )
        )
    return findings


def _may_relay_through(village: VillageState) -> bool:
    """May *village* be made to forward someone else's crop?

    An unreadable rate is refused before anything else is consulted. A
    declaration says what a village is FOR; it does not say what its granary is
    doing, so it cannot answer a question about a balance nobody could read.
    With the role consulted first the refusal was bypassed entirely -- a feeder,
    or any template carrying ``may_relay: true``, forwarded someone else's crop
    out of an unknown balance, which is the optimistic reading of an unparsed
    rate in the one place this codebase has never taken it (see the ``None``
    paragraph below).

    The declared answer next, and ``may_relay`` is the most specific form of
    it: merged per VILLAGE over its role template's, so it can arrive with no
    role at all -- the account whose one defensive village sits on the only
    road to a corner of the map wants that village relaying, not all four.
    Read before the role, because a role supplies a DEFAULT and this is a
    statement about this village.

    Then the role. A village with one has been described by the operator, and
    profile section 5.9 answers this for each kind directly: a feeder moves
    resources on, every other role has a job that a leg in transit interferes
    with (see :func:`~.roles.default_may_relay`, which also carries why the
    capital is a *no*).

    The rule below is what to do when nothing has been declared -- which is
    most accounts, and stays exactly as it was. It is an INFERENCE, and the
    role case is not a special exemption from it but the reason it was ever
    needed: with no vocabulary for "01 is crop-negative by design", the sign of
    a crop rate was the only evidence available.

    Failing a declaration, then: only if it is not losing crop of its own. The
    danger is asymmetric, and that asymmetry is the entire rule: a hub with
    non-negative production cannot be harmed by relaying, because if the leg
    refilling it is late it simply forwards less. A hub already eating into its
    granary -- troops consuming more than the fields grow -- funds the relay
    from a balance that is falling, and a slipped inbound leg becomes an empty
    granary and starving troops.

    Observed on a live account before this existed: two of seven night chains
    forwarded through villages at -3,037/h and -874/h. The second held 32,597
    crop, forwarded 8,410/h, and its refill was a single route on an eight-hour
    cycle -- one missed send and it ran dry in three and a half hours.

    ``None`` is refused. An unreadable rate is not a zero one; the codebase
    already treats a rate it could not parse as CRITICAL rather than guessing,
    and a relay hub is the last place to take the optimistic reading.

    The threshold is ``>= 0`` rather than ``> 0`` deliberately. A village that
    breaks even holds its own when a refill is late, so excluding it would cost
    the canonical midway hub for no gain in safety.
    """
    if village.crop_per_hour is None:
        return False
    # The explicit answer before the role's, and before the inference. It is
    # merged per VILLAGE, so it can arrive without a role at all -- and the
    # crop-sign rule below is the fallback for a village nothing has been
    # declared about, which an explicit permission is not.
    if village.may_relay is not None:
        return village.may_relay
    if village.role is not None:
        # Resolved here rather than only at the edge, so a ``VillageState``
        # carrying a role and no explicit permission cannot leak ``None`` into a
        # boolean question: an unresolved role would read as "may not relay" by
        # accident, which is the right answer four times out of five and
        # therefore the hardest kind of bug to notice.
        return default_may_relay(village.role)
    return village.crop_per_hour >= 0.0


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
    max_cycle: Mapping[int, int] | None = None,
) -> Route:
    """Build the concrete :class:`Route` for one merged pair."""
    hourly_total = sum(cargo.values())
    one_way = geometry.one_way_minutes(villages[origin].coords, villages[destination].coords)
    capacity = merchant_model.capacity(villages[origin].trade_office_level)
    cost = cheapest_cycle(
        hourly_total, 2.0 * one_way, capacity, _cycles_for(destination, cycles, max_cycle)
    )
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
    max_cycle: Mapping[int, int] | None = None,
) -> list[Route]:
    """Shorten over-target routes by spending each village's idle merchants.

    Merchant minimisation drives routes to long cycles (a 24h cycle can save one
    merchant over a 3h one), so the cheapest plan is also the slowest — measured
    at a median 5.6h against a 2h target. Yet villages carry idle merchants the
    budget already allows. This pass hands those idle merchants to the routes
    that most need speed: for each village it repeatedly picks the affordable
    shorter cycle giving the best latency cut per merchant, spending strictly
    within ``budget - already_committed`` so a village can never be pushed over
    its cap. The caller decides which cap: build_plan hands in the SOFT budgets,
    so speed is bought only with merchants the headroom policy considers
    spendable -- never with the reserve the plan promised to leave uncommitted. Compliant routes (<= target) are left alone rather than
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
                for cost in cycle_sweep(
                    route.hourly_total,
                    2.0 * one_way,
                    capacity,
                    _cycles_for(route.destination, cycles, max_cycle),
                ):
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
                    # The last two keys are geometry, so a tie cannot fall to
                    # the route's position in an id-ordered list.
                    key = (
                        urgency,
                        compliant,
                        per_merchant,
                        -delta,
                        -cost.cycle_hours,
                        villages[route.origin].coords,
                        villages[route.destination].coords,
                    )
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
    # The utilisation the plan aims to stay under, per village. Defaults to the
    # hard budget, which makes the soft key identically zero and the objective
    # exactly what it was -- so a caller that does not care is unaffected.
    soft_budgets: Mapping[int, int] | None = None,
    # Cadence caps by destination. The search MUST see these: costing a pair with
    # a cycle the builder is not allowed to use optimises against a price nobody
    # will pay, and the error is not small -- a 1h cap on a long haul costs
    # roughly twice what an unrestricted search assumes.
    max_cycle: Mapping[int, int] | None = None,
    # Origins the operator has forbidden, per destination. The search MUST see
    # these for the same reason it must see the cadence caps: it picks senders,
    # and a sender it is not allowed to use is not a sender.
    excluded_origins: Mapping[int, set[int]] | None = None,
    # Villages the operator has put in the crop plan, and so the only ones relay
    # may conscript as forwarding hubs. Empty by default, which means no relay:
    # a caller that does not supply it gets direct routes rather than a search
    # quietly free to draft any village it likes.
    relay_hub_candidates: Collection[int] = (),
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

    The objective is lexicographic ``(over_budget_excess, total_merchants +
    SOFT_BUDGET_PRICE x soft_excess, route_count, cargo_weighted_round_trip)`` —
    feasibility first, then merchants (§8.3 objective 1) with crowding past the
    soft cap priced in at a fixed exchange rate, then route count (objective 4),
    then shorter hauls as the tie-break so equal-cost plans prefer nearer
    assignments. A move
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

    # The improvement search asks for the same (rate, trip, capacity) triple over
    # and over: measured, 92.4% of these calls repeat their arguments at 23
    # villages and 93.8% at 40, and cheapest_cycle is still the largest leaf in
    # the profile at ~43% of build_plan. The memo lives and dies inside this one
    # build_plan call, so nothing leaks between requests or between accounts.
    cost_memo: dict[tuple[int, float, float], int] = {}

    # Cadence caps, and the candidate cycles they leave, resolved once per
    # destination instead of on every one of the ~4M costing calls.
    caps: Mapping[int, int] = max_cycle or {}
    _allowed_cache: dict[int, Sequence[int]] = {}

    # The greedy seed filters candidate senders on this (:func:`_flows_for_resource`),
    # but the seed is not the only thing that chooses an origin: a 2x2 swap lands a
    # flow on ``(o1, d2)`` and a relay invents ``(hub, destination)``, and neither
    # pair was ever vetted. Live account, 2026-09-02: the operator excluded his
    # resource hub from a foreign tribute, the seed duly avoided it, and the swap
    # put it straight back -- a direct route from the one origin that was banned,
    # 10,266 crop/h on 9 merchants. The exclusion was worse than useless: it moved
    # the seed into a poorer basin and was then violated anyway.
    excluded: Mapping[int, set[int]] = excluded_origins or {}

    # `_may_send`, not the old `_origin_allowed`: that name sat three lines
    # above `allowed_for`, which answers an unrelated question (which CYCLES a
    # destination allows), and the pair read as two halves of one idea. This
    # matches `_may_relay_through`, the module's other permission predicate.
    def _may_send(origin: int, destination: int) -> bool:
        forbidden = excluded.get(destination)
        return not forbidden or origin not in forbidden

    def allowed_for(destination: int) -> Sequence[int]:
        got = _allowed_cache.get(destination)
        if got is None:
            got = _cycles_for(destination, cycles, max_cycle)
            _allowed_cache[destination] = got
        return got

    def merchants_for(origin: int, destination: int, hourly_total: float) -> int:
        # Takes the already-summed tonnage rather than the cargo mapping: every
        # caller has just summed it for the objective anyway, and summing twice
        # is not free at ~4M calls. It must stay the caller's own
        # `sum(cargo.values())` -- see the key comment below on why the exact
        # float matters.
        if hourly_total <= EPSILON:
            return 0
        one_way = one_way_cache.get((origin, destination))
        if one_way is None:
            one_way = geometry.one_way_minutes(
                villages[origin].coords, villages[destination].coords
            )
            one_way_cache[(origin, destination)] = one_way
        capacity = capacities[origin]
        # Keyed on the float rate itself, not a rounded one: the search lands
        # rates exactly on capacity boundaries on purpose, and rounding the key
        # would collapse two rates that genuinely cost different merchants.
        # The round trip MUST be in the key: it sets sets_in_flight, so the same
        # rate to a nearer village costs fewer merchants. Leaving it out returned
        # one destination's cost for another's.
        # The cadence cap MUST be in it too, for exactly the same reason: two
        # destinations the same distance away with different caps cost different
        # merchants, and sharing a cached price between them hands one the
        # other's. Keyed on the cap rather than the filtered cycle tuple because
        # equal caps imply equal candidates and an int hashes cheaper than a
        # tuple at ~4M calls.
        cap = caps.get(destination)
        key = (capacity, hourly_total, one_way, cap)
        hit = cost_memo.get(key)
        if hit is not None:
            return hit
        committed = cheapest_cycle(
            hourly_total, 2.0 * one_way, capacity, allowed_for(destination)
        ).merchants_committed
        cost_memo[key] = committed
        return committed

    def _one_way(key: FlowKey) -> float:
        cached = one_way_cache.get(key)
        if cached is None:
            cached = geometry.one_way_minutes(villages[key[0]].coords, villages[key[1]].coords)
            one_way_cache[key] = cached
        return cached

    pair = _merge_pair_cargo(flows)
    # Tonnage per pair, kept in sync by _commit_changes rather than re-summed.
    # The scored candidate's own sum is what gets stored, so the value here is
    # always bit-identical to `sum(pair[key].values())` -- the pair dicts are
    # replaced wholesale on commit, never mutated in place, so the summation
    # order that produced it cannot drift.
    pair_total: dict[FlowKey, float] = {key: sum(cargo.values()) for key, cargo in pair.items()}
    pair_merch: dict[FlowKey, int] = {
        key: merchants_for(key[0], key[1], total) for key, total in pair_total.items()
    }
    committed: dict[int, int] = {}
    for (origin, _destination), merchants in pair_merch.items():
        committed[origin] = committed.get(origin, 0) + merchants

    def excess(origin: int, count: int) -> int:
        return max(0, count - budgets.get(origin, 0))

    # Crowding, measured ONLY in the band between the soft cap and the hard one.
    #
    # Counting everything above the soft cap bills load over the HARD cap twice,
    # once here and once in `excess`, which is simply not what this term means:
    # merchants past the hard budget are infeasibility, not crowding.
    #
    # It is bookkeeping, not a behavioural fix, and the difference matters to
    # anyone reading this. Clamped and unclamped were compared over four real
    # payloads at three headrooms and produced identical plans in 12 of 12 cases.
    # That is structural rather than luck: `excess` is the FIRST key, so an extra
    # copy of it inside the second can never reorder two candidates -- when
    # `excess` differs it decides alone, and when it ties at zero the unclamped
    # term is zero as well. Kept because the honest formulation costs nothing and
    # a future reordering of the keys would make the double charge real.
    soft = soft_budgets if soft_budgets is not None else budgets

    def soft_excess(origin: int, count: int) -> int:
        hard = budgets.get(origin, 0)
        return max(0, min(count, hard) - soft.get(origin, hard))

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
    hub_ids: set[int] = set()

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

        All four objective terms are accumulated in ONE pass over the touched
        keys. It used to be six separate generator expressions over the same
        four keys, which at ~4M calls made this function 98% of the improvement
        search. The pass walks ``touched_keys`` in its own set order and adds the
        round-trip term left to right, exactly as the generator expression did:
        that term is a float sum, so its order decides the last bits, and it is
        rounded into an objective key that breaks ties between candidate swaps.
        Reordering it would be a silent re-plan, not a speed-up.
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

        new_merch: dict[FlowKey, int] = {}
        new_total: dict[FlowKey, float] = {}
        per_origin: dict[int, int] = {}
        total_delta = 0
        rc_delta = 0
        # Cargo-weighted round-trip, the final tie-break. Plans that tie on
        # excess, merchants and route count used to be tie-broken by whatever
        # move happened to be scanned first, which left the search actively
        # latency-blind: two equal-cost assignments where one hauls the cargo
        # twice as far were interchangeable. Rounded to an integer so the
        # objective stays a strictly-decreasing bounded integer tuple and
        # termination remains guaranteed.
        round_trip = 0.0
        for key in touched_keys:
            cargo = new_cargo[key]
            tonnage = sum(cargo.values())
            new_total[key] = tonnage
            merchants = merchants_for(key[0], key[1], tonnage)
            new_merch[key] = merchants
            was = pair_merch.get(key, 0)
            origin = key[0]
            per_origin[origin] = per_origin.get(origin, 0) + (merchants - was)
            total_delta += merchants - was
            if merchants > 0:
                rc_delta += route_weight(key)
            if was > 0:
                rc_delta -= route_weight(key)
            round_trip += (tonnage - pair_total.get(key, 0.0)) * 2.0 * _one_way(key)

        over_delta = sum(
            excess(origin, committed.get(origin, 0) + delta)
            - excess(origin, committed.get(origin, 0))
            for origin, delta in per_origin.items()
        )
        # Load spreading, priced INTO the merchant term at a fixed rate rather
        # than ranked above it. Ranked above, the price is unbounded and the
        # search will pay any number of merchants for a unit of headroom it may
        # not even be able to deliver; priced in, it spends up to
        # SOFT_BUDGET_PRICE and stops. Hard feasibility still outranks both, so
        # no amount of headroom can be bought with an over-budget village.
        soft_delta = sum(
            soft_excess(origin, committed.get(origin, 0) + delta)
            - soft_excess(origin, committed.get(origin, 0))
            for origin, delta in per_origin.items()
        )
        # Rounded to 6dp, not to whole minutes. Integer minutes made the last
        # key of the objective collide constantly, and a collision here was
        # settled by scan order -- which follows village ids. Relabelling the
        # villages of an identical account therefore moved the plan: measured at
        # 127 vs 141 merchants on one account, and on another a village flipped
        # between over-budget and within it. 6dp is far coarser than the ~1e-11
        # float dust these sums carry and far finer than a minute, so it
        # discriminates real differences without becoming unstable.
        return (
            over_delta,
            total_delta + SOFT_BUDGET_PRICE * soft_delta,
            rc_delta,
            round(round_trip, 6),
        ), (
            touched_keys,
            new_cargo,
            new_merch,
            per_origin,
            new_total,
        )

    def _commit_changes(changes, state) -> None:
        touched_keys, new_cargo, new_merch, per_origin, new_total = state
        for key in touched_keys:
            if new_cargo[key]:
                pair[key] = new_cargo[key]
                pair_total[key] = new_total[key]
            else:
                pair.pop(key, None)
                pair_total.pop(key, None)
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
        # A candidate is a village the operator gave a crop instruction, whether
        # or not that instruction leaves it carrying any crop --
        # ``relay_hub_candidates``, which :func:`build_plan` derives from the crop
        # allocations. The set has moved twice, and both earlier versions were
        # wrong in opposite directions:
        #
        # * Until 2026-09-02 candidates came from the crop FLOW graph, so the
        #   canonical midway hub of profile section 8.5 -- it grows exactly what
        #   it keeps, so it has an allocation and no leg -- could never be chosen,
        #   while the same village given any flow at all was found at once.
        # * The fix widened it to every village of the account, one village too
        #   far: a village nobody had said anything about was conscripted the
        #   moment a relay through it priced marginally cheaper. Measured on the
        #   safety fixture, adding ONE village with no crop allocation changed
        #   the hub and moved every leg -- on a live account, 27,000/h of someone
        #   else's crop into a granary nobody had sized for it.
        #
        # That second failure is known issue #10, and this is its answer. A
        # re-plan on unchanged input must give an identical route set, because
        # /execute acts on the DIFF against the live configuration: it deletes
        # and recreates real Gold Club rows from it, so a settle that reshuffles
        # untouched villages is destructive, not merely surprising. A settle adds
        # no allocation, so it adds no candidate, so nothing moves. The
        # allocation is also the CONSENT: naming a village in the crop plan --
        # even at an absolute target equal to its own production, which changes
        # nothing else about the plan -- is how the operator offers it as
        # infrastructure. KEEP is not such a naming: `resolve_resource` defaults
        # every village with a production figure to KEEP, and the route layer
        # drops explicit KEEP entries because they mean exactly what an absent
        # one means.
        #
        # Two guards then apply on top. A hub relays crop through itself, so it
        # must be a real village that can staff the extra leg: foreign sinks
        # (merchant_count == 0) are excluded, because including a sink let the
        # search adopt an impossible sink->sink leg (zero distance between two
        # co-located tributes costs zero merchants), emitting a route whose
        # origin is a negative foreign id. And a hub must be PERMITTED to relay
        # (see :func:`_may_relay_through`): where the operator declared a role,
        # that role says so directly (profile section 5.9 -- only a feeder
        # forwards); where nothing was declared the village must be solvent in
        # crop, because relaying through one already losing crop turns a slow
        # deficit into a dead village the first time its refill slips, and an
        # unreadable rate is refused there too -- unknown is not zero. Computed
        # once per scan, not per edge.
        hubs = sorted(
            vid
            for vid in relay_hub_candidates
            if vid in villages
            and villages[vid].merchant_count > 0
            and _may_relay_through(villages[vid])
        )
        # ONE best relay per scan, chosen across every (leg, hub) pair rather than
        # committing the first leg that happens to have an improving hub. Taking
        # the first leg in id order let the labelling decide which relay got
        # built, and widening the candidate set above made that bite: with more
        # hubs to find, more legs have an improving one. Every tie-break key
        # below is coordinates for the same reason.
        best = None
        for origin, destination in sorted(key for key, amount in legs.items() if amount > EPSILON):
            # Both ends must be outside the relay graph, not just the origin.
            # Guarding only the origin let a leg that *ends* at an existing hub
            # extend the chain, producing depth-3 waterfalls like 2 -> 6 -> 1 -> 3
            # that the beat's collect-then-ship ordering was never designed for.
            if origin in hub_ids or destination in hub_ids:
                continue
            amount = legs.get((origin, destination), 0.0)
            if amount <= EPSILON:
                continue
            for hub in hubs:
                if hub in (origin, destination):
                    continue
                # Relay invents ``(origin, hub)`` and ``(hub, destination)``.
                # The second is the dangerous one: it makes the hub a sender to
                # a destination that may forbid it, which is how a banned
                # village became a forwarding hub into a foreign tribute.
                if excluded and not (_may_send(origin, hub) and _may_send(hub, destination)):
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
                # Tie-break on the collection leg: when two relays cost the
                # same, the nearer hub is the better place to send cargo. Every
                # remaining key is COORDINATES -- the hub's, then the leg's two
                # ends' -- never ids: hub, origin and destination are village
                # ids, so a tie settled by them was settled by the arbitrary
                # numbering, and relabelling the account moved the plan.
                # Coordinates survive relabelling.
                key = (
                    delta,
                    one_way_cache.get((origin, hub), 0.0),
                    villages[hub].coords,
                    villages[origin].coords,
                    villages[destination].coords,
                )
                if best is None or key < best[0]:
                    best = (key, hub, changes, state)
        if best is not None:
            _, hub, changes, state = best
            _commit_changes(changes, state)
            hub_ids.add(hub)
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
        if resource is not Resource.CROP or not hub_ids:
            return True
        prospective = _crop_edges() | {(o1, d2), (o2, d1)}
        for key in ((o1, d1), (o2, d2)):
            if legs.get(key, 0.0) - t <= EPSILON:
                prospective.discard(key)
        return _crop_shape_ok(prospective)

    def _breakpoint_ts(resource, o1, d1, o2, d2, t_full):
        grows = [(pair_total.get(key, 0.0), capacities[key[0]]) for key in ((o1, d2), (o2, d1))]
        shrinks = [(pair_total.get(key, 0.0), capacities[key[0]]) for key in ((o1, d1), (o2, d2))]
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
                    # A swap names two senders the seed never vetted. Rejected
                    # here rather than after scoring: a forbidden pair must not
                    # be priced, let alone win on price.
                    if excluded and not (_may_send(o1, d2) and _may_send(o2, d1)):
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
                    # Tie-break on GEOGRAPHY, not on scan position.
                    #
                    # This used to be a bare `improving[0] < best[0]`, so the
                    # first candidate holding the minimal objective won -- and
                    # the scan runs over id-sorted edges. Ties are common, so
                    # the arbitrary village numbering was deciding the plan:
                    # relabelling an otherwise identical account moved it on 8
                    # of 16 audited accounts, once by 9.9% of total merchants,
                    # and on another flipped a village between over-budget and
                    # within it -- the figure the Trade Office advice is built
                    # from.
                    #
                    # Coordinates are the account's real geometry and survive
                    # relabelling, so a tie now falls to where the villages ARE.
                    # The two legs of a swap are interchangeable, hence the
                    # sort. Determinism is preserved and no longer depends on
                    # what the villages happen to be called.
                    geo = tuple(
                        sorted(
                            (
                                (villages[o1].coords, villages[d1].coords),
                                (villages[o2].coords, villages[d2].coords),
                            )
                        )
                    )
                    if best is None or (improving[0], geo) < (best[0], best[3]):
                        best = (improving[0], improving[1], improving[2], geo)
        return best

    converged = False
    for _pass in range(max_passes):
        move = _best_swap(refinement=False)
        if move is not None:
            _delta, changes, state, _geo = move
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
            _delta, changes, state, _geo = move
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
    merchant_headroom: float = DEFAULT_MERCHANT_HEADROOM,
    cycles: Sequence[int] = DAILY_BEAT_CYCLES,
    max_latency_hours: float | None = 2.0,
    min_send_fill: float = MIN_SEND_FILL,
    max_improve_passes: int = MAX_IMPROVE_PASSES,
    max_relay_hops: int = MAX_RELAY_HOPS,
    # Cadence, by destination. Empty by default, so a plan that does not care
    # about WHEN cargo lands is byte-identical to what it was.
    max_cycle_by_destination: Mapping[int, int] | None = None,
    # Suppliers ruled out per destination. Empty by default, so a plan that does
    # not care who supplies what is byte-identical to what it was.
    excluded_origins_by_destination: Mapping[int, set[int]] | None = None,
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
        merchant_headroom: fraction of each village's budget the plan aims to
            leave uncommitted, so load spreads instead of piling onto whichever
            village is cheapest. Soft: exceeding it is reported, never fatal.
            0.0 restores the pre-headroom behaviour exactly.
        cycles: candidate cycle lengths. Defaults to those dividing 24h so the
            schedule has a daily period.
        max_latency_hours: soft target; exceeding it warns rather than fails,
            because geometry can make it impossible.

    Returns:
        A :class:`Plan`. Over-budget villages and unroutable demand are reported
        in the plan, never silently dropped or quietly trimmed to fit.
    """
    findings: list[Finding] = []
    # Every message below names villages the way the operator does.
    names = {vid: village.name for vid, village in villages.items() if village.name}
    assignment: Assignment = {}
    shortfalls: list[Shortfall] = []
    # Profile section 5's DECLARED tier, read off the villages the way the
    # merchant cap and the relay permission are: one place, so the flow builder,
    # the hub report and the beat all consult the same lists.
    relay_for: dict[int, tuple[int, ...]] = {
        vid: village.relay_for for vid, village in villages.items() if village.relay_for
    }
    # The tier's own edges, kept OUT of the improvement search on purpose (see
    # where they are merged in below).
    tier: Assignment = {}
    # The operator's per-village cap is folded in HERE, once, so every reader of
    # the budget -- the declared tier's source choice, the improvement search,
    # the latency pass, the crowding report, the over-budget record -- is
    # measuring against the same ceiling. A cap read in some places and not
    # others is a plan that reports itself inside a budget it broke.
    #
    # Computed before the resource loop rather than after it because the tier is
    # built INSIDE that loop and has to consult it: the tier is the one part of
    # the plan the improvement search never revisits, so a source it picks
    # blind to the cap is a source that ships.
    budgets = {vid: villages[vid].merchant_budget(merchant_reserve) for vid in villages}

    for resource in sorted(resource_plans, key=lambda r: r.value):
        plan = resource_plans[resource]
        missing = {v.village_id for v in plan.villages} - set(villages)
        if missing:
            labels = [village_label(vid, names) for vid in sorted(missing)]
            findings.append(
                Finding(
                    category=Category.UNREADABLE_RATE,
                    message=(
                        f"{resource.value}: no fetched state for "
                        + ", ".join(labels)
                        + "; excluded from routing"
                    ),
                    detail=f"{resource.value}: " + ", ".join(labels),
                    resource=resource,
                )
            )
        flows, resource_shortfalls = _flows_for_resource(
            plan,
            villages,
            geometry,
            names=names,
            excluded=excluded_origins_by_destination,
        )
        # Section 5's tier, over whatever the direct pass could not reach. Only
        # materials: crop already relays through a sub-hub wherever the search
        # finds it worth doing, and giving crop a declared tier as well would
        # mean two mechanisms answering one question.
        if relay_for and resource in MATERIALS:
            tier_flows, resource_shortfalls = _relay_tier_flows(
                plan,
                flows,
                resource_shortfalls,
                relay_for,
                villages,
                geometry,
                merchant_model,
                budgets,
                cycles,
                names=names,
                excluded=excluded_origins_by_destination,
                max_cycle=max_cycle_by_destination,
            )
            if tier_flows:
                tier[resource] = tier_flows
        shortfalls.extend(resource_shortfalls)
        assignment[resource] = {key: amount for key, amount in flows.items() if amount > EPSILON}

    # Reassign the greedy seed to cut merchants and relieve over-budget villages
    # wherever a cheaper routing exists. Never worse than the seed on
    # (excess, merchants) AT THIS STAGE -- the latency pass afterwards spends
    # idle merchants on speed deliberately, so the end-to-end guarantee is
    # per-phase: excess never rises anywhere; the merchant total is minimal here
    # and may rise later, strictly within per-village budgets (§8.3, §14).
    #
    # The HELD-BACK count is rounded half-up, not the cap truncated. Truncating
    # the cap quietly did the opposite of what this comment used to claim: a
    # budget-1 village got a soft cap of 0 (its every merchant billed as
    # crowding) and a budget-2 village had 50% held back. Rounding what is held
    # keeps the fraction honest at every size -- 10% of 18 holds 2, of 10 holds
    # 1, of 4 or fewer holds nothing, so a budget too small to spare anything
    # simply has no soft cap.
    soft_budgets = {
        vid: budget - int(budget * merchant_headroom + 0.5) for vid, budget in budgets.items()
    }
    # Relay may only conscript a village the operator put in the crop plan; see
    # the candidate-set comment in `_relay_scan` for why, and for what KEEP
    # means here.
    #
    # The gate was chosen for correctness, and it costs nothing. Measured
    # 2026-09-03 on this machine by overriding `relay_hub_candidates` below to
    # the whole account and timing THIS pass alone (median of five runs, warmed):
    #
    #   random_account(5), 40 villages: 4.5s over 33 candidates (crop plan)
    #                                   5.8s over 40 candidates (whole account)
    #   random_account(7), 21 villages: 0.09s over 19, 0.09s over 21
    #
    # So narrowing is ~20% FASTER at 40 villages and a wash at 21 -- not the
    # direction candidate-count arithmetic predicts, because runtime here is
    # dominated by which relays the search FINDS and how far it then chases them,
    # not by how many candidates it scanned.
    crop_plan = resource_plans.get(Resource.CROP)
    relay_hub_candidates = frozenset(
        allocation.village_id
        for allocation in (crop_plan.villages if crop_plan is not None else ())
        if allocation.mode is not AllocationMode.KEEP
    )
    assignment, converged = _improve_flows(
        assignment,
        villages,
        geometry,
        merchant_model,
        cycles,
        budgets,
        max_improve_passes,
        max_relay_hops,
        soft_budgets=soft_budgets,
        max_cycle=max_cycle_by_destination,
        excluded_origins=excluded_origins_by_destination,
        relay_hub_candidates=relay_hub_candidates,
    )
    if not converged:
        # Never let a truncated search masquerade as a converged one: it inflates
        # over_budget_excess, so villages get reported over budget -- and handed
        # Trade Office upgrade advice -- that a finished search would not flag.
        findings.append(
            Finding(
                category=Category.SEARCH_TRUNCATED,
                message=(
                    f"route search stopped after {max_improve_passes} improvement passes "
                    f"with better assignments still available; the over-budget figures "
                    f"below may overstate the real shortfall. Raise max_improve_passes to "
                    f"finish the search."
                ),
                detail=f"stopped after {max_improve_passes} passes",
            )
        )
    # The declared tier joins the plan HERE, after the search, and that is the
    # whole of why Design B is a fraction of Design A's risk.
    #
    # `_improve_flows`' one move is a 2x2 swap, which preserves every origin's
    # outflow and every destination's inflow -- that conservation is what makes
    # the no-two-way-pair and no-waterfall rules survive it. It is also blind to
    # relay shape for anything but crop (`_crop_shape_ok` is consulted on crop
    # edges alone), so a material tier seeded into the assignment could be
    # rewired by an ordinary swap into a self-loop or a chain, and the objective
    # would not know it had done anything wrong. Keeping the tier out means the
    # search's objective is untouched, the crop relay mover is untouched, and
    # the two legs section 5 dictates are the two legs that get built.
    #
    # What the search consequently does not do is PRICE the tier's merchants,
    # and that is honest rather than convenient: the tier's shape is the
    # operator's declaration, so there is no alternative assignment the search
    # could have preferred. The merchants are still counted -- `committed` below
    # is built from the finished route list -- so the per-village cap is
    # measured against the collecting legs too (section 5: "the relay leg counts
    # inside the 8"), and a breach is reported.
    for resource, legs in tier.items():
        into = assignment.setdefault(resource, {})
        for key, amount in legs.items():
            into[key] = into.get(key, 0.0) + amount

    pair_cargo = _merge_pair_cargo(assignment)

    # A route can only originate at a real village that can staff merchants. This
    # is guaranteed by construction (senders come from surplus, relay hubs are
    # filtered to merchant-capable villages), but assert it before emitting the
    # plan: a route whose origin is a foreign sink is impossible to execute, and
    # silently shipping it is worse than failing loudly.
    for origin, _destination in pair_cargo:
        if origin not in villages or villages[origin].merchant_count <= 0:
            raise AssertionError(
                f"route origin {origin} is not a merchant-capable village; "
                "the optimizer built an impossible route"
            )

    routes: list[Route] = [
        _route_for_pair(
            origin,
            destination,
            pair_cargo[(origin, destination)],
            villages,
            geometry,
            merchant_model,
            cycles,
            max_cycle_by_destination,
        )
        for origin, destination in sorted(pair_cargo)
        if sum(pair_cargo[(origin, destination)].values()) > EPSILON
    ]

    # Spend each village's idle merchants (within budget) to shorten the routes
    # furthest over the latency target; skipped entirely when no target is set,
    # leaving the plan purely merchant-minimal.
    if max_latency_hours is not None:
        # Handed the SOFT budgets, deliberately. This pass spends idle merchants
        # on shorter cycles, and spending up to the hard budget undoes the exact
        # thing the improvement search just paid merchants for: a village the
        # search left under its soft cap was refilled to 100%, making the
        # request field's promise ("aims to leave this fraction uncommitted")
        # false whenever a latency target was set -- measured at peak 78% -> 100%
        # and 0 -> 3 over-cap villages on one real payload from the target alone.
        # At headroom 0 soft equals hard, so the off position is unchanged.
        routes = _spend_idle_merchants_on_latency(
            routes,
            villages,
            merchant_model,
            cycles,
            soft_budgets,
            max_latency_hours,
            min_send_fill,
            max_cycle_by_destination,
        )

    committed: dict[int, int] = {vid: 0 for vid in villages}
    for route in routes:
        committed[route.origin] += route.merchants_committed

    # Worst first. On a spread-out account this is the single largest block of
    # warnings -- 23 of 132 on the account that motivated the finding structure
    # -- and every one of them ends in the same clause, so the only thing that
    # decides whether the operator reads the third one is whether it is worse
    # than the first.
    # The relays are carried, not reported. Their end-to-end figure depends on
    # when each leg actually fires, which only the beat knows -- so the planner
    # re-times them and reports them there, once, with the real number.
    relays = relay_hubs(routes, material_relays=relay_for)

    if max_latency_hours is not None:
        for route in sorted(
            (r for r in routes if r.latency_hours > max_latency_hours),
            key=lambda r: -r.latency_hours,
        ):
            leg = (
                f"{village_label(route.origin, names)} -> {village_label(route.destination, names)}"
            )
            findings.append(
                Finding(
                    category=Category.LATENCY,
                    message=(
                        f"route {leg} has {route.latency_hours:.1f}h latency against a "
                        f"{max_latency_hours:.0f}h target; geometry or the merchant "
                        f"budget may forbid better"
                    ),
                    detail=f"{leg} — {route.latency_hours:.1f}h",
                    village=village_label(route.origin, names),
                )
            )

    routes_by_origin: dict[int, list[Route]] = {}
    for route in routes:
        routes_by_origin.setdefault(route.origin, []).append(route)

    # Read from `budgets`, not recomputed: the ceiling the search was held to is
    # the ceiling being reported against, and the Trade Office advice is costed
    # against the same one. Advising an upgrade that fits the FLEET while the
    # plan is held to the operator's cap recommends a level that still does not
    # fit -- and a cap is fixed by carrying more per merchant, so the advice is
    # genuinely useful here rather than merely well-formed.
    over_budget = tuple(
        OverBudget(
            village_id=vid,
            committed=used,
            available=budgets[vid],
            max_busy=villages[vid].max_busy_merchants,
            fleet_spare=villages[vid].spare_merchants(merchant_reserve),
            trade_office_levels_needed=_trade_office_levels_needed(
                villages[vid],
                routes_by_origin.get(vid, []),
                merchant_model,
                budgets[vid],
                cycles,
                max_cycle_by_destination,
            ),
        )
        for vid, used in sorted(committed.items())
        if used > budgets[vid]
    )

    findings.extend(
        _crowding_findings(villages, routes, committed, budgets, soft_budgets, geometry, names)
    )

    return Plan(
        routes=tuple(routes),
        merchants_committed=committed,
        shortfalls=tuple(shortfalls),
        over_budget=over_budget,
        findings=tuple(findings),
        relays=relays,
    )
