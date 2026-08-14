"""Assign each route a slot in a repeating 24-hour beat.

Every cycle length divides 24 hours (see :data:`~.merchants.DAILY_BEAT_CYCLES`),
so the whole schedule repeats daily and can be written down as one table. That
restriction is what makes this module possible: with an arbitrary cycle set the
pattern's period is the lowest common multiple of every cycle, which is not a
schedule anyone can read.

A route with a 3-hour cycle fires eight times a day. Scheduling therefore works
on a 1440-minute timeline and places *every* firing, rather than a minute-of-hour
offset -- a 60-minute table cannot express a multi-hour cycle at all (review R5).

Two things this deliberately does not do:

* **Collect-then-ship ordering at hubs** is moot in the current model. Netting in
  :mod:`.allocation` leaves each village either a sender or a receiver of a given
  resource, never both, so no village relays a resource and there is no inbound
  that an outbound must wait for.
* **Crop relay through a sub-hub**, which profile section 3.5 permits, cannot be
  expressed for the same reason. Supporting it needs multi-leg flows in the
  optimizer, not scheduling changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .optimizer import Route

MINUTES_PER_DAY = 24 * 60
DEFAULT_MIN_ARRIVAL_GAP_MINUTES = 3


@dataclass(frozen=True)
class ScheduledRoute:
    """A route plus the minute of the day its first send leaves."""

    route: Route
    dispatch_minute: int
    """Minutes past midnight for the first send. Repeats every cycle."""

    @property
    def dispatch_minutes(self) -> tuple[int, ...]:
        """Every firing across the day."""
        step = self.route.cycle_hours * 60
        return tuple(
            (self.dispatch_minute + offset) % MINUTES_PER_DAY
            for offset in range(0, MINUTES_PER_DAY, step)
        )

    @property
    def arrival_minutes(self) -> tuple[int, ...]:
        """When each send lands."""
        travel = round(self.route.one_way_minutes)
        return tuple((minute + travel) % MINUTES_PER_DAY for minute in self.dispatch_minutes)

    @property
    def first_arrival_minute(self) -> int:
        return self.arrival_minutes[0]


@dataclass(frozen=True)
class Beat:
    """The daily schedule, plus anything that could not be honoured."""

    routes: tuple[ScheduledRoute, ...] = ()
    warnings: tuple[str, ...] = ()

    def arrivals_at(self, village_id: int) -> tuple[tuple[int, ScheduledRoute], ...]:
        """Every arrival at *village_id* through the day, in time order."""
        arrivals = [
            (minute, scheduled)
            for scheduled in self.routes
            if scheduled.route.destination == village_id
            for minute in scheduled.arrival_minutes
        ]
        return tuple(sorted(arrivals, key=lambda pair: (pair[0], pair[1].route.origin)))


def _circular_gap(a: int, b: int) -> int:
    """Shortest distance between two minutes of the day, either way round."""
    raw = abs(a - b)
    return min(raw, MINUTES_PER_DAY - raw)


def _worst_gap(candidate_arrivals: Sequence[int], taken: Sequence[int]) -> int:
    """Tightest spacing this candidate would create against already-placed ones."""
    if not taken:
        return MINUTES_PER_DAY
    return min(_circular_gap(arrival, other) for arrival in candidate_arrivals for other in taken)


def build_beat(
    routes: Sequence[Route],
    *,
    min_arrival_gap_minutes: int = DEFAULT_MIN_ARRIVAL_GAP_MINUTES,
    reserved_window: tuple[int, int] | None = None,
    step_minutes: int = 1,
) -> Beat:
    """Place every route on the daily beat, spacing arrivals at each destination.

    Arrivals are staggered so a receiving village's stock climbs in steps rather
    than in one lump, which is what keeps a hub from overflowing between its own
    outbound sends.

    Args:
        routes: routes to schedule. Order does not affect the result.
        min_arrival_gap_minutes: desired spacing between arrivals at the same
            village. Honoured where possible; a shortfall warns rather than
            fails, because a village with many inbound routes may not have room.
        reserved_window: ``(start_minute, end_minute)`` to keep clear for the
            manual NPC burst. Arrivals avoid it when an alternative exists.
        step_minutes: granularity of candidate dispatch minutes. 1 tries every
            minute; coarser values trade precision for speed on large accounts.

    Returns:
        A :class:`Beat`. Every route is always scheduled -- a route that cannot
        be spaced politely still gets a slot, with a warning naming it.
    """
    if min_arrival_gap_minutes < 0:
        raise ValueError("min_arrival_gap_minutes cannot be negative")
    if step_minutes < 1:
        raise ValueError("step_minutes must be at least 1")

    scheduled: list[ScheduledRoute] = []
    warnings: list[str] = []
    # Arrivals already claimed, per destination.
    claimed: dict[int, list[int]] = {}

    # Deterministic order: the busiest destinations are placed first, while they
    # still have the whole day to spread across.
    inbound_count: dict[int, int] = {}
    for route in routes:
        inbound_count[route.destination] = inbound_count.get(route.destination, 0) + 1
    ordered = sorted(
        routes,
        key=lambda r: (-inbound_count[r.destination], r.destination, r.origin, r.cycle_hours),
    )

    for route in ordered:
        window = route.cycle_hours * 60
        taken = claimed.setdefault(route.destination, [])

        best_offset = 0
        best_score: tuple[int, int] | None = None
        for offset in range(0, window, step_minutes):
            candidate = ScheduledRoute(route=route, dispatch_minute=offset)
            arrivals = candidate.arrival_minutes
            gap = _worst_gap(arrivals, taken)
            clear = (
                0
                if reserved_window is None
                else sum(1 for minute in arrivals if _in_window(minute, reserved_window))
            )
            # Prefer avoiding the reserved window, then the widest spacing.
            # Every offset is evaluated: stopping at the first one that merely
            # clears the minimum gap would crowd arrivals that had a whole day
            # of room. The full sweep is pure CPU and runs off the event loop.
            score = (-clear, gap)
            if best_score is None or score > best_score:
                best_score, best_offset = score, offset

        placement = ScheduledRoute(route=route, dispatch_minute=best_offset)
        scheduled.append(placement)
        taken.extend(placement.arrival_minutes)

        achieved = best_score[1] if best_score else MINUTES_PER_DAY
        if achieved < min_arrival_gap_minutes:
            warnings.append(
                f"route {route.origin} -> {route.destination} lands within "
                f"{achieved} min of another arrival there, against a "
                f"{min_arrival_gap_minutes} min target; village {route.destination} "
                f"has {inbound_count[route.destination]} inbound routes and may be "
                f"too busy to space them"
            )
        if reserved_window is not None and best_score and best_score[0] < 0:
            warnings.append(
                f"route {route.origin} -> {route.destination} unavoidably lands in "
                f"the reserved window {reserved_window}"
            )

    return Beat(
        routes=tuple(sorted(scheduled, key=lambda s: (s.dispatch_minute, s.route.origin))),
        warnings=tuple(warnings),
    )


def _in_window(minute: int, window: tuple[int, int]) -> bool:
    """Is *minute* inside a possibly midnight-wrapping window?"""
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end
