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

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .allocation import Resource, village_label
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


def _staleness(dispatches: Sequence[int], inbound_arrivals: Sequence[int]) -> int:
    """How long the worst dispatch waits after the freshest inbound cargo.

    Both schedules repeat daily, so "after" is measured modulo the day. For each
    firing, the wait is the time since the most recent inbound arrival; the
    figure returned is the worst of those, which is what the scheduler minimises
    so a relay hub forwards cargo it has just collected rather than cargo it will
    only receive later in the cycle.

    Zero when there is no inbound to wait for, so ordinary routes are unaffected.
    """
    if not inbound_arrivals:
        return 0
    return max(
        min((dispatch - arrival) % MINUTES_PER_DAY for arrival in inbound_arrivals)
        for dispatch in dispatches
    )


def _worst_gap(candidate_arrivals: Sequence[int], taken_sorted: Sequence[int]) -> int:
    """Tightest spacing this candidate would create against already-placed ones.

    ``taken_sorted`` must be sorted ascending. On a circle the nearest placed
    arrival to a candidate is always one of the two entries bracketing it, so a
    bisect finds it in log time instead of scanning every pair. That matters
    because this runs once per candidate dispatch minute -- up to 1,440 times
    per route -- against every arrival already claimed at the destination, and a
    real account funnels its remainder into one capital hub: the frozen
    23-village fixture puts 292 arrivals a day on a single node, which cost more
    than a generated 50-village account with the load spread out.
    """
    if not taken_sorted:
        return MINUTES_PER_DAY
    count = len(taken_sorted)
    worst = MINUTES_PER_DAY
    for arrival in candidate_arrivals:
        index = bisect_left(taken_sorted, arrival)
        # The entry at or after `arrival`, and the one before it -- both wrapping
        # past midnight, which is why _circular_gap does the distance.
        for neighbour in (taken_sorted[index % count], taken_sorted[index - 1]):
            gap = _circular_gap(arrival, neighbour)
            if gap < worst:
                worst = gap
                if worst == 0:
                    return 0
    return worst


def build_beat(
    routes: Sequence[Route],
    *,
    min_arrival_gap_minutes: int = DEFAULT_MIN_ARRIVAL_GAP_MINUTES,
    reserved_window: tuple[int, int] | None = None,
    step_minutes: int = 1,
    names: Mapping[int, str] | None = None,
    dispatch_window: tuple[int, int] | None = None,
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
        dispatch_window: ``(start_minute, end_minute)``, possibly wrapping past
            midnight, of the allocation profile these routes belong to. Sends
            are phased into it: a profile only runs part of the day, and a
            firing outside its hours does not happen at all (see
            :func:`~.storage.simulate_profile_cycle`). Left None the whole day
            is open, which is what a single round-the-clock route set wants.

    Returns:
        A :class:`Beat`. Every route is always scheduled -- a route that cannot
        be spaced politely still gets a slot, with a warning naming it.
    """
    if min_arrival_gap_minutes < 0:
        raise ValueError("min_arrival_gap_minutes cannot be negative")
    if step_minutes < 1:
        raise ValueError("step_minutes must be at least 1")
    if dispatch_window is not None and _window_length(dispatch_window) == 0:
        raise ValueError("dispatch_window is zero-width: no minute of the day is inside it")

    scheduled: list[ScheduledRoute] = []
    warnings: list[str] = []
    # Arrivals already claimed, per destination.
    claimed: dict[int, list[int]] = {}
    # Crop arrivals only, per village: what a relay hub is waiting to forward.
    crop_arrivals: dict[int, list[int]] = {}

    # A village that both receives and sends crop is forwarding it -- the crop
    # relay the optimizer builds to lift load off villages that cannot staff
    # their own haul. Its outbound must be scheduled after its inbound lands, or
    # the route ships from the hub's own granary and the relay only refills it.
    crop_senders = {r.origin for r in routes if Resource.CROP in r.cargo_per_hour}
    crop_receivers = {r.destination for r in routes if Resource.CROP in r.cargo_per_hour}
    relay_hubs = crop_senders & crop_receivers

    # Deterministic order: the busiest destinations are placed first, while they
    # still have the whole day to spread across.
    inbound_count: dict[int, int] = {}
    for route in routes:
        inbound_count[route.destination] = inbound_count.get(route.destination, 0) + 1

    def placement_key(route: Route) -> tuple[int, int, int, int]:
        return (
            -inbound_count[route.destination],
            route.destination,
            route.origin,
            route.cycle_hours,
        )

    # A crop route out of a hub must be placed after the routes feeding that hub,
    # or its inbound arrivals are not on the clock yet and collect-then-ship
    # quietly does nothing. Merely sorting hub-outbound last is not enough: in a
    # chain A -> B -> C -> D every leg is hub-outbound, and the tie-break decides
    # their relative order by destination id, which has nothing to do with the
    # direction cargo flows. So this is a real topological pass over the crop
    # graph, with the ordinary key breaking ties inside each layer.
    crop_routes = [r for r in routes if Resource.CROP in r.cargo_per_hour]
    pending = {id(r): r for r in crop_routes}
    feeders: dict[int, list[Route]] = {}
    for route in crop_routes:
        feeders.setdefault(route.destination, []).append(route)

    layered: list[Route] = []
    placed_ids: set[int] = set()
    while pending:
        ready = [
            route
            for route in pending.values()
            if all(id(feed) in placed_ids for feed in feeders.get(route.origin, ()))
        ]
        if not ready:
            # A cycle in the crop graph (a two-way pair, or a longer loop). No
            # ordering can satisfy every leg, so fall back to the ordinary key
            # for the remainder and let the staleness warning report the cost
            # rather than looping here forever.
            ready = sorted(pending.values(), key=placement_key)[:1]
        for route in sorted(ready, key=placement_key):
            layered.append(route)
            placed_ids.add(id(route))
            pending.pop(id(route), None)

    non_crop = sorted(
        (r for r in routes if Resource.CROP not in r.cargo_per_hour), key=placement_key
    )
    ordered = non_crop + layered

    for route in ordered:
        cycle_minutes = route.cycle_hours * 60
        taken = claimed.setdefault(route.destination, [])
        forwards_crop = route.origin in relay_hubs and Resource.CROP in route.cargo_per_hour
        inbound = crop_arrivals.get(route.origin, []) if forwards_crop else []

        # Where the phase search starts, and how far it runs. Firing patterns
        # repeat every cycle, so one cycle from midnight covers all of them.
        # Inside a profile the search starts at the profile's first minute and
        # never runs past its last, so the route's own "Send at" time is an hour
        # the operator is running it. Nothing worth having is cut off: a window
        # shorter than a cycle can hold one firing at most anyway.
        base = 0 if dispatch_window is None else dispatch_window[0]
        span = (
            cycle_minutes
            if dispatch_window is None
            else min(cycle_minutes, _window_length(dispatch_window))
        )

        best_minute = base
        best_score: tuple[int, int, int, int, int] | None = None
        # Sorted once per route rather than inside the offset sweep below.
        taken_sorted = sorted(taken)
        for offset in range(0, span, step_minutes):
            candidate = ScheduledRoute(
                route=route, dispatch_minute=(base + offset) % MINUTES_PER_DAY
            )
            arrivals = candidate.arrival_minutes
            gap = _worst_gap(arrivals, taken_sorted)
            clear = (
                0
                if reserved_window is None
                else sum(1 for minute in arrivals if _in_window(minute, reserved_window))
            )
            # Collect-then-ship: how long the stalest dispatch waits after the
            # freshest crop landed here. Zero for everything that is not a relay
            # hub, which leaves non-relay scheduling byte-identical.
            stale = _staleness(candidate.dispatch_minutes, inbound)
            # Sends that actually happen. A firing outside the profile's hours
            # is not dispatched, so a phase with none inside it ships nothing at
            # all -- which no amount of tidy arrival spacing makes up for, hence
            # first in the ranking. Constant zero without a window, leaving the
            # round-the-clock ordering below exactly as it was.
            sends = (
                0
                if dispatch_window is None
                else sum(
                    1
                    for minute in candidate.dispatch_minutes
                    if _in_window(minute, dispatch_window)
                )
            )
            # Order of preference: send at all, then clear the reserved window,
            # then MEET the arrival-gap target, then ship soon after collecting,
            # then widen the spacing further.
            #
            # The gap term saturates at the target deliberately. Ranking raw
            # staleness above raw gap made the target unenforceable for relay
            # hubs: staleness takes a different value at nearly every candidate
            # minute, so it decided every comparison and the gap was never
            # consulted -- measured as arrivals colliding at 0 minutes apart
            # where the previous scheduler kept them 15 apart. Saturating means
            # a few minutes of extra staleness can never buy a collision, while
            # beyond the target staleness is still free to choose.
            score = (sends, -clear, min(gap, min_arrival_gap_minutes), -stale, gap)
            if best_score is None or score > best_score:
                best_score, best_minute = score, candidate.dispatch_minute

        placement = ScheduledRoute(route=route, dispatch_minute=best_minute)
        scheduled.append(placement)
        taken.extend(placement.arrival_minutes)
        if Resource.CROP in route.cargo_per_hour:
            crop_arrivals.setdefault(route.destination, []).extend(placement.arrival_minutes)

        # A cycle shorter than the gap target violates the constraint all by
        # itself — no dispatch offset can space a route's own repeats.
        if cycle_minutes < min_arrival_gap_minutes:
            warnings.append(
                f"route {village_label(route.origin, names)} -> "
                f"{village_label(route.destination, names)} fires every "
                f"{cycle_minutes} min, closer than the {min_arrival_gap_minutes} "
                f"min arrival-gap target; its own arrivals cannot be spaced by "
                f"choosing a dispatch offset"
            )

        # A cycle the profile's hours cannot contain fires at most once inside
        # them however it is phased, so the route moves one cycle of cargo a day
        # where its rate was sized for a day of it. The daily-cycle guard is the
        # boundary case: an all-day 00:00-23:59 window is a minute short of a
        # 24h cycle, and that minute costs nothing -- the route still sends the
        # once a day it was planned to.
        if dispatch_window is not None:
            window_minutes = _window_length(dispatch_window)
            if window_minutes < cycle_minutes < MINUTES_PER_DAY:
                warnings.append(
                    f"route {village_label(route.origin, names)} -> "
                    f"{village_label(route.destination, names)} repeats every "
                    f"{route.cycle_hours}h but its profile runs only {window_minutes} min, "
                    f"so it sends once a day instead of "
                    f"{MINUTES_PER_DAY // cycle_minutes} times and cannot deliver its "
                    f"planned {route.hourly_total:,.0f}/h; shorten the cycle or widen "
                    f"the profile"
                )

        # score is (sends, -clear, saturated_gap, -stale, gap); spacing is last.
        achieved = best_score[4] if best_score else MINUTES_PER_DAY
        if achieved < min_arrival_gap_minutes:
            warnings.append(
                f"route {village_label(route.origin, names)} -> "
                f"{village_label(route.destination, names)} lands within "
                f"{achieved} min of another arrival there, against a "
                f"{min_arrival_gap_minutes} min target; {village_label(route.destination, names)} "
                f"has {inbound_count[route.destination]} inbound routes and may be "
                f"too busy to space them"
            )
        if reserved_window is not None and best_score and best_score[1] < 0:
            warnings.append(
                f"route {village_label(route.origin, names)} -> "
                f"{village_label(route.destination, names)} unavoidably lands in "
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


def _window_length(window: tuple[int, int]) -> int:
    """Minutes a possibly midnight-wrapping window spans."""
    start, end = window
    return (end - start) % MINUTES_PER_DAY
