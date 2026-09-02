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

Pure functions over already-fetched state. Nothing here spends a game request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .allocation import Resource, village_label
from .findings import Category, Finding
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


def simulate_day(
    beat: Beat,
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    net_per_hour: Mapping[int, Mapping[Resource, float]],
    step_minutes: int = 5,
    dispatch_window: tuple[int, int] | None = None,
    floors: Mapping[int, Mapping[Resource, float]] | None = None,
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
    """
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
    _floors: Mapping[int, Mapping[Resource, float]] = floors or {}

    def floor_for(vid: int, resource: Resource) -> float | None:
        return _floors.get(vid, {}).get(resource)

    moved: dict[tuple[int, Resource], float] = {}
    in_flight: dict[int, float] = {}
    previous_close: dict[tuple[int, Resource], float] | None = None
    production_steps = len(range(0, MINUTES_PER_DAY, step_minutes))
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
                    available = level.get(key, 0.0)
                    # An operator who NPCs the warehouse back up whenever it dips
                    # below its floor really does hold that floor, every hour, so
                    # a departure always finds its full batch. One dict lookup;
                    # this branch runs per departure, not per step.
                    floor = floor_for(origin, resource)
                    if floor is None:
                        shipped = min(batch, available)
                        level[key] = available - shipped
                    else:
                        shipped = batch
                        level[key] = max(floor, available - batch)
                    in_flight[index] = shipped
                    moved[key] = moved.get(key, 0.0) - shipped

        closing = dict(level)
        if previous_close is not None and all(
            abs(closing.get(key, 0.0) - previous_close.get(key, 0.0)) < 1e-6
            for key in set(closing) | set(previous_close)
        ):
            settled = True
            break
        previous_close = closing

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
) -> tuple[Finding, ...]:
    """Structured store findings. Starvation first: it destroys troops, not surplus.

    A store with a reported overflow is *not* also reported as filling up. The
    two checks look at the same store from different angles -- the continuous
    one says it will be full in 1.1h, the discrete one says it is full and
    costing 22,224/day -- and emitting both made half of a 132-line warning list
    a restatement of the other half. The overflow line strictly dominates: it
    says the cap is reached AND what that costs.
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


def simulate_profile_cycle(
    segments: Sequence[ProfileSegment],
    own_rates: Mapping[int, Mapping[Resource, float]],
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    ceilings: Mapping[int, float] | None = None,
    step_minutes: int = 5,
    max_days: int = 45,
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

    Runs until the daily trajectory repeats (steady state) or ``max_days``.
    A store still drifting at the horizon is reported unsettled with its daily
    net, which is itself the answer: it will cross everything eventually.
    """
    if step_minutes < 1:
        raise ValueError(f"step_minutes must be at least 1, got {step_minutes}")

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
    ticks = sorted(
        set(range(0, MINUTES_PER_DAY, step_minutes)) | set(departures) | set(arrivals_at)
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

    # Production rates in one flat list, in the exact order the nested
    # own_rates iteration visited them: apply() records the FIRST breach of each
    # kind, so the order stores are moved in is part of the output.
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
    day_nets: dict[tuple[int, Resource], float] = {}
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
            # Dispatch: the origin parts with the cargo now, under whichever
            # profile is running now.
            for index in departures.get(minute, ()):
                _out, _in, origin, _destination, resource, amount = firings[index]
                key = (origin, resource)
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
        )
        for (vid, resource) in sorted(level)
    ]
    return trajectories, breaches
