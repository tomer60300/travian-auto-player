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

Pure functions over already-fetched state. Nothing here spends a game request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .allocation import Resource, village_label
from .schedule import MINUTES_PER_DAY, Beat

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
    """Minute of the day the cap was first reached."""
    wasted_per_day: float
    """Resources lost to the cap on a settled day -- a genuinely recurring rate.

    Deliberately measured after the simulation has reached a steady state. Read
    off day one it would be an artifact of whatever the store happened to hold
    at snapshot time: a village sitting near its cap loses one batch, settles,
    and never loses anything again, which is a different problem with a
    different fix and is already covered by the continuous fill-time check.
    """


# Waste below this is float residue from the settling loop, not a loss. A
# store that converges to exactly its cap leaves a few nano-resources of
# rounding behind, and reporting that as a daily loss is noise.
MIN_REPORTED_WASTE = 1.0

# Days to run before believing the numbers. Convergence is normally immediate
# once the transient has drained; this only bounds a pathological input.
MAX_SETTLING_DAYS = 14


def simulate_day(
    beat: Beat,
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    net_per_hour: Mapping[int, Mapping[Resource, float]],
    step_minutes: int = 5,
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
    for scheduled in beat.routes:
        dispatches = scheduled.dispatch_minutes
        arrivals = scheduled.arrival_minutes
        for out_minute, in_minute in zip(dispatches, arrivals, strict=True):
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
    in_flight: dict[int, float] = {}
    previous_close: dict[tuple[int, Resource], float] | None = None

    for _day in range(MAX_SETTLING_DAYS):
        wasted = {}
        first_full = {}

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
                    add(destination, resource, in_flight.get(index, batch), event_minute)
                for index, origin, _dest, resource, batch in departures.get(event_minute, ()):
                    available = level.get((origin, resource), 0.0)
                    shipped = min(batch, available)
                    in_flight[index] = shipped
                    level[(origin, resource)] = available - shipped

        closing = dict(level)
        if previous_close is not None and all(
            abs(closing.get(key, 0.0) - previous_close.get(key, 0.0)) < 1e-6
            for key in set(closing) | set(previous_close)
        ):
            break
        previous_close = closing

    return tuple(
        sorted(
            (
                OverflowEvent(
                    village_id=vid,
                    resource=resource,
                    minute=first_full[(vid, resource)],
                    wasted_per_day=amount,
                )
                for (vid, resource), amount in wasted.items()
                if amount >= MIN_REPORTED_WASTE
            ),
            key=lambda event: (-event.wasted_per_day, event.village_id, event.resource.value),
        )
    )


def storage_warnings(
    statuses: Sequence[StoreStatus],
    overflows: Sequence[OverflowEvent],
    warn_hours: float = DEFAULT_WARN_HOURS,
    names: Mapping[int, str] | None = None,
) -> tuple[str, ...]:
    """Operator-facing lines. Starvation first: it destroys troops, not surplus."""
    warnings: list[str] = []
    for status in sorted(
        (s for s in statuses if s.trend is Trend.DRAINING and s.hours_remaining is not None),
        key=lambda s: s.hours_remaining or 0.0,
    ):
        if (status.hours_remaining or 0.0) < warn_hours:
            warnings.append(
                f"{village_label(status.village_id, names)}: {status.resource.value} runs out in "
                f"{status.hours_remaining:.1f}h at {status.net_per_hour:+.0f}/h "
                f"({status.stock:,} in store)"
            )
    for status in sorted(
        (s for s in statuses if s.trend is Trend.FILLING and s.hours_remaining is not None),
        key=lambda s: s.hours_remaining or 0.0,
    ):
        if (status.hours_remaining or 0.0) < warn_hours:
            warnings.append(
                f"{village_label(status.village_id, names)}: {status.resource.value} "
                f"fills its store in "
                f"{status.hours_remaining:.1f}h at {status.net_per_hour:+.0f}/h; "
                f"anything past the cap is lost"
            )
    for event in overflows:
        warnings.append(
            f"{village_label(event.village_id, names)}: {event.resource.value} hits the cap at "
            f"{event.minute // 60:02d}:{event.minute % 60:02d} and loses about "
            f"{event.wasted_per_day:,.0f}/day — an arriving batch overflows even "
            f"though the average rate fits"
        )
    return tuple(warnings)
