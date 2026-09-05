"""Derive a night profile's allocations from the account's own shape.

A night profile is not a preference, it is arithmetic. With the operator asleep
nothing is spent, so every unit that arrives stays, and the most a village may
accumulate per hour is fixed by its store:

    ceiling = (target_fill - baseline_fill) * capacity / window_hours

Measured from the BASELINE the operator leaves behind before sleeping, not from
whatever a snapshot happens to catch. Those are different numbers and only the
first repeats: a mid-afternoon reading is mid-accumulation, so a profile built
from one goes stale within the hour -- 34 of 100 allocations moved in a single
hour on the real account, three of them changing sign. Emptying before sleep is
what makes the baseline reproducible, and reproducible is what lets one profile
hold for weeks.

Two rules do the rest.

*Nobody exports what they have room for.* At night a village with space in its
warehouse gains nothing by shipping: moving 400 clay an hour fifteen hours across
the map buys nothing and spends merchants the plan needs elsewhere. So only two
kinds of village send -- one already past the target, and one drawn in to cover
what the army villages need, nearest first.

*Demand comes before supply.* A ceiling is an upper bound, not a target. The
stores can absorb far more than the account produces, so letting every village
claim its ceiling over-claims the account and drives the remainder negative.

Pure: no requests, no clock, no I/O. Everything it needs is passed in.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# MATERIALS moved to `allocation` once four modules needed it; imported here
# both because this module uses it and so the older `night_profile.MATERIALS`
# import keeps resolving.
from .allocation import MATERIALS, Allocation, AllocationMode, Resource
from .geometry import MapGeometry

# The reserve, capacity and distance models the planner already owns. Operator
# ruling section 1: capacity lives behind ONE injectable `MerchantModel` with
# `calibrate()`, and nothing else in the planner may hardcode a capacity -- so
# this module takes the model rather than the two primitives it is built from,
# and `MapGeometry` rather than its own copy of the torus.
from .merchants import MerchantModel
from .optimizer import DEFAULT_MERCHANT_RESERVE

DEFAULT_TARGET_FILL = 0.60
"""How full a store may be at dawn, and the FLOOR it must reach.

One number doing two jobs, which is why it is one number. The derivation reads
it as a ceiling -- the room a store has to fill is what bounds how much may be
shipped into it overnight -- and section 6 reads the same figure as a floor
every role village must have reached by 07:00. They are the same statement seen
from either side: "never overflow during the night, never arrive empty at
morning". Two constants could disagree; this one cannot.

Settled at 0.60 by the operator on 2026-09-03. It was 0.80 while the question
was open, and the extra 20 points of room is cargo the night ships into a store
that has to hold it -- so the pair was the unsafe direction to guess.
"""

DEFAULT_BASELINE_FILL = 0.25
"""How full each store is when the operator goes to bed.

An ASSUMPTION THE OPERATOR OWNS, not something the planner enforces. At the
day->night switch they spend the stores down by hand so no role village is above
this fraction, and the derivation is entitled to start from it: every night
ceiling is the room between here and :data:`DEFAULT_TARGET_FILL`.

What the planner may do is notice a disagreement. A snapshot showing a role
village above this at the switch means the room reserved to fill it is not
there, so the night ships into a store that cannot hold it -- reported as a
finding (``Category.PRE_NIGHT_BASELINE``) and never as a refusal, because the
manual spend-down is the operator's action and the planner is not in the room
when it happens.

Settled at 0.25 by the operator on 2026-09-03, from 0.30.
"""

NIGHT_WINDOW: tuple[int, int] = (23 * 60, 7 * 60)
"""Section 6's night hours, as minutes past midnight: 23:00 to 07:00.

Start-inclusive and end-exclusive, the same convention every other window in
the planner uses, so the minutes in it are exactly the firings a plan counts.
"""

MORNING_MINUTE: int = NIGHT_WINDOW[1]
"""When the day profile takes over, and the deadline the night has.

Two rules land on this minute. Every night movement must be COMPLETE by it --
no merchant underway or returning -- so the morning profile starts with a full
pool everywhere; and every role village must be at
:data:`DEFAULT_TARGET_FILL` on both stores by it.
"""


def is_night_window(window: tuple[int, int] | None, *, overnight: bool | None = None) -> bool:
    """Is this profile the overnight one, which section 6's rules govern?

    ``overnight`` is the profile's own DECLARATION, and it wins. Which profile
    the operator sleeps through is a fact about the operator and not about the
    clock -- the same reason ``npc_attended`` is declared per profile rather
    than inferred from its hours -- and the wrapping test below is neither
    necessary nor sufficient for it. Both misreadings are legal input:

    * A night SPLIT at midnight is two profiles, 23:00-00:00 and 00:00-07:00.
      Minute 1440 does not exist, so the first is typed ``(1380, 0)`` and does
      wrap; the SECOND -- the half that actually runs up to the switch -- does
      not, so nothing checks that its merchants are home by 07:00 and the 2h
      latency target buys speed nobody is waiting for.
    * A day profile that runs almost the whole day, ``(420, 419)``, wraps.
      Nothing about it is overnight, and the derivation alone silently
      suspends the latency target for the entire day.

    Absent a declaration the wrap is still the answer, so every caller that
    never had one keeps the behaviour it had:

    * A day's profiles do not overlap, so at most one of them can span
      midnight, and *where the operator's night is a single window* that one is
      it. The test is therefore structural rather than a magic number an
      operator has to match to the minute -- 22:45 to 07:15 is still the night.
    * The rules exist because nothing is SPENT overnight. That is what makes
      latency the wrong measure (a delivery nobody is waiting for costs nothing
      by being six hours old) and what makes the closing deadline the right one
      (the pool has to be whole when spending resumes). Both follow from the
      operator being asleep, which is what the declaration states outright and
      what the wrap only stands in for.

    ``None`` is a round-the-clock route set, which has no switch to be ready
    for and so keeps the day's rules whatever is declared: section 6's deadline
    is measured against a window's END, and there is no end to measure. The
    declaration cannot invent one.
    """
    if window is None:
        return False
    if overnight is not None:
        return overnight
    return window[0] > window[1]


@dataclass(frozen=True)
class NightVillage:
    """What deriving a night profile needs to know about one village."""

    village_id: int
    name: str
    x: int
    y: int
    merchants_total: int
    trade_office_level: int
    warehouse_capacity: int
    granary_capacity: int
    production: Mapping[Resource, float]
    """Own net production per hour, after whatever the village SPENDS.

    Crop may be negative for an army village. A material may be negative too,
    once the operator declares a spend larger than the village makes: the
    caller nets `consumption_per_hour` off the material rates here, because a
    village that burns its whole lumber production has none to keep overnight
    and none to shed. Crop is NOT netted -- the snapshot's crop rate is already
    net of upkeep, so subtracting a spend would count the same troops twice.
    """

    max_busy_merchants: int | None = None
    """The operator's ceiling on merchants underway or returning at once.

    It belongs here for the same reason `production` is netted here: how much a
    village can SHED overnight is its fleet times what a merchant carries times
    the trips it can make, and under a cap the fleet that matters is the cap. A
    derivation that ignored it would hand a capped village a retention it has no
    way to honour -- which is the defect a declared spend had on this path
    before, seen from the other side. ``None`` is no ceiling declared."""

    def capacity_for(self, resource: Resource) -> int:
        return self.granary_capacity if resource is Resource.CROP else self.warehouse_capacity


@dataclass(frozen=True)
class TributeTarget:
    """One foreign obligation the night must ship crop to.

    A place and a rate, kept together. They used to be a single
    ``tribute_per_hour`` beside a single ``tribute_at``, which cannot describe
    two allies: N obligations were summed into one rate and pinned to the FIRST
    one's coordinates, so a 500/h ally two fields out beside a 20,000/h artifact
    sixty fields out became 20,500/h priced at the two-field hop. Reordering the
    request body gave the opposite answer.
    """

    at: tuple[int, int]
    per_hour: float


@dataclass
class NightProfile:
    """The derived allocations, and what deriving them decided."""

    allocations: dict[Resource, dict[int, Allocation]] = field(default_factory=dict)
    forced_senders: dict[Resource, list[int]] = field(default_factory=dict)
    """Villages already past the target, which must give whatever the distance."""
    drawn_in: dict[Resource, list[int]] = field(default_factory=dict)
    """Villages pulled in to cover demand, nearest first."""
    unmet: dict[Resource, float] = field(default_factory=dict)
    """Demand no village could cover, per hour. Reported, never hidden."""
    residual_trimmed: float = 0.0
    """Crop per hour taken back off the largest share to stop the retentions
    claiming more than the account produces. A unit or two is the rounding of
    each retention to a whole number; more than that is a real deficit, and
    whatever the largest share could not cover lands in `unmet` instead."""


def _hours(a: NightVillage, b: NightVillage, geometry: MapGeometry) -> float:
    """Travel time one way, in hours, on Travian's wrapped map.

    The map is a torus: the shortest path between two edge villages can cross
    the seam, and the game routes merchants that way. Raw hypot made a hub at
    (-200|0) see a supplier at (200|0) as 400 fields away when it is 1, so the
    night draw picked a genuinely distant village over a next-door one and
    understated how much edge villages can shed overnight.

    Measured by :class:`~.geometry.MapGeometry`, which the plan side already
    uses, rather than by a second implementation of the same wrap here.
    """
    return geometry.one_way_minutes((a.x, a.y), (b.x, b.y)) / 60.0


def merchants_needed(
    legs: Sequence[tuple[int, float]],
    send: float,
    *,
    capacity: int,
    window_hours: float,
) -> int:
    """Merchants a send of `send` per hour costs, under the optimizer's own
    model: one POOL of whole merchants per destination.

    `legs` is ``(complete round trips, share of the cargo)`` per destination.
    That is what `merchants.py` charges a route -- ``ceil(batch / capacity) *
    sets_in_flight`` per destination, summed -- so the night bound has to be
    charged the same way, or the profile promises sends the planner will refuse
    to build. Destination `i` receives ``send * share_i`` an hour, i.e.
    ``send * share_i * window_hours`` over the night, and one merchant dedicated
    to it delivers `capacity` on each of its `trips` -- so it needs the `ceil` of
    the ratio, because a fraction of a merchant does not exist.
    """
    return sum(math.ceil(send * share * window_hours / (capacity * trips)) for trips, share in legs)


def partitioned_fleet_limit(
    legs: Sequence[tuple[int, float]],
    *,
    fleet: int,
    capacity: int,
    window_hours: float,
) -> float:
    """The largest per-hour send `fleet` merchants can actually make over `legs`.

    The fleet is PARTITIONED: a merchant serves one destination for the night,
    because `merchants.py` charges every route its own whole merchants and the
    planner will not build anything else. So this is the largest `send` with

        m_i = ceil(send * share_i * window_hours / (capacity * trips_i))
        sum(m_i) <= fleet

    -- exactly `merchants_needed` above, which is the predicate the answer is
    re-checked against.

    Solved exactly, not approximated. Every destination taking cargo needs at
    least one merchant, so a fleet smaller than the destination count sheds
    nothing at all; otherwise start at one merchant each and repeatedly give the
    next merchant to the destination currently holding the send down. That IS
    the optimum. Writing ``u_i = capacity * trips_i / (share_i * window_hours)``
    for what one merchant dedicated to `i` sustains, the send an allocation
    supports is ``min_i m_i * u_i``, and each `m_i` was last raised while `i` was
    the binding destination, so ``(m_i - 1) * u_i`` was the send at that moment
    and the send never falls. Any `x` above the final `min` therefore has
    ``x > (m_i - 1) * u_i`` at every destination and ``x > m_j * u_j`` at the
    binding one, so it needs at least ``sum(m_i) + 1`` merchants: nothing above
    the answer fits, which is what exactness means here.

    Zero-share destinations must not be passed in. They take no cargo, so they
    need no merchant -- but they still weigh in the demand shares upstream, and
    charging them a merchant here would be an invented cost.
    """
    if not legs or len(legs) > fleet:
        # Nowhere to put the cargo, or not enough merchants to give every
        # destination the one it must have. Either way nothing is shippable --
        # and no `max(1, ...)` anywhere here: a destination whose round trip does
        # not fit the window contributes `trips = 0` and is dropped upstream,
        # because crediting it one trip promises cargo that would still be in the
        # air at 07:00, which section 6 forbids outright.
        return 0.0
    units = [capacity * trips / (share * window_hours) for trips, share in legs]
    pool = [(unit, 1, unit) for unit in units]
    heapq.heapify(pool)
    for _ in range(fleet - len(legs)):
        _, merchants, unit = heapq.heappop(pool)
        heapq.heappush(pool, ((merchants + 1) * unit, merchants + 1, unit))
    limit = pool[0][0]
    # `limit` and `merchants_needed` reach the same quantity by different
    # arithmetic, so the ceiling can land one ulp high and reject the very send
    # the allocation was built to make. Step down to the last float the predicate
    # accepts -- a handful of ulps in practice, and it can only ever LOWER the
    # bound, which is the safe direction (operator ruling section 1:
    # over-estimating is the dangerous one). It terminates: at 0.0 nothing is
    # needed and nothing is over-promised.
    while limit > 0.0 and (
        merchants_needed(legs, limit, capacity=capacity, window_hours=window_hours) > fleet
    ):
        limit = math.nextafter(limit, 0.0)
    return limit


def derive_night_profile(
    villages: Sequence[NightVillage],
    *,
    window_hours: float,
    geometry: MapGeometry,
    merchant_model: MerchantModel,
    day_retention: Mapping[Resource, Mapping[int, float]],
    hub_id: int,
    consumer_ids: Sequence[int] = (),
    tributes: Sequence[TributeTarget] = (),
    baseline_fill: float = DEFAULT_BASELINE_FILL,
    target_fill: float = DEFAULT_TARGET_FILL,
    merchant_reserve: int = DEFAULT_MERCHANT_RESERVE,
) -> NightProfile:
    """Allocations for one night, from capacities and production alone.

    Args:
        geometry: the map's span and the merchants' speed, as
            :func:`craft_plan` takes them. The wrap matters here for the same
            reason it does there -- the night draw orders suppliers by
            distance -- and one model means the two cannot disagree about it.
        merchant_model: how much one merchant carries. Injected rather than
            re-derived from a base and a bonus, per operator ruling section 1:
            capacity lives behind this one model and its ``calibrate()``, and
            nothing else in the planner may hardcode one. It bounds how much a
            village can SHED overnight, so a stale copy here would promise
            cargo the merchants cannot carry.
        day_retention: what each village retains per hour under the DAY profile,
            per resource. The army villages' shares come from here rather than
            being invented, so the night is the day's plan bounded by the stores.
        hub_id: the village that absorbs surplus materials. At night its
            consumers fill up while it has room, so the day's direction reverses.
        consumer_ids: villages that consume crop. They break even -- ending the
            night at the fill they started -- which is what makes the profile
            hold rather than drift.
        tributes: the foreign obligations, each with its OWN coordinates. One
            per target rather than a summed rate at one place: the rate decides
            how much of a sender's cargo goes there and the coordinates decide
            what that leg costs, so two allies at different distances are two
            destinations and cannot be averaged into one before the derivation
            sees them.
    """
    by_id = {v.village_id: v for v in villages}
    tribute_per_hour = sum(target.per_hour for target in tributes)
    if hub_id not in by_id:
        raise ValueError(f"hub {hub_id} is not among the villages given")
    consumers = [vid for vid in consumer_ids if vid in by_id]

    def ceiling(v: NightVillage, resource: Resource) -> int:
        return round((target_fill - baseline_fill) * v.capacity_for(resource) / window_hours)

    def _trips(one_way_hours: float) -> int:
        """Complete round trips one merchant fits in the window.

        Never rounded up. A merchant cannot make a fraction of a trip, and
        crediting the fraction promises cargo that would still be in the air at
        07:00 -- which section 6 forbids outright.
        """
        if one_way_hours <= 0:
            return 0
        return int(window_hours // (2 * one_way_hours))

    def _anyone_reaches(point: tuple[int, int]) -> bool:
        """Whether ANY village could complete a round trip to `point` tonight.

        A destination nobody can reach and return from is not demand the night
        can cover at any fleet size, so its claim is reported outstanding rather
        than pooled with the rest -- where a single lump of demand let a sender
        bounded by its OWN reachable destinations be booked to cover it.
        """
        return any(
            _trips(geometry.one_way_minutes((v.x, v.y), point) / 60.0) > 0 for v in by_id.values()
        )

    def _material_receivers(resource: Resource) -> list[tuple[int, float]]:
        """Where a material goes tonight, with what each destination needs/hour.

        The hub is always here: it is the REMAINDER village, so a forced sender
        shedding to avoid overflow ships to it whatever anyone else needs, and
        its own claim is whatever its production falls short by. Everything
        else in the list is a village the draw below books senders AGAINST --
        one the day plan wants to hold more than it makes, or one whose spend
        exceeds its production. Both are places the cargo really goes, and
        pricing every sender's hop to the hub bound it by a village its cargo
        never visits: a supplier 199 fields from the hub and ONE field from the
        receiver it feeds was told it could ship nothing over a ten-minute
        haul. That is the same error `_destinations` documents for crop, one
        resource across.

        A zero claim is kept rather than dropped -- `_legs` gives it no share,
        so it neither weighs in the mean nor bounds the integral, but a hub
        that needs nothing while nobody else needs anything either must still
        be the destination a forced sender is measured against.
        """
        day = day_retention.get(resource, {})
        receivers: list[tuple[int, float]] = [
            (hub_id, max(0.0, -by_id[hub_id].production.get(resource, 0.0)))
        ]
        for vid, village in by_id.items():
            if vid == hub_id:
                continue
            own = village.production.get(resource, 0.0)
            wanted = day.get(vid)
            if wanted is not None and wanted > own:
                claim = min(wanted, ceiling(village, resource)) - own
            elif own < 0:
                claim = -own
            else:
                continue
            if claim > 0:
                receivers.append((vid, claim))
        return receivers

    def _destinations(v: NightVillage, resource: Resource) -> list[tuple[float, float]]:
        """Where this village's cargo of `resource` goes, in one-way hours.

        It is a DIFFERENT set per resource, which is why `shed_limit` cannot be
        asked without one. The hub absorbs surplus MATERIALS, so for lumber,
        clay and iron it is always among the destinations -- but not the only
        one: the material pass below draws villages to cover each RECEIVER's
        `take - own`, and a village drawn to cover a receiver ships to the
        receiver. See `_material_receivers`. Crop never reaches the hub at all:
        the crop pass ships to the crop-negative villages in `consumer_ids` and
        to `tribute_at`, and on this account the hub is a crop SENDER rather
        than a sink -- its own granary ceiling can sit under its own production
        and force it to give.

        Measuring every sender to the hub bound each crop sender by a village
        its cargo never visits, and it read wrong in both directions. A feeder
        one field from the hub with the hammer 199 fields out was booked to
        ship its whole production at the hub's 48 turnarounds while no round
        trip to the hammer fits the night at all; and a feeder 199 fields from
        the hub feeding a hammer one field away -- a ten-minute haul, 48
        turnarounds -- was told it could ship nothing.

        A village is never its own destination: the tile is unique in Travian,
        so a zero hop is the village itself and is dropped rather than allowed
        to read as a free delivery.

        Each hop is paired with the CLAIM on it -- how much the destination
        needs per hour -- because a bound over destinations at different
        distances has to be weighted by something, and demand is the only thing
        that says how the cargo is actually split. See `shed_limit`.
        """
        if resource is not Resource.CROP:
            hops = [
                (_hours(v, by_id[other], geometry), claim)
                for other, claim in _material_receivers(resource)
            ]
        else:
            hops = [
                (
                    _hours(v, by_id[c], geometry),
                    max(0.0, -by_id[c].production.get(Resource.CROP, 0.0)),
                )
                for c in consumers
            ]
            hops.extend(
                (
                    geometry.one_way_minutes((v.x, v.y), target.at) / 60.0,
                    target.per_hour,
                )
                for target in tributes
            )
        return [(hop, claim) for hop, claim in hops if hop > 0]

    def _legs(v: NightVillage, resource: Resource) -> list[tuple[float, int, float]]:
        """Where this village's cargo of `resource` goes tonight, as
        ``(one-way hours, complete trips, share of the cargo)`` per destination.

        The share is the destination's claim over the claims of everything this
        village can actually reach -- demand is the only thing that says how the
        cargo is split. Destinations no round trip reaches are dropped: they take
        no cargo, so they neither bound the send nor weigh in the mean, and
        `_anyone_reaches` keeps their claim out of the pooled demand so nothing
        reads them as covered.

        One function because two callers need exactly the same set: `shed_limit`
        bounds the send by it, and the crop draw ORDERS by it. Ordering by
        anything else -- the hub, or the tribute -- spends merchants for
        nothing.
        """
        measured = [(hop, claim, _trips(hop)) for hop, claim in _destinations(v, resource)]
        reachable = [(hop, claim, trips) for hop, claim, trips in measured if trips > 0]
        if not reachable:
            return []
        claimed = sum(claim for _, claim, _ in reachable)
        if claimed > 0:
            shares = [claim / claimed for _, claim, _ in reachable]
        else:
            # Destinations that need nothing. Nothing is drawn to them, so this
            # only reaches a FORCED sender shedding to avoid overflow, and no
            # destination has a larger claim on that cargo than another -- so
            # they weigh the same. Not zero: they are real places, and a mean
            # of nothing would read as a free delivery.
            shares = [1.0 / len(reachable)] * len(reachable)
        return [(hop, trips, share) for (hop, _, trips), share in zip(reachable, shares)]

    def _mean_hop(v: NightVillage, resource: Resource) -> float:
        """The demand-weighted mean one-way hop, or infinity where nothing is
        reachable -- which sorts such a village last and sheds nothing."""
        legs = _legs(v, resource)
        if not legs:
            return math.inf
        return sum(hop * share for hop, _, share in legs)

    def shed_limit(v: NightVillage, resource: Resource) -> float:
        """The most this village can send per hour and still be shippable.

        A retention below production only means something if the merchants exist
        to carry the difference, and how much a fleet moves in a night depends on
        how far it goes: a village one field from its neighbour turns round dozens
        of times, one an hour away twice.

        Over SEVERAL destinations the fleet is PARTITIONED, and that is the whole
        finding here. A merchant serves one destination for the night: it cannot
        spend 0.6 of itself on the near consumer and 0.4 on the far ally. So the
        bound is `partitioned_fleet_limit` -- the largest send whose
        per-destination merchant demand fits the fleet.

        It replaces `min(merchant-hours conservation, per-destination
        integrality)`, which over-promised because neither factor sees the
        partition. Conservation lets merchant-time split fractionally across
        destinations, and the integral bound asks each destination ALONE whether
        the whole fleet could serve it; both pass sends no allocation can
        actually make. Measured: one merchant carrying 2,500, an 8h night, a
        destination an hour out claiming 3 and one three hours out claiming 1 --
        shares 0.75/0.25, mean hop 1.5h, two turnarounds -- conserved 625/h,
        5,000 over the night. Its 3,750 to the near one is two trips (4h) and its
        1,250 to the far one is one trip (6h): ten merchant-hours out of eight,
        from a fleet of one. Under the partition the two destinations need a
        merchant each, and the answer is 0.

        A destination NO round trip reaches is dropped from the set rather than
        allowed to zero the bound, and it removes only its own claim:
        `_anyone_reaches` keeps it out of the pooled demand, so it lands in
        `unmet` instead of being covered by a sender that cannot get there.
        """
        # Off the injected model, floored exactly as the plan side floors it --
        # understating capacity over-provisions merchants, overstating it
        # breaches the budget, and only the first is recoverable.
        capacity = merchant_model.capacity(v.trade_office_level)
        fleet = max(0, v.merchants_total - merchant_reserve)
        # The operator's cap, where there is one, is what may actually be in the
        # air -- so it is the fleet this village ships the night with. The
        # tighter of the two, the same rule `VillageState.merchant_budget`
        # applies on the plan side: a cap above the fleet is not extra merchants.
        if v.max_busy_merchants is not None:
            fleet = min(fleet, v.max_busy_merchants)
        # Where the cargo of THIS resource actually goes. It used to measure the
        # nearest village on the account, which is an upper bound on turnarounds
        # and no bound at all on a clustered map: a neighbour one field away
        # yields 47 trips in an 8h night, so the "limit" came out around six
        # times fleet x capacity per hour. Worse, the operator's cap is applied
        # just above and then multiplied by that count -- so the "8 busy at 02"
        # ceiling this function exists to honour was negated inside it.
        # Zero-share destinations are dropped: they take no cargo and so need no
        # merchant. They are here to be MEASURED against -- a hub that needs
        # nothing is still where a forced sender's surplus goes -- not served.
        legs = [(trips, share) for _, trips, share in _legs(v, resource) if share > 0]
        # An empty set means nowhere for this resource to go: the hub asked about
        # its own materials, a crop sender on an account with no crop-negative
        # village and no tribute, or every destination further than a night.
        # Sheds nothing, which is the honest reading either way.
        return partitioned_fleet_limit(
            legs, fleet=fleet, capacity=capacity, window_hours=window_hours
        )

    def capped(v: NightVillage, resource: Resource) -> int:
        """Its ceiling, never asking for more export than it can ship."""
        own = v.production.get(resource, 0.0)
        return max(ceiling(v, resource), round(own - shed_limit(v, resource)))

    profile = NightProfile()

    # ── Materials ────────────────────────────────────────────────────────────
    # The hub absorbs whatever its consumers cannot hold, which at night is most
    # of it: their stores fill in hours while the hub has room to spare.
    for resource in MATERIALS:
        entries: dict[int, Allocation] = {
            hub_id: Allocation(mode=AllocationMode.REMAINDER, value=0.0)
        }
        demand = -by_id[hub_id].production.get(resource, 0.0)
        day = day_retention.get(resource, {})
        forced: list[int] = []
        for vid, village in by_id.items():
            if vid == hub_id:
                continue
            own = village.production.get(resource, 0.0)
            room = ceiling(village, resource)
            wanted = day.get(vid)
            if wanted is not None and wanted > own:
                # A receiver under the day plan: give it what it can still hold.
                # Bounded below by what it can actually SHIP, exactly as
                # `capped()` bounds a forced sender: `min(wanted, room)` under
                # its own production is a promise to move the difference, and
                # this branch consulted no distance at all -- a village whose
                # store leaves room for 44/h was booked to ship 19,956/h over
                # 199 fields, where no round trip fits the night.
                take = max(min(wanted, room), round(own - shed_limit(village, resource)))
                entries[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(take))
                demand += take - own
                continue
            if room < own:
                # Already past the target. It gives whatever the distance, because
                # leaving it to overflow is a certain loss.
                entries[vid] = Allocation(
                    mode=AllocationMode.ABSOLUTE, value=float(capped(village, resource))
                )
                demand -= own - capped(village, resource)
                forced.append(vid)
                continue
            if own < 0:
                # It spends more of this material than it makes, so it has
                # nothing to keep and needs the difference DELIVERED -- exactly
                # the claim the receiver branch above books as `take - own` with
                # a `take` of zero. Booked here, before the draw, so a village
                # with slack can cover it and whatever nobody covers is reported
                # in `unmet`. Its own retention is clamped to zero below: a
                # negative absolute retention is refused in `Allocation`, and
                # dropping the deficit instead would hand back a profile that
                # starves the village while claiming nothing was outstanding.
                demand += -own
        drawn: list[int] = []
        if demand > 0:
            order = sorted(
                (vid for vid in by_id if vid != hub_id and vid not in entries),
                key=lambda vid: _hours(by_id[vid], by_id[hub_id], geometry),
            )
            for vid in order:
                if demand <= 0:
                    break
                own = by_id[vid].production.get(resource, 0.0)
                keep = day.get(vid, own)
                # Never more than it can carry. A retention below production is
                # a promise to ship the difference, and `shed_limit` is what
                # that promise costs -- the bound `capped()` already puts under
                # a FORCED sender, and the draw is the same promise made for a
                # different reason.
                give = min(own - keep, demand, shed_limit(by_id[vid], resource))
                if give <= 0:
                    continue
                entries[vid] = Allocation(
                    mode=AllocationMode.ABSOLUTE, value=float(round(own - give))
                )
                demand -= give
                drawn.append(vid)
        # Everyone untouched keeps exactly what it makes, stated rather than
        # omitted so it stays inside the conservation sum instead of leaving it.
        # Clamped at zero, for the reason the crop loop below is: a declared
        # spend larger than the village's production makes the rate NEGATIVE
        # (the caller nets it off, and `NightVillage.production` says so), and a
        # negative absolute retention is refused in `Allocation.__post_init__`
        # -- so `/night-profile` answered 500 for a spend one unit past
        # production. Nothing to keep is zero; the difference is already booked
        # as demand above, so the clamp reports it rather than hiding it.
        for vid, village in by_id.items():
            if vid not in entries:
                entries[vid] = Allocation(
                    mode=AllocationMode.ABSOLUTE,
                    value=float(max(0, round(village.production.get(resource, 0.0)))),
                )
        profile.allocations[resource] = entries
        profile.forced_senders[resource] = sorted(forced)
        profile.drawn_in[resource] = drawn
        profile.unmet[resource] = max(0.0, demand)

    # ── Crop ─────────────────────────────────────────────────────────────────
    crop: dict[int, Allocation] = {}
    demand = 0.0
    # Claims no village can complete a round trip to tonight. Held apart from
    # `demand` because `demand` is a single pool and `shed_limit` is bounded by
    # each sender's OWN reachable destinations -- so pooling them let a sender
    # be booked to cover a place it cannot get to and back from, and the
    # obligation read as paid. Reported as outstanding instead.
    unservable = 0.0
    for target in tributes:
        if target.per_hour > 0 and not _anyone_reaches(target.at):
            unservable += target.per_hour
        else:
            demand += target.per_hour
    for vid in consumers:
        # Break even: end the night at the fill it started, which is what stops
        # the profile drifting from night to night.
        crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=0.0)
        claim = -by_id[vid].production.get(Resource.CROP, 0.0)
        if claim > 0 and not _anyone_reaches((by_id[vid].x, by_id[vid].y)):
            unservable += claim
        else:
            demand += claim

    forced_crop: list[int] = []
    for vid, village in by_id.items():
        if vid in crop:
            continue
        own = village.production.get(Resource.CROP, 0.0)
        if ceiling(village, Resource.CROP) < own:
            value = capped(village, Resource.CROP)
            crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(value))
            demand -= own - value
            forced_crop.append(vid)

    # Drawn cheapest-first, where cheap means the haul this village's crop
    # actually makes: the demand-weighted mean hop over its own reachable
    # destinations, the same quantity `shed_limit` bounds it by.
    #
    # It used to be the distance to the HUB, or -- with a tribute -- to the
    # tribute, and crop goes to neither. A supplier 2 fields from the hub and 18
    # from the hammer (a 3h round trip, two turnarounds) was drawn ahead of one
    # 19 from the hub and ONE from the hammer (ten minutes, forty-eight
    # turnarounds), and the plan then built the long route at six merchants
    # where the short one costs three -- with the early firing still in the air
    # at 09:00, which is a NIGHT_OVERRUN as well. The tribute branch was worse:
    # 40,000/h of consumers beside a 1,000/h obligation ordered every supplier
    # by the 1,000/h destination.
    #
    # Coverage does not move either way -- `give = min(own, demand, shed_limit)`
    # and `shed_limit` reads nothing the loop mutates, so greedy fill yields
    # `min(demand, sum of caps)` under every permutation. Only the merchant bill
    # does.
    #
    # The MATERIAL draw above deliberately keeps its hub ordering: for materials
    # the hub genuinely is the sole destination, so there the hub distance is
    # the haul.
    drawn_crop: list[int] = []
    if demand > 0:

        def _cost(vid: int) -> float:
            return _mean_hop(by_id[vid], Resource.CROP)

        for vid in sorted((v for v in by_id if v not in crop), key=_cost):
            if demand <= 0:
                break
            own = by_id[vid].production.get(Resource.CROP, 0.0)
            if own <= 0:
                continue
            # Bounded by what it can move, as the material draw above is.
            give = min(own, demand, shed_limit(by_id[vid], Resource.CROP))
            if give <= 0:
                continue
            crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(round(own - give)))
            demand -= give
            drawn_crop.append(vid)

        # A ceiling is an upper bound on what a village may ACCUMULATE, not a
        # floor under what it may give. A forced sender was set to shed exactly
        # the excess over its ceiling and then skipped, so a hub making 60,000 an
        # hour offered 10,000 and the rest of the demand was reported unmet while
        # its crop sat there. When demand survives the first pass, come back to
        # them and take more -- down to retaining nothing, never below.
        for vid in sorted(forced_crop, key=_cost):
            if demand <= 0:
                break
            held = crop[vid].value
            own = by_id[vid].production.get(Resource.CROP, 0.0)
            # Down to retaining nothing, never below -- and never past what it
            # can ship. The forced pass already booked `own - held` of its shed
            # limit, so only the rest of that limit is still available here.
            give = min(held, demand, shed_limit(by_id[vid], Resource.CROP) - (own - held))
            if give <= 0:
                continue
            crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(held - give))
            demand -= give
            if vid not in drawn_crop:
                drawn_crop.append(vid)

    for vid, village in by_id.items():
        if vid not in crop:
            # Clamped at zero. `consumer_ids` defaults to `()` and
            # `NightVillage.production` is documented as possibly negative for an
            # army village, so a caller can legally reach here with a village
            # that is LOSING crop -- and a negative absolute retention is refused
            # in `Allocation.__post_init__`, so the derivation raised at the
            # caller for supplying exactly what the signature invites. A village
            # losing crop retains none of it; the deficit belongs to the
            # receiving side, which is what `consumer_ids` is for. The HTTP path
            # never gets here (`post_night_profile` classifies consumers from
            # `crop < 0` first), so this is the library contract, not a route.
            crop[vid] = Allocation(
                mode=AllocationMode.ABSOLUTE,
                value=float(max(0, round(village.production.get(Resource.CROP, 0.0)))),
            )

    # Every retention above is an integer and production is not, so the rounded
    # parts miss the fractional whole by a fraction of a unit. The planner treats
    # ANY negative residual as the allocation over-claiming the account, so 0.29
    # crop an hour out of 106,558 made a plan that simulates perfectly read as not
    # executable. Give the rounding somewhere to land, taking it from the largest
    # entry -- the one least disturbed by losing a unit. Only when the demand was
    # covered, though: an unmet shortfall is not rounding, it is already reported
    # as ``unmet``, and trimming it would push a retention below zero.
    produced = sum(v.production.get(Resource.CROP, 0.0) for v in villages)
    claimed = sum(a.value for a in crop.values())
    residual = produced - tribute_per_hour - claimed
    over_claimed = 0.0
    # Everything still owed after the draw: what nobody had the capacity for,
    # plus what nobody could reach. The trim is for ROUNDING, so it only runs
    # when the night owes nothing -- an unreachable consumer's whole deficit is
    # not a rounding error, and trimming it would take crop off a retention and
    # ship it nowhere.
    outstanding = max(0.0, demand) + unservable
    if residual < 0 and outstanding <= 0:
        largest = max(crop, key=lambda vid: crop[vid].value)
        # Never more than that entry actually holds. The trim builds another
        # absolute retention, and `Allocation` refuses a negative one, so a
        # slack larger than the largest entry would raise instead of rounding.
        # `residual_trimmed` reports what was taken rather than what was wanted,
        # so the figure the operator reads is the one the profile used.
        taken = min(int(-residual) + 1, crop[largest].value)
        if taken > 0:
            crop[largest] = Allocation(
                mode=AllocationMode.ABSOLUTE, value=crop[largest].value - taken
            )
            profile.residual_trimmed = float(taken)
        # And what the clamp could NOT take is reported, not dropped. The gap is
        # only rounding when it is a unit or two; a crop-negative village that no
        # caller named a consumer is clamped to zero retention here, so the gap
        # can be that village's whole deficit -- 20,000/h against a 5,000/h share
        # to take it from, or against nothing at all when every retention is
        # already zero. Silently discarding it returned a profile that still
        # claimed more crop than the account makes while reporting no shortfall.
        # Reported rather than raised: the demand is real and the profile is
        # usable, and `unmet` is exactly the channel for demand nobody covered.
        over_claimed = max(0.0, -residual - taken)

    profile.allocations[Resource.CROP] = crop
    profile.forced_senders[Resource.CROP] = sorted(forced_crop)
    profile.drawn_in[Resource.CROP] = drawn_crop
    # Disjoint by construction: the trim above only runs when nothing is
    # outstanding, so at most one of the two terms is ever non-zero.
    profile.unmet[Resource.CROP] = outstanding + over_claimed
    return profile
