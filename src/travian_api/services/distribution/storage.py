"""Will a village overflow its warehouse, or starve its granary?

A route set that balances perfectly on paper can still be wrong in the game.
Two failure modes, and the profile records both because both have bitten:

* **Overflow** (section 10). Resources arriving faster than they are spent stop
  at the cap and the excess is simply lost.
* **Starvation** (review R7). The same arithmetic run on a village with negative
  net crop returns a *negative* fill time, which reads as "no problem" while the
  granary is emptying and troops are about to die. R7's point is that
  ``fill_time = (capacity - stock) / net`` only models one direction, and the
  direction it ignores is the one that kills armies.

So this module never returns a single "time to trouble" number: it returns which
way the village is heading and how long it has, and treats a zero-ish rate as
"no trouble either way" rather than dividing by it.

The second half is known issue #12: a continuous-rate check passes while a lumpy
arrival overflows. Rates are averages, but cargo lands in discrete batches, so
:func:`simulate_day` replays the actual beat against the actual capacity instead
of trusting the average.

That replay finds two different failures, and until they were told apart both
were reported in the words of the rarer one ("an arriving batch overflows even
though the average rate fits"). A *burst* overflow is issue #12 proper: the
average fits, one delivery does not, and a shorter cycle fixes it. A
*structural* overflow is a store that gains more every day than it can pass on,
so it never leaves its cap, no schedule can help, and the surplus needs
somewhere to go. :attr:`OverflowEvent.structural` is which.

A third case reads off the same replay: a village holding cargo it never grew.
:func:`relay_buffer_findings` asks whether a DECLARED material relay (profile
section 5) has the warehouse to absorb its pass-through -- the collecting rate
over the longer of its two cycles. That is the reason material relay was
deferred once before -- the capital's warehouse is 1,200,000 and a neighbour's
is 160,000 -- and it is not the same question as either check above, because the
cargo at risk is somebody else's and its loss is invisible at both ends of the
tier.

Pure functions over already-fetched state. Nothing here spends a game request.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .allocation import MATERIALS, Resource, village_label
from .findings import Category, Finding
from .night_profile import DEFAULT_BASELINE_FILL, DEFAULT_TARGET_FILL
from .npc import NpcReserve
from .optimizer import RelayHub
from .schedule import MINUTES_PER_DAY, Beat, ScheduledRoute

# Rates below this are treated as level rather than drifting. Dividing by a rate
# of 0.01/h yields a "time to full" of centuries, which is noise dressed as a
# number.
NEGLIGIBLE_RATE = 1.0

# Hours of headroom below which the operator wants telling. Section 10 asks for
# 18h on the filling side; the same threshold is applied to starvation, because
# a granary emptying inside 18h is at least as urgent.
DEFAULT_WARN_HOURS = 18.0


class Trend(StrEnum):
    FILLING = "filling"
    DRAINING = "draining"
    STEADY = "steady"


@dataclass(frozen=True)
class StoreStatus:
    """Where one store on one village is heading, and how fast."""

    village_id: int
    resource: Resource
    stock: int
    capacity: int | None
    net_per_hour: float
    trend: Trend
    hours_remaining: float | None
    """Hours until full (filling) or empty (draining). None when unknowable:
    a steady rate, or a filling store whose capacity was never read."""

    @property
    def is_urgent(self) -> bool:
        return self.hours_remaining is not None and self.hours_remaining < DEFAULT_WARN_HOURS


def store_status(
    village_id: int,
    resource: Resource,
    stock: int,
    capacity: int | None,
    net_per_hour: float,
    negligible_rate: float = NEGLIGIBLE_RATE,
) -> StoreStatus:
    """Classify one store. Both directions, never a negative "time to full"."""
    if abs(net_per_hour) < negligible_rate:
        return StoreStatus(village_id, resource, stock, capacity, net_per_hour, Trend.STEADY, None)
    if net_per_hour < 0:
        # Emptying: what matters is the stock on hand, and capacity is
        # irrelevant -- which is why this branch needs no capacity reading and
        # always produces an answer.
        return StoreStatus(
            village_id,
            resource,
            stock,
            capacity,
            net_per_hour,
            Trend.DRAINING,
            stock / -net_per_hour,
        )
    if capacity is None:
        return StoreStatus(village_id, resource, stock, capacity, net_per_hour, Trend.FILLING, None)
    return StoreStatus(
        village_id,
        resource,
        stock,
        capacity,
        net_per_hour,
        Trend.FILLING,
        max(0.0, (capacity - stock) / net_per_hour),
    )


@dataclass(frozen=True)
class OverflowEvent:
    """A store that hits its cap on a *repeating* day of the beat."""

    village_id: int
    resource: Resource
    minute: int
    """Minute of the day the cap was first reached.

    Meaningless for a :attr:`structural` overflow: a store that never leaves its
    cap is already there when the day starts, so this is always 00:00 and is not
    the time of any event. Only read it for a burst overflow.
    """
    wasted_per_day: float
    """Resources lost to the cap on a settled day -- a genuinely recurring rate.

    Deliberately measured after the simulation has reached a steady state. Read
    off day one it would be an artifact of whatever the store happened to hold
    at snapshot time: a village sitting near its cap loses one batch, settles,
    and never loses anything again, which is a different problem with a
    different fix and is already covered by the continuous fill-time check.
    """
    net_gain_per_day: float = 0.0
    """Own production plus arrivals minus departures over the settled day.

    The whole point of a discrete replay was issue #12: the *average* fits and
    the batch does not. That claim is only true when this is not positive. When
    it IS positive the store gains this much every day no matter how the sends
    are phased, so the average does not fit either and no scheduling change can
    help -- a completely different problem with a completely different fix. The
    two were reported with identical wording, and the wording asserted the
    reason for the case that was in fact the rarer of the two.
    """
    days_to_cap: float | None = None
    """Days from the snapshot until a projected store sits at its cap.

    The horizon the replay ran, plus whatever headroom was left at its end
    covered at the last day's net gain. ``None`` for an observed event, whose
    store is already at its cap -- and that absence is what :attr:`projected`
    reads, so the two cannot disagree. They used to be separate fields with an
    invariant nobody enforced: a ``projected`` event with ``days_to_cap`` left
    at 0.0 would have reported "will reach its cap in about 0 days".
    """

    @property
    def projected(self) -> bool:
        """The loss was extrapolated, not watched.

        True when the replay ran its whole horizon without the day repeating
        and this store was still gaining: it had not reached its cap yet, or
        reached it too late in the last day for that day to show the whole loss.
        Then ``wasted_per_day`` is its ``net_gain_per_day`` -- what a store that
        never leaves its cap loses -- rather than a figure any simulated day
        produced.
        """
        return self.days_to_cap is not None

    @property
    def structural(self) -> bool:
        """Is the surplus itself too big, rather than one delivery of it?"""
        return self.net_gain_per_day > MIN_REPORTED_WASTE


# Waste below this is float residue from the settling loop, not a loss. A
# store that converges to exactly its cap leaves a few nano-resources of
# rounding behind, and reporting that as a daily loss is noise.
MIN_REPORTED_WASTE = 1.0

# Accumulating production in fractional steps leaves float residue: eighteen
# five-minute spans of 20,000/h land on 30,000.000000000004, not 30,000. Without
# a tolerance a store that reaches its alert level exactly is reported as having
# crossed it one step early, so the hour in the warning is an artifact of the
# step grid rather than a fact about the account.
CROSSING_TOLERANCE = 1e-6

# Days to run before believing the numbers. Convergence is normally immediate
# once the transient has drained; this only bounds a pathological input.
MAX_SETTLING_DAYS = 14

# How far ahead a projected overflow is still this plan's problem. Beyond a
# month the account has changed -- fields upgraded, troops trained, targets
# retuned -- so a store that far from its cap says nothing about the sheet.
MAX_PROJECTION_DAYS = 30


def _net_of_consumption(
    own_per_hour: Mapping[int, Mapping[Resource, float]],
    consumption: Mapping[int, Mapping[Resource, float]] | None,
) -> Mapping[int, Mapping[Resource, float]]:
    """Own production less what the village spends, per village per resource.

    The two replays below both need it and both need it identically -- a
    parameter threaded into one and not the other is how /plan and /day-check
    came to answer the same account differently once before.

    Only pairs the caller supplied a production rate for are adjusted. A village
    whose rate could not be read sits its resource out of the simulation
    entirely (no stock and no capacity are supplied for it either), so
    subtracting a spend there would half-simulate a store: a level that only
    ever falls, against a cap nobody read. The caller reports the unreadable
    rate instead.

    Returned unchanged when there is nothing to subtract, so the no-consumption
    path allocates nothing and is byte-for-byte the pre-consumption replay.
    """
    if not consumption:
        return own_per_hour
    out: dict[int, dict[Resource, float]] = {}
    for vid, per_resource in own_per_hour.items():
        spend = consumption.get(vid)
        if not spend:
            out[vid] = dict(per_resource)
            continue
        out[vid] = {
            resource: own - spend.get(resource, 0.0) for resource, own in per_resource.items()
        }
    return out


def _accrue(
    reserves: Mapping[int, NpcReserve], budget: MutableMapping[int, float], hours: float
) -> None:
    """Add ``hours`` of conversion capacity to each village's budget.

    Capped at one day's allowance: a budget that accumulated without bound over
    the settling days would be the infinite reservoir again, wearing a rate.
    """
    for vid, reserve in reserves.items():
        if reserve.allowance_per_day > 0.0:
            budget[vid] = min(
                reserve.allowance_per_day,
                budget.get(vid, 0.0) + reserve.allowance_per_hour * hours,
            )


def _npc_top_up(
    reserve: NpcReserve,
    resource: Resource,
    level: Mapping[tuple[int, Resource], float],
    budget: MutableMapping[int, float],
    shortfall: float,
) -> tuple[float, dict[Resource, float]]:
    """How much conversion funds this departure, and what each store paid for it.

    Bounded three ways, and each one is load-bearing: by what is asked for, by
    the budget accrued so far (so the reservoir is finite and can run out), and
    by what the feedstock stores actually hold (so nothing is converted from
    crop that has already been shipped away). NPC conserves the village's total,
    so this only ever MOVES resources between that village's own stores at 1:1 --
    the caller books both halves.

    A material source keeps the buffer the operator maintains on it; crop keeps
    nothing, because a granary has no floor -- it is not NPC-fed.
    """
    if shortfall <= 0.0 or not reserve.sources:
        return 0.0, {}
    spare: dict[Resource, float] = {}
    for source in reserve.sources:
        keep = 0.0 if source is Resource.CROP else reserve.floor_level
        spare[source] = max(0.0, level.get((reserve.village_id, source), 0.0) - keep)
    funded = min(shortfall, budget.get(reserve.village_id, 0.0), sum(spare.values()))
    if funded <= 0.0:
        return 0.0, {}
    budget[reserve.village_id] = budget.get(reserve.village_id, 0.0) - funded
    # Proportional to each store's share of the retention that sized the
    # allowance, so the store funding most of it is debited most -- then spilled
    # onto whatever else has room where one store cannot cover its share. The
    # operator converts from what is actually in the village, not from a ratio.
    paid: dict[Resource, float] = {}
    left = funded
    for source in reserve.sources:
        want = min(spare[source], funded * reserve.share_of(source))
        if want > 0.0:
            paid[source] = want
            left -= want
    for source in reserve.sources:
        if left <= 0.0:
            break
        room = spare[source] - paid.get(source, 0.0)
        if room > 0.0:
            take = min(room, left)
            paid[source] = paid.get(source, 0.0) + take
            left -= take
    return funded, paid


def simulate_day(
    beat: Beat,
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    net_per_hour: Mapping[int, Mapping[Resource, float]],
    step_minutes: int = 5,
    dispatch_window: tuple[int, int] | None = None,
    npc: Mapping[int, NpcReserve] | None = None,
    consumption: Mapping[int, Mapping[Resource, float]] | None = None,
) -> tuple[OverflowEvent, ...]:
    """Replay the beat against real capacities until it settles. Issue #12.

    The continuous check asks whether the *average* inflow overruns the cap. This
    asks whether the store is ever actually full, which is a different question:
    a village can sit comfortably under its cap on average and still lose a whole
    batch every cycle because a long-cycle route dumps it all at once.

    Two things this is careful about, both of which produce confident nonsense if
    skipped:

    **It runs to a steady state, not for one day.** Seeded from the observed
    stock, day one measures the transient -- a store that happens to be nearly
    full right now loses a batch, settles, and never loses anything again. That
    is real, but it is a "drain this village today" problem, already reported by
    the continuous fill-time check, and calling it a *per-day* loss overstates it
    indefinitely. Only waste that survives to a settled day is a property of the
    beat, and only that is reported.

    **Cargo is conserved.** A dispatch takes what the origin actually has; the
    matching arrival delivers exactly that. Crediting the destination a full
    batch the origin could not fund invents resources, and the invented cargo
    then shows up as overflow at the far end.

    ``net_per_hour`` is each village's OWN production. The routes are applied
    here as discrete events, so folding them into the rate as well would count
    every delivery twice.

    ``consumption`` is what each village SPENDS per hour, subtracted from that
    production. Without it a village's allocation target reads as permanent
    accumulation, and an army village told to land what it burns is reported as
    losing ``target x 24`` a day at a cap it never actually reaches. Absent, the
    rate is production alone and this is the pre-consumption replay exactly.

    ``npc`` is section 7's balancing, per village: a buffer level a departure
    may be topped back up to, out of a budget that **accrues at the allowance
    and exhausts**. The previous version topped the store back to its floor
    however often it was asked -- an infinite reservoir, which reported cargo
    nobody could have funded. Here the top-up is bounded by the accrued budget
    AND by what the feedstock stores actually hold, it is booked as an inflow so
    ``net_gain_per_day`` stays true, and the feedstock is debited by exactly
    what it paid for. That last debit is what makes a granary trigger honest:
    crop converted into wood is crop the granary no longer holds.
    """
    net_per_hour = _net_of_consumption(net_per_hour, consumption)
    # (route index, firing index, resource) -> what actually left the origin.
    firings: list[tuple[int, int, int, int, Resource, float]] = []

    def _survives(minute: int) -> bool:
        """Whether a departure at this minute-of-day will exist after pruning.

        Passed a window only when the caller will actually delete the rows
        outside it (prune_to_window). Simulating pruned firings reported traffic
        that never moves: an hourly 8-hour profile keeps 8 of its 24 rows, and
        replaying all 24 tripled every flow into false overflow and starvation.
        """
        if dispatch_window is None:
            return True
        start, end = dispatch_window
        m = minute % MINUTES_PER_DAY
        if start <= end:
            return start <= m < end
        return m >= start or m < end

    for scheduled in beat.routes:
        dispatches = scheduled.dispatch_minutes
        arrivals = scheduled.arrival_minutes
        for out_minute, in_minute in zip(dispatches, arrivals, strict=True):
            if not _survives(out_minute):
                continue
            for resource, amount in scheduled.route.batch_per_resource.items():
                firings.append(
                    (
                        out_minute,
                        in_minute,
                        scheduled.route.origin,
                        scheduled.route.destination,
                        resource,
                        amount,
                    )
                )

    departures: dict[int, list[tuple[int, int, int, Resource, float]]] = {}
    arrivals_at: dict[int, list[int]] = {}
    for index, (out_minute, in_minute, origin, destination, resource, amount) in enumerate(firings):
        departures.setdefault(out_minute, []).append((index, origin, destination, resource, amount))
        arrivals_at.setdefault(in_minute, []).append(index)

    level: dict[tuple[int, Resource], float] = {}
    for vid, per_resource in stocks.items():
        for resource, amount in per_resource.items():
            level[(vid, resource)] = float(amount)

    def cap_for(vid: int, resource: Resource) -> float | None:
        value = capacities.get(vid, {}).get(resource)
        return None if value is None else float(value)

    wasted: dict[tuple[int, Resource], float] = {}
    first_full: dict[tuple[int, Resource], int] = {}
    # Cargo that arrived minus cargo that left, per store, over the day. Kept
    # apart from production, which needs no accumulation: the day adds
    # ``rate * step`` a fixed number of times, so its total is arithmetic. That
    # matters -- production is added ~28,800 times a day on a 25-village
    # account, and folding a second dict update into that loop cost 34%.
    moved: dict[tuple[int, Resource], float] = {}
    # Section 7's reserves, and the conversion budget each one has accrued so
    # far. The budget starts EMPTY: nothing has been traded yet, which is the
    # conservative direction, and the replay runs to a steady state anyway.
    reserves: Mapping[int, NpcReserve] = npc or {}
    budget: dict[int, float] = {}
    in_flight: dict[int, float] = {}
    previous_close: dict[tuple[int, Resource], float] | None = None
    previous_budget: dict[int, float] | None = None
    production_steps = len(range(0, MINUTES_PER_DAY, step_minutes))
    # A flag rather than the `for/else` it looks like: the only reader is the
    # `if not settled` block far below, which needs `events` -- built from the
    # loop's own last day -- so an `else:` clause would run before its input.
    settled = False

    for _day in range(MAX_SETTLING_DAYS):
        wasted = {}
        first_full = {}
        moved = {}

        def add(vid: int, resource: Resource, amount: float, minute: int) -> None:
            key = (vid, resource)
            cap = cap_for(vid, resource)
            updated = level.get(key, 0.0) + amount
            if cap is not None and updated > cap:
                wasted[key] = wasted.get(key, 0.0) + (updated - cap)  # noqa: B023
                first_full.setdefault(key, minute)  # noqa: B023
                updated = cap
            level[key] = max(0.0, updated)

        for minute in range(0, MINUTES_PER_DAY, step_minutes):
            for vid, per_resource in net_per_hour.items():
                for resource, rate in per_resource.items():
                    if rate:
                        add(vid, resource, rate * step_minutes / 60.0, minute)
            if reserves:
                _accrue(reserves, budget, step_minutes / 60.0)
            for event_minute in range(minute, min(minute + step_minutes, MINUTES_PER_DAY)):
                # Arrivals before departures within the same minute: cargo that
                # lands now is available to a route leaving now, which is what
                # the beat's collect-then-ship ordering is arranging for.
                for index in arrivals_at.get(event_minute, ()):
                    _o, _i, _origin, destination, resource, batch = firings[index]
                    landed = in_flight.get(index, batch)
                    add(destination, resource, landed, event_minute)
                    key = (destination, resource)
                    moved[key] = moved.get(key, 0.0) + landed
                for index, origin, _dest, resource, batch in departures.get(event_minute, ()):
                    key = (origin, resource)
                    # An operator who NPCs the warehouse back up when it dips
                    # below its floor funds this departure -- but only as far as
                    # the conversion budget and the feedstock actually go. Booked
                    # as an inflow at the destination store and a debit at the
                    # source ones, so the day's net gain stays true at both ends
                    # and the granary really does shrink by what it paid.
                    reserve = reserves.get(origin)
                    if reserve is not None and resource in MATERIALS:
                        funded, paid = _npc_top_up(
                            reserve,
                            resource,
                            level,
                            budget,
                            batch + reserve.floor_level - level.get(key, 0.0),
                        )
                        if funded:
                            # What LANDED, not what was funded: NPC cannot
                            # exceed a store's cap either (reference I.6.4), so
                            # a top-up into a nearly full warehouse clamps -- and
                            # booking the pre-clamp figure would leave
                            # net_gain_per_day describing resources that were
                            # discarded on arrival.
                            before = level.get(key, 0.0)
                            add(origin, resource, funded, event_minute)
                            moved[key] = moved.get(key, 0.0) + (level.get(key, 0.0) - before)
                            for source, amount in paid.items():
                                add(origin, source, -amount, event_minute)
                                source_key = (origin, source)
                                moved[source_key] = moved.get(source_key, 0.0) - amount
                    available = level.get(key, 0.0)
                    shipped = min(batch, available)
                    level[key] = available - shipped
                    in_flight[index] = shipped
                    moved[key] = moved.get(key, 0.0) - shipped

        closing = dict(level)
        # The conversion budget is part of the closing state: a day that ended
        # with the reserve half spent is not the same day as one that ended with
        # it full, and calling the first settled would report a loss figure
        # measured while the reservoir was still draining.
        closing_budget = dict(budget)
        if (
            previous_close is not None
            and all(
                abs(closing.get(key, 0.0) - previous_close.get(key, 0.0)) < 1e-6
                for key in set(closing) | set(previous_close)
            )
            and all(
                abs(closing_budget.get(vid, 0.0) - (previous_budget or {}).get(vid, 0.0)) < 1e-6
                for vid in closing_budget
            )
        ):
            settled = True
            break
        previous_close = closing
        previous_budget = closing_budget

    def net_gain_per_day(vid: int, resource: Resource) -> float:
        per_step = net_per_hour.get(vid, {}).get(resource, 0.0) * step_minutes / 60.0
        return per_step * production_steps + moved.get((vid, resource), 0.0)

    events: dict[tuple[int, Resource], OverflowEvent] = {
        (vid, resource): OverflowEvent(
            village_id=vid,
            resource=resource,
            minute=first_full[(vid, resource)],
            wasted_per_day=amount,
            net_gain_per_day=net_gain_per_day(vid, resource),
        )
        for (vid, resource), amount in wasted.items()
        if amount >= MIN_REPORTED_WASTE
    }
    if not settled:
        # The horizon ran out before the day repeated, so the last day is not a
        # settled one and what it clamped is not the recurring figure: a store
        # that reaches its cap on day 15 clamped nothing, one that got there at
        # noon on day 14 clamped half a day. Physics answers instead. A store
        # gaining more per day than it passes on WILL sit at its cap, and once
        # there everything beyond what leaves is lost -- so its recurring loss
        # is its net gain, and the horizon only decided whether the replay got
        # to watch it.
        for vid, per_resource in capacities.items():
            for resource, cap in per_resource.items():
                key = (vid, resource)
                gain = net_gain_per_day(vid, resource)
                # A day that clamped within MIN_REPORTED_WASTE of the gain was
                # spent at the cap, and the gap is float residue: that observed
                # event already IS the recurring loss, so it stands.
                if gain <= MIN_REPORTED_WASTE or gain - wasted.get(key, 0.0) < MIN_REPORTED_WASTE:
                    continue
                days_to_cap = MAX_SETTLING_DAYS + (cap - level.get(key, 0.0)) / gain
                # A store more than a month out is not a defect in THIS plan.
                # Production, troops and targets all move first, so projecting a
                # loss that far ahead reported accounts as broken over stores
                # 130 days from trouble.
                #
                # Rounded before comparing, and rounded the same way the message
                # states it (`:.0f`, i.e. round-half-even, as `round` is). The
                # accumulation leaves float residue -- 40,000 rising at 500/h
                # lands on 30.000000000000004 days -- so an exact comparison
                # dropped a store the report would have called "about 30 days"
                # while keeping one at 29.99.
                if round(days_to_cap) > MAX_PROJECTION_DAYS:
                    continue
                events[key] = OverflowEvent(
                    village_id=vid,
                    resource=resource,
                    # Structural by construction, so as for any structural event
                    # this is not the time of anything. Where the last day did
                    # clamp it is at least that day's real first-full minute
                    # rather than an invented one.
                    minute=first_full.get(key, 0),
                    wasted_per_day=gain,
                    net_gain_per_day=gain,
                    days_to_cap=days_to_cap,
                )

    return tuple(
        sorted(
            events.values(),
            key=lambda event: (-event.wasted_per_day, event.village_id, event.resource.value),
        )
    )


def _store_name(resource: Resource) -> str:
    return "granary" if resource is Resource.CROP else "warehouse"


def storage_findings(
    statuses: Sequence[StoreStatus],
    overflows: Sequence[OverflowEvent],
    warn_hours: float = DEFAULT_WARN_HOURS,
    names: Mapping[int, str] | None = None,
    crop_negative_by_design: Collection[int] = (),
) -> tuple[Finding, ...]:
    """Structured store findings. Starvation first: it destroys troops, not surplus.

    A store with a reported overflow is *not* also reported as filling up. The
    two checks look at the same store from different angles -- the continuous
    one says it will be full in 1.1h, the discrete one says it is full and
    costing 22,224/day -- and emitting both made half of a 132-line warning list
    a restatement of the other half. The overflow line strictly dominates: it
    says the cap is reached AND what that costs.

    ``crop_negative_by_design`` names the villages the operator has declared
    permanently crop-negative (profile sections 9.1-9.2: the Hammer and the
    troops-only village eat more than they grow, every day, on purpose). Their
    countdown is reported as a NOTE instead of a CRITICAL -- the same fact, the
    same figures, without the claim that something is going wrong. It is a
    downgrade and never a suppression: the hours of cover are the one number
    review R7 exists to produce, and they say how long the granary lasts if the
    deliveries stop. Empty by default, so nothing declared plans exactly as
    before, and only the villages named are affected -- an undeclared village
    draining at the same rate keeps its CRITICAL.
    """
    overflowing = {(event.village_id, event.resource) for event in overflows}
    findings: list[Finding] = []
    for status in sorted(
        (
            s
            for s in statuses
            # CROP only. Starvation is a troop mechanic and troops eat crop: an
            # empty warehouse stops construction, it does not kill an army. Emitting
            # STARVATION for lumber, clay or iron put a CRITICAL finding under a name
            # that misdescribes it, and a critical signal that cries wolf is worse
            # than none. Non-crop depletion is already covered as a shortfall against
            # the allocation.
            if s.resource is Resource.CROP
            and s.trend is Trend.DRAINING
            and s.hours_remaining is not None
        ),
        key=lambda s: s.hours_remaining or 0.0,
    ):
        if (status.hours_remaining or 0.0) < warn_hours:
            label = village_label(status.village_id, names)
            if status.village_id in crop_negative_by_design:
                # Same facts, different tense. "Runs out in 17.0h" describes an
                # accident; "drains by design, 17.0h in store" describes the
                # arrangement AND still hands over the 17 hours, which is what
                # the operator has to act inside either way.
                findings.append(
                    Finding(
                        category=Category.STARVATION_BY_DESIGN,
                        message=(
                            f"{label}: {status.resource.value} drains at "
                            f"{status.net_per_hour:+,.0f}/h by design; "
                            f"{status.hours_remaining:.1f}h in store "
                            f"({status.stock:,} left)"
                        ),
                        detail=f"{label} — {status.hours_remaining:.1f}h of cover",
                        village=label,
                        resource=status.resource,
                    )
                )
                continue
            findings.append(
                Finding(
                    category=Category.STARVATION,
                    message=(
                        f"{label}: {status.resource.value} runs out in "
                        f"{status.hours_remaining:.1f}h at {status.net_per_hour:+.0f}/h "
                        f"({status.stock:,} in store)"
                    ),
                    detail=f"{label} — {status.hours_remaining:.1f}h left",
                    village=label,
                    resource=status.resource,
                )
            )
    for status in sorted(
        (s for s in statuses if s.trend is Trend.FILLING and s.hours_remaining is not None),
        key=lambda s: s.hours_remaining or 0.0,
    ):
        if (status.hours_remaining or 0.0) < warn_hours and (
            status.village_id,
            status.resource,
        ) not in overflowing:
            label = village_label(status.village_id, names)
            findings.append(
                Finding(
                    category=Category.STORE_FILLING,
                    message=(
                        f"{label}: {status.resource.value} fills its store in "
                        f"{status.hours_remaining:.1f}h at {status.net_per_hour:+.0f}/h; "
                        f"anything past the cap is lost"
                    ),
                    detail=f"{label} — full in {status.hours_remaining:.1f}h",
                    village=label,
                    resource=status.resource,
                )
            )
    for event in overflows:
        label = village_label(event.village_id, names)
        if event.projected:
            # The replay never saw this day: the cap is past the horizon, and
            # the loss is what the store will shed once it sits there.
            #
            # Its own WARNING category, and no `loss_per_day`. A projected event
            # used to land in OVERFLOW_STRUCTURAL, which is CRITICAL and whose
            # figures are summed into "This account loses N resources a day at
            # its store caps" -- so one quiet village 20 days from its cap
            # produced a red headline about a 24,000/day loss that had not
            # started. The 30-day bound above fixed the horizon and not the
            # tense. The figure stays visible in the message and the detail;
            # excluded from the account total is not the same as hidden.
            category = Category.OVERFLOW_PROJECTED
            loss_per_day = 0.0
            detail = f"{label} — {event.wasted_per_day:,.0f}/day in ~{event.days_to_cap:.0f} days"
            message = (
                f"{label}: {event.resource.value} will reach its cap in about "
                f"{event.days_to_cap:.0f} days and then lose about "
                f"{event.wasted_per_day:,.0f}/day — the {_store_name(event.resource)} will "
                f"never leave its cap, because {event.net_gain_per_day:,.0f}/day more arrives "
                f"than leaves"
            )
        elif event.structural:
            # No clock: a store that never leaves its cap did not "reach" it at a
            # minute of the day, and reporting 00:00 as an event time sent the
            # operator looking for a midnight arrival that does not exist.
            category = Category.OVERFLOW_STRUCTURAL
            loss_per_day = event.wasted_per_day
            detail = f"{label} — {event.wasted_per_day:,.0f}/day"
            message = (
                f"{label}: {event.resource.value} hits the cap and loses about "
                f"{event.wasted_per_day:,.0f}/day — the {_store_name(event.resource)} never "
                f"leaves its cap, because {event.net_gain_per_day:,.0f}/day more arrives "
                f"than leaves"
            )
        else:
            category = Category.OVERFLOW_BURST
            loss_per_day = event.wasted_per_day
            detail = f"{label} — {event.wasted_per_day:,.0f}/day"
            message = (
                f"{label}: {event.resource.value} hits the cap at "
                f"{event.minute // 60:02d}:{event.minute % 60:02d} and loses about "
                f"{event.wasted_per_day:,.0f}/day — an arriving batch overflows even "
                f"though the average rate fits"
            )
        findings.append(
            Finding(
                category=category,
                message=message,
                detail=detail,
                village=label,
                resource=event.resource,
                loss_per_day=loss_per_day,
            )
        )
    return tuple(findings)


def storage_warnings(
    statuses: Sequence[StoreStatus],
    overflows: Sequence[OverflowEvent],
    warn_hours: float = DEFAULT_WARN_HOURS,
    names: Mapping[int, str] | None = None,
) -> tuple[str, ...]:
    """The same findings as the flat prose list callers have always read."""
    return tuple(
        finding.message
        for finding in storage_findings(statuses, overflows, warn_hours=warn_hours, names=names)
    )


def relay_buffer_findings(
    hubs: Sequence[RelayHub],
    overflows: Sequence[OverflowEvent],
    beat: Beat,
    capacities: Mapping[int, Mapping[Resource, int]],
    names: Mapping[int, str] | None = None,
) -> list[Finding]:
    """Can each declared material relay's WAREHOUSE hold its pass-through?

    This is the check that had material relay deferred once before, and it is
    not a refinement of the generic overflow report. A relay does not merely
    receive -- it holds cargo it never grew between collecting and forwarding,
    and a store that tops out in between destroys the difference.

    The law, and the two readings of it that are both wrong. What the warehouse
    must hold is

        **the collecting rate times the LONGER of the two cycles.**

    Not "one collecting batch": that is ``rate x collect_cycle``, and it is the
    answer only when the forward leg is no slower. Not "the pass-through between
    two forward sends" either: that is ``rate x forward_cycle``, and it is the
    answer only when the COLLECTING leg is no slower. Both regimes are real. On
    the operator's own geometry the forward leg is the slower one -- 02 -> 18 is
    one field and costs least on 1h, 18 -> 11 is seventeen and costs least on 2h
    -- so two batches land between forward sends and the relay holds both:
    silent at 33,488 (16,744/h x 2h) and reporting at 32,988, while one batch is
    16,744. Move the relay far from its source and the other regime appears:
    measured at collect 1,000/h on a 2h cycle against a 1h forward cycle, the
    finding is silent at a 2,000 warehouse and critical at 1,800, where
    ``rate x forward_cycle`` is 500.

    The deferral's own figure -- the capital at 1,200,000, a neighbour at
    160,000, "it fills in under 7 hours of an 8-hour night" -- assumed the relay
    does not forward while it collects, which is the defect the collect-then-ship
    generalisation in :func:`~.schedule.build_beat` fixed for materials. With the
    forward legs phased after the collecting arrival, 160,000 is many times this
    tier's pass-through and never troubles it.

    Read off the replay rather than re-simulated: :func:`simulate_day` already
    walks the beat against real capacities and records the minute each store
    first reached its cap, which is exactly the fact this needs. A second
    simulation with its own assumptions is how /plan and /day-check came to
    answer the same account differently once before.

    That inheritance also bounds what this can see, and the bound is worth
    stating: ``simulate_day`` reports only waste that survives to a **settled**
    day, so the figures here are recurring rates. A relay that starts nearly
    full sheds one pass-through on day one and then settles -- measured on a
    160,000 warehouse holding 150,000, a 6,744 shed that this reports as silent,
    and at 159,999 a 16,743 shed, also silent. That is a real one-off cost and
    it is deliberately not this finding: it is bounded by a single pass-through,
    it is fixed by draining the village once rather than by changing the tier,
    and the continuous fill-time check is what reports it. So "capacity, not
    free space" holds for the RECURRING figure. In steady state the trough
    settles at ``cap - peak`` wherever that is positive, and only a capacity
    below the peak sheds something every cycle.

    **Only overflow the pass-through can explain.** A relay forwards everything
    it collects -- the tier nets zero at the hub by construction, and
    :func:`~.optimizer._relay_tier_flows` asserts it -- so the pass-through
    cannot be what makes a store gain more every day than it sheds. Where the
    replay reports a *structural* event (the store never leaves its cap) and the
    warehouse is big enough to hold the pass-through, what fills it is the
    relay's OWN retention or production, and ``storage_findings`` already names
    that with the right cause: "N/day more arrives than leaves". Measured before
    this gate: a relay whose 100,000 warehouse held its 10,000 pass-through ten
    times over got a CRITICAL blaming the tier for 240,000/day that was entirely
    its own 10,000/h target, and prescribed three fixes none of which was the
    one.

    A *burst* event is the opposite case and is always this function's: the
    average fits and one delivery does not, and the delivery is the tier's.

    Severity turns on **whether anything had left yet**, which is the difference
    between destroyed cargo and a scheduling cost:

    * a store pinned at its cap all day -> it cannot pass on what keeps landing,
      so everything above the cap is destroyed at the relay.
      :attr:`Category.RELAY_BUFFER`, critical. No clock: a store that never
      leaves its cap did not reach it at a minute of the day, exactly as
      :func:`storage_findings` says for ``OVERFLOW_STRUCTURAL``. Nor does it
      claim nothing was forwarded -- measured on a 12,000 warehouse, 401,856/day
      landed and 288,000/day left again.
    * a burst that fills before the forward leg's first send -> the relay never
      passed any of it on. :attr:`Category.RELAY_BUFFER`, critical.
    * a burst that fills after a forward send -> the tier IS delivering, and the
      warehouse tops out afterwards and sheds what lands next.
      :attr:`Category.RELAY_BUFFER_TIGHT`, a warning.

    Crop hubs are not this function's business. A granary filling on a relay is
    the ordinary overflow :func:`storage_findings` already reports, and the crop
    hub carries its own guard -- :func:`~.optimizer._may_relay_through` refuses
    a village that is losing crop. The declared material tier had neither.

    **These findings carry no ``loss_per_day``.** The cargo is real and the
    figure is in every message, but ``storage_findings`` has already billed the
    same event to the account and ``Diagnostics.total_loss_per_day`` is a plain
    sum. See the comment at the ``Finding`` below.
    """
    filled = {
        (event.village_id, event.resource): event
        for event in overflows
        if event.wasted_per_day >= MIN_REPORTED_WASTE
    }
    # Paired with the event's waste so the list can still be ordered worst
    # first: the findings themselves carry no loss figure (see below).
    findings: list[tuple[float, Finding]] = []
    for relay in hubs:
        if relay.resource not in MATERIALS:
            continue
        event = filled.get((relay.hub, relay.resource))
        if event is None:
            continue
        hub = village_label(relay.hub, names)
        # Every minute a forward leg of THIS hub actually dispatches. Taken from
        # the beat rather than from the hub's `forward_hours`, because the
        # question is ordering against a concrete minute, not a duration.
        forwards = sorted(
            minute
            for scheduled in beat.routes
            if scheduled.route.origin == relay.hub
            and scheduled.route.destination in relay.destinations
            and relay.resource in scheduled.route.cargo_per_hour
            for minute in scheduled.dispatch_minutes
        )
        first_send = forwards[0] if forwards else None
        capacity = capacities.get(relay.hub, {}).get(relay.resource)
        bound = _pass_through_bound(relay, beat)
        if (
            event.structural
            and capacity is not None
            and bound is not None
            and capacity + MIN_REPORTED_WASTE >= bound
        ):
            # The store gains every day and the warehouse holds the pass-through
            # anyway, so the surplus pinning it at its cap is the relay's own.
            # `storage_findings` reports it, with the cause that is actually
            # true.
            continue
        held = "" if capacity is None else f"{capacity:,}"
        collected = _named_villages(relay.origins, names)
        onward = _named_villages(relay.destinations, names)
        if event.structural:
            # No clock, and no claim about what was forwarded. A store that
            # never leaves its cap did not reach it at a minute of the day, and
            # it does forward -- it simply cannot keep up with what keeps
            # landing.
            message = (
                f"{hub} relays {relay.resource.value} from {collected} to {onward}, and its "
                f"{held or 'own'} warehouse cannot hold the pass-through: it sits at its cap "
                f"all day and {event.wasted_per_day:,.0f}/day is destroyed AT THE RELAY. A "
                f"relay has to hold the collecting rate over the longer of its two cycles"
            )
            category = Category.RELAY_BUFFER
        elif first_send is None or event.minute <= first_send:
            message = (
                f"{hub} relays {relay.resource.value} from {collected} to {onward}, and its "
                f"{held or 'own'} warehouse is full at "
                f"{event.minute // 60:02d}:{event.minute % 60:02d} before it has forwarded "
                f"any of it -- {event.wasted_per_day:,.0f}/day is destroyed AT THE RELAY. "
                f"A relay has to hold the pass-through between collecting and forwarding"
            )
            category = Category.RELAY_BUFFER
        else:
            message = (
                f"{hub} relays {relay.resource.value} from {collected} to {onward} and does "
                f"forward before it fills, but its {held or 'own'} warehouse tops out at "
                f"{event.minute // 60:02d}:{event.minute % 60:02d} -- after its first send "
                f"at {first_send // 60:02d}:{first_send % 60:02d} -- and sheds "
                f"{event.wasted_per_day:,.0f}/day of what lands afterwards"
            )
            category = Category.RELAY_BUFFER_TIGHT
        findings.append(
            (
                event.wasted_per_day,
                Finding(
                    category=category,
                    message=message,
                    detail=f"{hub} — {event.wasted_per_day:,.0f}/day of {relay.resource.value}",
                    village=hub,
                    resource=relay.resource,
                    # No `loss_per_day`, and the figure is in the message and
                    # the detail instead. `Diagnostics.total_loss_per_day` is a
                    # plain sum over the findings, and this event is ALREADY
                    # billed there by `storage_findings` -- which reports it for
                    # every account, relay or no relay, straight off
                    # `simulate_day`. Carrying it again put 113,856/day of
                    # lumber into one account's total twice.
                    #
                    # The overflow line owns the number because it is the one
                    # that always exists; this finding exists to name a CAUSE
                    # the generic line cannot, and a diagnosis destroys nothing
                    # of its own. Same treatment `OVERFLOW_PROJECTED` gets a few
                    # hundred lines up, and for the same stated reason:
                    # excluded from the account total is not the same as hidden.
                    loss_per_day=0.0,
                ),
            )
        )
    # Worst first, as every other finding list is ordered -- on the EVENT's
    # figure, which the findings themselves no longer carry.
    return [finding for _waste, finding in sorted(findings, key=lambda f: (-f[0], f[1].village))]


def _pass_through_bound(relay: RelayHub, beat: Beat) -> float | None:
    """What *relay*'s store must hold for the tier, or None if it cannot be told.

    The law from :func:`relay_buffer_findings`: the pass-through rate times the
    LONGER of the collecting and forwarding cycles. Both regimes occur, and each
    half of the bound on its own is wrong in one of them -- see that docstring.

    The rate is read off the FORWARD legs, not the collecting ones. They are the
    same figure for a pure relay, because the tier forwards everything it
    collects; where they differ, the collecting leg has the relay's own
    retention merged into it (routes carry one village pair's whole cargo), and
    the relay's own target is not something the tier asked it to hold.

    Cycles come from the beat rather than from :attr:`RelayHub.forward_hours`,
    which is a LATENCY and would size the store from travel time.
    """
    collect = [
        scheduled.route
        for scheduled in beat.routes
        if scheduled.route.destination == relay.hub
        and scheduled.route.origin in relay.origins
        and relay.resource in scheduled.route.cargo_per_hour
    ]
    forward = [
        scheduled.route
        for scheduled in beat.routes
        if scheduled.route.origin == relay.hub
        and scheduled.route.destination in relay.destinations
        and relay.resource in scheduled.route.cargo_per_hour
    ]
    if not collect or not forward:
        return None
    rate = sum(route.cargo_per_hour[relay.resource] for route in forward)
    window = max(
        max(route.cycle_hours for route in collect),
        max(route.cycle_hours for route in forward),
    )
    return rate * window


def _named_villages(village_ids: Sequence[int], names: Mapping[int, str] | None) -> str:
    """Village names for a message, abridged once the list stops being readable.

    Same shape as :func:`~.optimizer._named`, kept here rather than imported so
    this module keeps depending on the optimizer for types only.
    """
    labels = [village_label(vid, names) for vid in village_ids]
    if len(labels) <= 3:
        return ", ".join(labels)
    return ", ".join(labels[:3]) + f" and {len(labels) - 3} more"


@dataclass(frozen=True)
class ProfileSegment:
    """One allocation profile's share of the day, as the routes it runs.

    ``start_minute``/``end_minute`` are minutes past midnight; a segment may
    wrap (start > end). ``routes`` is the profile's own beat -- the scheduled
    routes its plan produced.

    Routes rather than a netted per-village rate, because a rate cannot say
    *when* cargo lands. A route dispatched at 22:00 that travels 100 minutes
    arrives at 23:40, in the next profile's hours, and the destination's stock
    rises then -- not spread across the sending profile's afternoon. See
    :func:`simulate_profile_cycle`.
    """

    name: str
    start_minute: int
    end_minute: int
    routes: tuple[ScheduledRoute, ...] = ()
    manual_rates: Mapping[int, Mapping[Resource, float]] = field(default_factory=dict)
    """Continuous per-village rates this profile runs that are NOT routes.

    A hand-shipped obligation -- a tribute Travian will not let a Gold Club
    route target, say -- has no route and therefore no dispatch or travel time
    to model, so a rate confined to the profile's hours is the honest shape for
    it. Leaving it out entirely would flatter the day by the whole obligation.
    """

    npc_attended: bool = True
    """Whether the operator is at the marketplace during this profile's hours.

    Section 7's conversion is a manual action, so a profile the operator sleeps
    through accrues no conversion budget at all -- the night one, on this
    account. Nothing here guesses which that is: the endpoint refuses a request
    that declares a floor and does not say, so by the time a segment reaches
    this replay the answer came from the operator.
    """

    def covers(self, minute: int) -> bool:
        if self.start_minute == self.end_minute:
            return False  # zero-width: validated away upstream, inert here
        if self.start_minute < self.end_minute:
            return self.start_minute <= minute < self.end_minute
        return minute >= self.start_minute or minute < self.end_minute


@dataclass(frozen=True)
class TrajectoryBreach:
    """A stock line crossing something it should not, with when and why."""

    village_id: int
    resource: Resource
    kind: str  # "ceiling" | "above" | "capacity" | "empty"
    day: int
    minute: int
    segment: str
    """Which profile was running when the line crossed."""


@dataclass(frozen=True)
class VillageTrajectory:
    """One village's crop-store day at steady state (or while still drifting)."""

    village_id: int
    resource: Resource
    daily_net: float
    """The UNCLAMPED nominal drift per day. Deliberately not measured off the
    simulated levels: a store that hits its cap clamps there and its measured
    net collapses to zero, which reads as "stable" about a village that is in
    fact overflowing every day. The nominal keeps telling the truth."""
    low: float
    high: float
    settled: bool

    segment_opening: Mapping[str, float] = field(default_factory=dict)
    """This store's level as each profile took over, on the measured day.

    The hand-over stock, keyed by profile name. ``low``/``high`` say how far the
    store swings and ``daily_net`` where it is heading, but section 6 asks two
    questions neither can answer: is this village at 60% of both stores when the
    day profile takes over at 07:00, and was it below 25% when the night one
    did at 23:00? Both are the level at one named minute, so the replay records
    it rather than a second simulation being written to find out.

    Sampled BEFORE the boundary minute's own dispatches, arrivals and
    production, so it is the stock the outgoing profile finished with. The
    distinction is not academic for the morning: a night route landing at 07:00
    sharp would otherwise count toward a floor the plan is supposed to have
    reached without it -- and a route still delivering at the switch is already
    a ``NIGHT_OVERRUN``, which is the finding that case belongs to.
    """


def simulate_profile_cycle(
    segments: Sequence[ProfileSegment],
    own_rates: Mapping[int, Mapping[Resource, float]],
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    ceilings: Mapping[int, float] | None = None,
    step_minutes: int = 5,
    max_days: int = 45,
    consumption: Mapping[int, Mapping[Resource, float]] | None = None,
    npc: Mapping[int, NpcReserve] | None = None,
) -> tuple[list[VillageTrajectory], list[TrajectoryBreach]]:
    """Replay a day that switches between allocation profiles.

    Each profile is planned in isolation, but the account lives through all of
    them every day: what the day profile ships decides the stock the night
    profile starts from. This runs the composite -- production always on, each
    profile's routes dispatching only inside its own hours -- so questions like
    "does the capital cross 90k during the night?" have an answer with an hour
    on it.

    **Cargo is attributed by impact, not by dispatch.** A load that leaves at
    22:00 under Day and travels 100 minutes lands at 23:40, under Night: it is
    Night's stock that rises, and Night that is named in any breach it causes.
    Only the *dispatch* keeps the sending profile's hours, because that is when
    the origin actually parts with the cargo. Modelling a profile's shipping as
    a rate confined to its own hours drops those boundary-crossing deliveries
    entirely, understating the night's inflow -- the optimistic direction for an
    overnight overflow, and so the wrong way to be wrong.

    **Cargo is conserved.** A dispatch takes what the origin actually has, and
    the matching arrival delivers exactly that. Crediting a batch the origin
    could not fund invents resources, and the invention resurfaces as overflow
    at the far end.

    ``ceilings`` is an operator-set alert level for CROP, below capacity --
    typically an NPC trigger. Crossing it (from below, on the post-clamp level)
    is reported with the day, the minute, and the profile that was running at
    the moment of impact; a store that already starts the day above it is
    reported once as kind "above".

    ``consumption`` is what each village SPENDS per hour, subtracted from
    ``own_rates`` before anything is replayed -- so the "runs dry" test sees the
    NET sign. Read off production alone it would miss the operator's own army
    village, whose lumber and crop both read gross positive while it is in fact
    5,880/h short: the granary would empty and nothing would say so.

    ``npc`` is section 7's balancing, per village, and it is here for the same
    reason ``consumption`` is: one replay given a mechanism and not the other is
    how /plan and /day-check came to answer the same account differently. The
    reservoir is finite in both, and here the budget accrues only while an
    ATTENDED profile is running -- see :attr:`ProfileSegment.npc_attended`.
    Overnight the operator is asleep, so a night route funded by conversion
    finds nothing to convert, which is the whole reason attendance is stated.

    Runs until the daily trajectory repeats (steady state) or ``max_days``.
    A store still drifting at the horizon is reported unsettled with its daily
    net, which is itself the answer: it will cross everything eventually.
    """
    if step_minutes < 1:
        raise ValueError(f"step_minutes must be at least 1, got {step_minutes}")

    own_rates = _net_of_consumption(own_rates, consumption)
    ceilings = ceilings or {}

    # Every per-store lookup :func:`apply` needs, flattened to a single
    # ``(village_id, resource)`` key, and every production rate to a pre-keyed
    # triple. The simulation never settles on a real account, so all `max_days`
    # run: 45 days x ~1,000 ticks x 92 stores is ~4.1M apply() calls, and the
    # nested ``capacities[vid][resource]`` / ``ceilings[vid]`` chains inside it
    # cost 14.2M dict lookups -- 70% of the simulation. Nothing here changes
    # WHAT is looked up, only how many hops each lookup takes. Capacities stay
    # in whatever numeric type they arrived as, because the clamp compares
    # ``updated > cap`` and Python compares int to float exactly; pre-converting
    # would change that comparison rather than merely speed it up.
    caps: dict[tuple[int, Resource], int | None] = {
        (vid, resource): cap for vid, per in capacities.items() for resource, cap in per.items()
    }
    # Only crop has an alert level, so a crop-only key set replaces the
    # `resource is CROP` test and the per-village lookup behind it with one get.
    crop_ceilings: dict[tuple[int, Resource], float] = {
        (vid, Resource.CROP): ceiling for vid, ceiling in ceilings.items()
    }

    def segment_at(minute: int) -> ProfileSegment | None:
        for segment in segments:
            if segment.covers(minute):
                return segment
        return None

    def segment_name_at(minute: int) -> str:
        active = segment_at(minute)
        return active.name if active else "no profile"

    # One firing per route per cycle, kept only where the sending profile is
    # actually running: a Day route stops dispatching when Day ends, but a load
    # already in the air still lands.
    firings: list[tuple[int, int, int, int, Resource, float]] = []
    for segment in segments:
        for scheduled in segment.routes:
            pairs = zip(scheduled.dispatch_minutes, scheduled.arrival_minutes, strict=True)
            for out_minute, in_minute in pairs:
                if not segment.covers(out_minute):
                    continue
                for resource, amount in scheduled.route.batch_per_resource.items():
                    if amount > 0:
                        firings.append(
                            (
                                out_minute,
                                in_minute,
                                scheduled.route.origin,
                                scheduled.route.destination,
                                resource,
                                amount,
                            )
                        )

    departures: dict[int, list[int]] = {}
    arrivals_at: dict[int, list[int]] = {}
    for index, firing in enumerate(firings):
        departures.setdefault(firing[0], []).append(index)
        arrivals_at.setdefault(firing[1], []).append(index)

    # Tick on the production grid plus every event minute, so a dispatch or an
    # arrival lands on the minute it happens instead of being rounded into a
    # step. Ticking every minute would cost 1,440 x max_days x stores instead.
    #
    # Every profile boundary is a tick too, so `segment_opening` is sampled at
    # the minute the hand-over happens rather than at whichever grid step
    # precedes it. On the operator's own 07:00/23:00 pair those land on the
    # 5-minute grid anyway; a profile starting at 07:01 would not.
    starts = {segment.start_minute: segment.name for segment in segments}
    ticks = sorted(
        set(range(0, MINUTES_PER_DAY, step_minutes))
        | set(departures)
        | set(arrivals_at)
        | set(starts)
    )

    level: dict[tuple[int, Resource], float] = {}
    for vid, per in stocks.items():
        for resource, amount in per.items():
            level[(vid, resource)] = float(amount)

    # Only somewhere with a store is simulated. A route's destination need not
    # be a village at all: a foreign tribute joins the optimizer as a
    # negative-id pseudo-village with no production, no capacity and nothing
    # consuming it, so crediting arrivals to one grows a phantom store that can
    # never settle -- and `settled` is a single flag for the whole account, so
    # one tribute would report every real village as still drifting. The
    # dispatch still debits the origin: the cargo really does leave.
    simulated = set(stocks) | set(own_rates)

    # Own rates -- already net of consumption -- in one flat list, in the exact
    # order the nested own_rates iteration visited them: apply() records the
    # FIRST breach of each kind, so the order stores are moved in is part of the
    # output. The `draining` flag comes from that net sign, not from production,
    # which is what lets a village producing 1,000/h and spending 20,000/h be
    # reported as running dry.
    own_flat: list[tuple[tuple[int, Resource], float, bool]] = [
        ((vid, resource), own, own < 0)
        for vid, per in own_rates.items()
        for resource, own in per.items()
        if own
    ]

    nominal: dict[tuple[int, Resource], float] = {}
    breaches: list[TrajectoryBreach] = []
    breached: set[tuple[tuple[int, Resource], str]] = set()
    lows: dict[tuple[int, Resource], float] = {}
    highs: dict[tuple[int, Resource], float] = {}
    in_flight: dict[int, float] = {}
    reserves: Mapping[int, NpcReserve] = npc or {}
    budget: dict[int, float] = {}
    day_nets: dict[tuple[int, Resource], float] = {}
    # profile name -> store -> level as that profile took over, measured day.
    openings: dict[str, dict[tuple[int, Resource], float]] = {}
    settled_day = -1

    # A store that BEGINS the day above its alert level never "crosses" it --
    # for a draining store that claim would be factually inverted. Report the
    # standing condition once, as its own kind, before any simulation runs.
    for vid, ceiling in ceilings.items():
        key = (vid, Resource.CROP)
        if key in level and level[key] > ceiling:
            breaches.append(TrajectoryBreach(vid, Resource.CROP, "above", 0, 0, segment_name_at(0)))

    # Set per day below; apply() reads it through its closure to know whether
    # this day is the one being reported.
    measuring = False

    def apply(
        key: tuple[int, Resource], amount: float, minute: int, day: int, *, draining: bool
    ) -> None:
        """Move one store by *amount*, clamping as the game does, recording why.

        Takes the ``(village_id, resource)`` key its callers already hold rather
        than the pair, so the hot production path builds no tuple per call; the
        pair is unpacked only on the breach branches, which fire at most once
        per store per kind.
        """
        previous = level.get(key, 0.0)
        updated = previous + amount
        if measuring:
            # Accumulated on the measured day, not day 0. A dispatch now
            # contributes what the origin could actually FUND, so day 0's figure
            # depends on the snapshot's opening stock -- a route on a
            # just-emptied sender under-reports by up to a batch while low/high
            # describe the settled day. Measuring both on the same day makes the
            # three numbers describe one day again.
            nominal[key] = nominal.get(key, 0.0) + amount
        cap = caps.get(key)
        if cap is not None and updated > cap:
            if (key, "capacity") not in breached:
                breached.add((key, "capacity"))
                vid, resource = key
                breaches.append(
                    TrajectoryBreach(
                        vid, resource, "capacity", day, minute, segment_name_at(minute)
                    )
                )
            updated = float(cap)
        ceiling = crop_ceilings.get(key)
        # A crossing needs a below-side and an above-side: a store already
        # past the alert (reported as "above" up front) must not fire
        # "crosses" while it drains. Tested on the post-clamp value, so a
        # ceiling set above the cap can never fire at a level the store
        # cannot reach.
        if (
            ceiling is not None
            and previous - CROSSING_TOLERANCE <= ceiling < updated - CROSSING_TOLERANCE
            and (key, "ceiling") not in breached
        ):
            breached.add((key, "ceiling"))
            vid, resource = key
            breaches.append(
                TrajectoryBreach(vid, resource, "ceiling", day, minute, segment_name_at(minute))
            )
        if updated <= 0.0:
            if draining and (key, "empty") not in breached:
                breached.add((key, "empty"))
                vid, resource = key
                breaches.append(
                    TrajectoryBreach(vid, resource, "empty", day, minute, segment_name_at(minute))
                )
            updated = 0.0
        level[key] = updated

    for day in range(max_days):
        opening = dict(level)
        measuring = settled_day >= 0 or day == max_days - 1
        if measuring:
            lows = dict(level)
            highs = dict(level)
            nominal.clear()  # exactly one day's worth, this day's
        for position, minute in enumerate(ticks):
            # The hand-over stock, before this minute's own events: what the
            # profile taking over at this minute starts from.
            if measuring and minute in starts:
                openings[starts[minute]] = dict(level)
            # Dispatch: the origin parts with the cargo now, under whichever
            # profile is running now.
            for index in departures.get(minute, ()):
                _out, _in, origin, _destination, resource, amount = firings[index]
                key = (origin, resource)
                # Section 7's conversion, finite here exactly as in simulate_day:
                # what the budget has accrued and what the feedstock stores hold,
                # never a store magically restored to its floor.
                reserve = reserves.get(origin)
                if reserve is not None and resource in MATERIALS:
                    funded, paid = _npc_top_up(
                        reserve,
                        resource,
                        level,
                        budget,
                        amount + reserve.floor_level - level.get(key, 0.0),
                    )
                    if funded:
                        apply(key, funded, minute, day, draining=False)
                        for source, debit in paid.items():
                            apply((origin, source), -debit, minute, day, draining=True)
                shipped = min(amount, max(0.0, level.get(key, 0.0)))
                in_flight[index] = shipped
                if shipped:
                    apply(key, -shipped, minute, day, draining=True)
            # Impact: credited at the minute it lands, in whatever profile owns
            # that minute -- but only what the origin actually funded. A firing
            # whose arrival minute precedes its dispatch minute lands before
            # that dispatch on day 0, so nothing has been funded yet and there
            # is nothing to deliver. Standing in the nominal batch there would
            # credit cargo the origin never had, and nothing drains it again, so
            # the invention survives into the settled day and its daily net.
            for index in arrivals_at.get(minute, ()):
                _out, _in, _origin, destination, resource, _amount = firings[index]
                if destination not in simulated:
                    continue
                shipped = in_flight.get(index)
                if shipped:
                    apply((destination, resource), shipped, minute, day, draining=False)
            # Production for the span up to the next tick, always on.
            end = ticks[position + 1] if position + 1 < len(ticks) else MINUTES_PER_DAY
            span_hours = (end - minute) / 60.0
            if span_hours > 0:
                for key, own, draining in own_flat:
                    apply(key, own * span_hours, minute, day, draining=draining)
                # Hand-shipped obligations: rate-based, and only while the
                # profile that owns them is the one running.
                active = segment_at(minute)
                # Conversion capacity accrues only while somebody is awake to
                # convert. An hour with no profile at all is nobody's hour, so
                # it accrues nothing either.
                if reserves and active is not None and active.npc_attended:
                    _accrue(reserves, budget, span_hours)
                if active is not None:
                    for vid, per in active.manual_rates.items():
                        for resource, rate in per.items():
                            if rate:
                                apply(
                                    (vid, resource),
                                    rate * span_hours,
                                    minute,
                                    day,
                                    draining=rate < 0,
                                )
            if measuring:
                for key, value in level.items():
                    lows[key] = min(lows.get(key, value), value)
                    highs[key] = max(highs.get(key, value), value)
        day_nets = nominal
        if settled_day >= 0:
            break  # the settled day has now been measured; done
        if all(abs(level[key] - opening.get(key, 0.0)) < 1.0 for key in level):
            settled_day = day  # measure min/max over one more identical day

    trajectories = [
        VillageTrajectory(
            village_id=vid,
            resource=resource,
            daily_net=day_nets.get((vid, resource), 0.0),
            low=lows.get((vid, resource), level.get((vid, resource), 0.0)),
            high=highs.get((vid, resource), level.get((vid, resource), 0.0)),
            settled=settled_day >= 0,
            segment_opening={
                name: per_store[(vid, resource)]
                for name, per_store in openings.items()
                if (vid, resource) in per_store
            },
        )
        for (vid, resource) in sorted(level)
    ]
    return trajectories, breaches


@dataclass(frozen=True)
class FillAtSwitch:
    """One store's level at a profile boundary, against its capacity.

    Section 6 asks two questions about the night and both are the same shape:
    a named store, a named minute, and a fraction to compare. So one record
    answers both, and the two producers below differ only in which boundary
    they read and which way the comparison runs.
    """

    village_id: int
    resource: Resource
    stock: float
    capacity: int
    fraction: float
    """``stock / capacity``. Carried rather than recomputed by every reader, so
    the percentage the operator is shown is the one that was compared."""


def morning_floor_shortfalls(
    trajectories: Sequence[VillageTrajectory],
    capacities: Mapping[int, Mapping[Resource, int]],
    floor_villages: Collection[int],
    morning_profile: str,
    floor: float = DEFAULT_TARGET_FILL,
) -> tuple[FillAtSwitch, ...]:
    """Role villages that wake below the morning fill floor. Section 6.

    "At 07:00, every role village (DEF + OFF; capital excluded) must be at 60%
    capacity on both warehouse and granary." Read off the composite replay's
    hand-over stock, so this is the level at the switch on a REPEATING day
    rather than whatever the snapshot happened to catch.

    Per resource, not per store, and that is the honest reading of "both
    warehouse and granary": in Travian each material has its own bar against
    the warehouse's capacity, so a village at 80% clay and 10% iron is not at
    60% of its warehouse -- it cannot build with clay alone. Reporting the
    resource is also what makes the finding actionable, since the fix is a
    bigger share of that one resource.

    ``floor_villages`` is who holds the floor, decided by the caller from the
    declared roles (:func:`~.roles.keeps_a_morning_floor`). Passed in rather
    than inferred, for the reason roles exist at all: nothing in a snapshot
    says a village is the Hammer, and the crop-sign inference that used to
    stand in for it is wrong precisely on the villages this rule is about.

    A store whose capacity was never read is skipped rather than assumed: every
    figure here is a fraction OF the capacity, and inventing one decides whether
    a village is reported.
    """
    wanted = set(floor_villages)
    found: list[FillAtSwitch] = []
    for trajectory in trajectories:
        if trajectory.village_id not in wanted:
            continue
        capacity = capacities.get(trajectory.village_id, {}).get(trajectory.resource)
        if not capacity:
            continue
        stock = trajectory.segment_opening.get(morning_profile)
        if stock is None:
            continue  # the replay never handed over to a profile by that name
        # In resource units, and tolerant, for the reason CROSSING_TOLERANCE
        # exists: the replay accumulates production in fractional steps, so a
        # store standing exactly on its floor lands a few nano-resources under
        # it and would be reported as short of a target it met.
        if stock < floor * capacity - CROSSING_TOLERANCE:
            found.append(
                FillAtSwitch(
                    village_id=trajectory.village_id,
                    resource=trajectory.resource,
                    stock=stock,
                    capacity=capacity,
                    fraction=stock / capacity,
                )
            )
    return tuple(
        sorted(found, key=lambda fill: (fill.fraction, fill.village_id, fill.resource.value))
    )


def pre_night_overfills(
    trajectories: Sequence[VillageTrajectory],
    capacities: Mapping[int, Mapping[Resource, int]],
    floor_villages: Collection[int],
    night_profile: str,
    baseline: float = DEFAULT_BASELINE_FILL,
) -> tuple[FillAtSwitch, ...]:
    """Role villages that start the night fuller than the profile assumes.

    The mirror of :func:`morning_floor_shortfalls`, and deliberately NOT a
    constraint. At the day-to-night switch the operator spends the stores down
    by hand so no role village is above the baseline, and the derivation is
    entitled to start from that -- every night ceiling is the room between the
    baseline and the target. It is an assumption the OPERATOR OWNS; the planner
    is not in the room when the spending happens and cannot make it happen.

    What it can do is notice the disagreement, because the consequence is real:
    a store starting higher has less room than the night reserved for it, so
    the cargo sized for that room arrives at a cap. Hence a finding, never a
    refusal.
    """
    wanted = set(floor_villages)
    found: list[FillAtSwitch] = []
    for trajectory in trajectories:
        if trajectory.village_id not in wanted:
            continue
        capacity = capacities.get(trajectory.village_id, {}).get(trajectory.resource)
        if not capacity:
            continue
        stock = trajectory.segment_opening.get(night_profile)
        if stock is None:
            continue
        if stock > baseline * capacity + CROSSING_TOLERANCE:
            found.append(
                FillAtSwitch(
                    village_id=trajectory.village_id,
                    resource=trajectory.resource,
                    stock=stock,
                    capacity=capacity,
                    fraction=stock / capacity,
                )
            )
    return tuple(
        sorted(found, key=lambda fill: (-fill.fraction, fill.village_id, fill.resource.value))
    )


def night_state_findings(
    morning_short: Sequence[FillAtSwitch],
    pre_night_over: Sequence[FillAtSwitch],
    floor: float = DEFAULT_TARGET_FILL,
    baseline: float = DEFAULT_BASELINE_FILL,
    names: Mapping[int, str] | None = None,
) -> tuple[Finding, ...]:
    """Prose for both ends of the night, from the two computed lists.

    Shaped like :func:`storage_findings`: the arithmetic is done and this only
    decides what a person reads, so the wording cannot drift from the figure it
    describes. Both thresholds are stated in every line, because "10%" means
    nothing without the number it fell short of.
    """
    findings: list[Finding] = []
    for fill in morning_short:
        label = village_label(fill.village_id, names)
        store = _store_name(fill.resource)
        findings.append(
            Finding(
                category=Category.MORNING_FLOOR,
                message=(
                    f"{label}: {fill.resource.value} is {fill.fraction:.0%} of its {store} "
                    f"at the morning switch, under the {floor:.0%} floor -- it wakes with "
                    f"{fill.stock:,.0f} of {fill.capacity:,} and has to wait for a "
                    f"delivery before it can spend"
                ),
                detail=f"{label} {fill.resource.value} - {fill.fraction:.0%} of {store}",
                village=label,
                resource=fill.resource,
            )
        )
    for fill in pre_night_over:
        label = village_label(fill.village_id, names)
        store = _store_name(fill.resource)
        findings.append(
            Finding(
                category=Category.PRE_NIGHT_BASELINE,
                message=(
                    f"{label}: {fill.resource.value} is {fill.fraction:.0%} of its {store} "
                    f"as the night starts, above the {baseline:.0%} baseline the profile "
                    f"assumes -- the room reserved to fill it is not there, so the night's "
                    f"cargo arrives at a cap"
                ),
                detail=f"{label} {fill.resource.value} - {fill.fraction:.0%} of {store}",
                village=label,
                resource=fill.resource,
            )
        )
    return tuple(findings)
