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

from .allocation import Resource
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
    """A store that hits its cap during the simulated day."""

    village_id: int
    resource: Resource
    minute: int
    """Minute of the day the cap was first reached."""
    wasted_per_day: float
    """Resources lost to the cap over one day at this rate and beat."""


def simulate_day(
    beat: Beat,
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    net_per_hour: Mapping[int, Mapping[Resource, float]],
    step_minutes: int = 5,
) -> tuple[OverflowEvent, ...]:
    """Replay one day of the beat against real capacities. Known issue #12.

    The continuous check asks whether the *average* inflow overruns the cap. This
    asks whether the store is ever actually full, which is a different question:
    a village can sit comfortably under its cap on average and still lose a whole
    batch because a 24h route dumps it all at once.

    Production accrues continuously between steps; each scheduled arrival lands
    as a lump at its minute. Anything above the cap is lost, exactly as the game
    does it, and the loss is accumulated so the report can say what it costs per
    day rather than merely that it happened.
    """
    # Cargo leaves the origin when the merchants depart and lands at the
    # destination when they arrive. Both halves are needed: modelling only the
    # arrivals would let every sender's store grow without bound and report an
    # overflow that the outbound route is in fact preventing.
    moves: dict[int, list[tuple[int, Resource, float]]] = {}
    for scheduled in beat.routes:
        batch = scheduled.route.batch_per_resource
        for resource, amount in batch.items():
            for minute in scheduled.dispatch_minutes:
                moves.setdefault(minute, []).append((scheduled.route.origin, resource, -amount))
            for minute in scheduled.arrival_minutes:
                moves.setdefault(minute, []).append((scheduled.route.destination, resource, amount))

    level: dict[tuple[int, Resource], float] = {}
    for vid, per_resource in stocks.items():
        for resource, amount in per_resource.items():
            level[(vid, resource)] = float(amount)

    wasted: dict[tuple[int, Resource], float] = {}
    first_full: dict[tuple[int, Resource], int] = {}

    def cap_for(vid: int, resource: Resource) -> float | None:
        value = capacities.get(vid, {}).get(resource)
        return None if value is None else float(value)

    def apply(vid: int, resource: Resource, amount: float, minute: int) -> None:
        key = (vid, resource)
        cap = cap_for(vid, resource)
        current = level.get(key, 0.0)
        updated = current + amount
        if cap is not None and updated > cap:
            wasted[key] = wasted.get(key, 0.0) + (updated - cap)
            first_full.setdefault(key, minute)
            updated = cap
        level[key] = max(0.0, updated)

    for minute in range(0, MINUTES_PER_DAY, step_minutes):
        # Production for this slice, then whatever landed inside it.
        for vid, per_resource in net_per_hour.items():
            for resource, rate in per_resource.items():
                if rate:
                    apply(vid, resource, rate * step_minutes / 60.0, minute)
        for event_minute in range(minute, minute + step_minutes):
            for vid, resource, amount in moves.get(event_minute, ()):
                apply(vid, resource, amount, event_minute)

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
                if amount > 0
            ),
            key=lambda event: (-event.wasted_per_day, event.village_id, event.resource.value),
        )
    )


def storage_warnings(
    statuses: Sequence[StoreStatus],
    overflows: Sequence[OverflowEvent],
    warn_hours: float = DEFAULT_WARN_HOURS,
) -> tuple[str, ...]:
    """Operator-facing lines. Starvation first: it destroys troops, not surplus."""
    warnings: list[str] = []
    for status in sorted(
        (s for s in statuses if s.trend is Trend.DRAINING and s.hours_remaining is not None),
        key=lambda s: s.hours_remaining or 0.0,
    ):
        if (status.hours_remaining or 0.0) < warn_hours:
            warnings.append(
                f"village {status.village_id}: {status.resource.value} runs out in "
                f"{status.hours_remaining:.1f}h at {status.net_per_hour:+.0f}/h "
                f"({status.stock:,} in store)"
            )
    for status in sorted(
        (s for s in statuses if s.trend is Trend.FILLING and s.hours_remaining is not None),
        key=lambda s: s.hours_remaining or 0.0,
    ):
        if (status.hours_remaining or 0.0) < warn_hours:
            warnings.append(
                f"village {status.village_id}: {status.resource.value} fills its store in "
                f"{status.hours_remaining:.1f}h at {status.net_per_hour:+.0f}/h; "
                f"anything past the cap is lost"
            )
    for event in overflows:
        warnings.append(
            f"village {event.village_id}: {event.resource.value} hits the cap at "
            f"{event.minute // 60:02d}:{event.minute % 60:02d} and loses about "
            f"{event.wasted_per_day:,.0f}/day — an arriving batch overflows even "
            f"though the average rate fits"
        )
    return tuple(warnings)
