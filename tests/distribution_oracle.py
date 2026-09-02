"""A deliberately dumb, slow, obviously-correct storage simulator.

Written from the *specification* -- add production, apply arrivals, apply
departures, clamp at the cap, accumulate what the clamp threw away -- and not
from the production code. Its only job is to disagree with
:func:`~travian_api.services.distribution.storage.simulate_day` and
:func:`~travian_api.services.distribution.storage.simulate_profile_cycle` when
one of them is wrong.

Minute granularity, no settling shortcut, no memoisation, no flattened keys.
The production functions are called with ``step_minutes=1`` so both tick grids
are the same 1,440 minutes and the comparison is exact rather than approximate.

The one concession to speed is bucketing each firing under the minute it
happens instead of rescanning every firing at every minute -- a dict lookup for
a linear search, which changes nothing about what is computed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from travian_api.services.distribution.allocation import Resource

MINUTES_PER_DAY = 1440


def _firings(routes, gate=None) -> list[tuple[int, int, int, int, Resource, float]]:
    """(dispatch_minute, arrival_minute, origin, destination, resource, batch).

    ``gate`` filters on the dispatch minute, which is how a profile that only
    runs part of the day drops the firings outside its own hours.
    """
    out = []
    for scheduled in routes:
        for out_minute, in_minute in zip(
            scheduled.dispatch_minutes, scheduled.arrival_minutes, strict=True
        ):
            if gate is not None and not gate(out_minute):
                continue
            for resource, amount in scheduled.route.batch_per_resource.items():
                if amount > 0:
                    out.append(
                        (
                            out_minute,
                            in_minute,
                            scheduled.route.origin,
                            scheduled.route.destination,
                            resource,
                            amount,
                        )
                    )
    return out


def _by_minute(firings, position: int) -> dict[int, list[int]]:
    buckets: dict[int, list[int]] = {}
    for index, firing in enumerate(firings):
        buckets.setdefault(firing[position], []).append(index)
    return buckets


def oracle_day(
    beat,
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    net_per_hour: Mapping[int, Mapping[Resource, float]],
    days: int = 14,
) -> dict[tuple[int, Resource], dict[str, float]]:
    """Replay ``days`` days minute by minute and report the LAST day.

    Once the day repeats, every later day is identical, so the last day is the
    settled day whenever the beat settles at all -- the same figure
    ``simulate_day`` reports, reached without its convergence bookkeeping.

    When the last day did NOT repeat the one before it, its clamping is not the
    recurring loss. A capped store still gaining will sit at its cap and shed
    its whole daily gain from then on, so any such store whose last day showed
    less than that is reported at the gain -- the contract's projection rule,
    reimplemented here from the specification.

    Order within a minute matches the documented contract: production for the
    minute, then arrivals, then departures (cargo landing now is available to a
    route leaving now).
    """
    firings = _firings(beat.routes)
    outbound = _by_minute(firings, 0)
    inbound = _by_minute(firings, 1)
    rates = [
        ((vid, resource), rate / 60.0)
        for vid, per in net_per_hour.items()
        for resource, rate in per.items()
        if rate
    ]
    level = {(vid, r): float(amount) for vid, per in stocks.items() for r, amount in per.items()}
    # Cargo in the air at midnight is still in the air, carrying whatever
    # yesterday's dispatch could fund. So this outlives the day loop: a firing
    # whose arrival minute precedes its dispatch minute lands *before* today's
    # dispatch, and what lands is yesterday's load, not a fresh nominal batch.
    in_flight: dict[int, float] = {}
    result: dict[tuple[int, Resource], dict[str, float]] = {}
    previous_close: dict[tuple[int, Resource], float] | None = None
    settled = False
    for _day in range(days):
        wasted: dict[tuple[int, Resource], float] = {}
        first_full: dict[tuple[int, Resource], int] = {}
        moved: dict[tuple[int, Resource], float] = {}

        for minute in range(MINUTES_PER_DAY):
            for key, per_minute in rates:
                cap = capacities.get(key[0], {}).get(key[1])
                new = level.get(key, 0.0) + per_minute
                if cap is not None and new > cap:
                    wasted[key] = wasted.get(key, 0.0) + (new - cap)
                    first_full.setdefault(key, minute)
                    new = float(cap)
                level[key] = max(0.0, new)
            for index in inbound.get(minute, ()):
                firing = firings[index]
                key = (firing[3], firing[4])
                landed = in_flight.get(index, firing[5])
                cap = capacities.get(key[0], {}).get(key[1])
                new = level.get(key, 0.0) + landed
                if cap is not None and new > cap:
                    wasted[key] = wasted.get(key, 0.0) + (new - cap)
                    first_full.setdefault(key, minute)
                    new = float(cap)
                level[key] = max(0.0, new)
                moved[key] = moved.get(key, 0.0) + landed
            for index in outbound.get(minute, ()):
                firing = firings[index]
                key = (firing[2], firing[4])
                shipped = min(firing[5], max(0.0, level.get(key, 0.0)))
                in_flight[index] = shipped
                level[key] = level.get(key, 0.0) - shipped
                moved[key] = moved.get(key, 0.0) - shipped

        settled = previous_close is not None and all(
            abs(level.get(key, 0.0) - previous_close.get(key, 0.0)) < 1e-6
            for key in set(level) | set(previous_close)
        )
        previous_close = dict(level)
        result = {
            key: {
                "wasted": amount,
                "first_full": float(first_full[key]),
                "net_gain": net_per_hour.get(key[0], {}).get(key[1], 0.0) * 24.0
                + moved.get(key, 0.0),
            }
            for key, amount in wasted.items()
        }
    if not settled:
        for vid, per in capacities.items():
            for resource, cap in per.items():
                key = (vid, resource)
                gain = net_per_hour.get(vid, {}).get(resource, 0.0) * 24.0 + moved.get(key, 0.0)
                if gain <= 1.0 or gain - wasted.get(key, 0.0) < 1.0:
                    continue
                # Projected only within a month of the cap, the contract's other
                # half: past that the account has changed and the store says
                # nothing about this sheet. Spelled out with its own literal
                # rather than importing the planner's, so the two
                # implementations stay genuinely independent.
                if days + (cap - level.get(key, 0.0)) / gain > 30:
                    continue
                result[key] = {
                    "wasted": gain,
                    "first_full": float(first_full.get(key, 0)),
                    "net_gain": gain,
                }
    return result


def oracle_profile_cycle(
    segments: Sequence,
    own_rates: Mapping[int, Mapping[Resource, float]],
    stocks: Mapping[int, Mapping[Resource, int]],
    capacities: Mapping[int, Mapping[Resource, int]],
    ceilings: Mapping[int, float] | None = None,
    max_days: int = 45,
):
    """The same replay, for a day that switches between allocation profiles.

    Returns ``(rows, breaches, settled)``; rows maps ``(village_id, resource)``
    to ``{"low", "high", "daily_net"}`` for the measured day, and breaches is a
    list of ``(village_id, resource, kind)`` in the order they were hit.

    Order within a minute follows the documented contract, which differs from
    :func:`oracle_day`: departures, then arrivals, then production for the
    minute.
    """
    ceilings = dict(ceilings or {})
    simulated = set(stocks) | set(own_rates)

    firings: list[tuple[int, int, int, int, Resource, float]] = []
    firing_segment: list[object] = []
    for segment in segments:
        found = _firings(segment.routes, gate=segment.covers)
        firings.extend(found)
        firing_segment.extend([segment] * len(found))
    outbound = _by_minute(firings, 0)
    inbound = _by_minute(firings, 1)

    rates = [
        ((vid, resource), own / 60.0, own < 0)
        for vid, per in own_rates.items()
        for resource, own in per.items()
        if own
    ]
    # Which profile owns each minute, and what hand-shipped rate it runs.
    owner: list[object] = []
    manual_at: list[list[tuple[tuple[int, Resource], float]]] = []
    for minute in range(MINUTES_PER_DAY):
        active = next((s for s in segments if s.covers(minute)), None)
        owner.append(active)
        entries: list[tuple[tuple[int, Resource], float]] = []
        if active is not None:
            for vid, per in active.manual_rates.items():
                for resource, rate in per.items():
                    if rate:
                        entries.append(((vid, resource), rate / 60.0))
        manual_at.append(entries)

    level = {(vid, r): float(amount) for vid, per in stocks.items() for r, amount in per.items()}
    breaches: list[tuple[int, Resource, str]] = []
    seen: set[tuple[tuple[int, Resource], str]] = set()
    for vid, ceiling in ceilings.items():
        key = (vid, Resource.CROP)
        if key in level and level[key] > ceiling:
            breaches.append((vid, Resource.CROP, "above"))

    lows: dict[tuple[int, Resource], float] = {}
    highs: dict[tuple[int, Resource], float] = {}
    nets: dict[tuple[int, Resource], float] = {}
    settled_day = -1
    state = {"measuring": False}

    def put(key, amount, draining):
        previous = level.get(key, 0.0)
        new = previous + amount
        if state["measuring"]:
            nets[key] = nets.get(key, 0.0) + amount
        cap = capacities.get(key[0], {}).get(key[1])
        if cap is not None and new > cap:
            if (key, "capacity") not in seen:
                seen.add((key, "capacity"))
                breaches.append((key[0], key[1], "capacity"))
            new = float(cap)
        ceiling = ceilings.get(key[0]) if key[1] is Resource.CROP else None
        if (
            ceiling is not None
            and previous - 1e-6 <= ceiling < new - 1e-6
            and (key, "ceiling") not in seen
        ):
            seen.add((key, "ceiling"))
            breaches.append((key[0], key[1], "ceiling"))
        if new <= 0.0:
            if draining and (key, "empty") not in seen:
                seen.add((key, "empty"))
                breaches.append((key[0], key[1], "empty"))
            new = 0.0
        level[key] = new

    # Outlives the day loop: cargo in the air at midnight carries yesterday's
    # load, so a firing that lands before its own dispatch delivers that.
    in_flight: dict[int, float] = {}
    for day in range(max_days):
        opening = dict(level)
        state["measuring"] = settled_day >= 0 or day == max_days - 1
        if state["measuring"]:
            lows = dict(level)
            highs = dict(level)
            nets = {}
        for minute in range(MINUTES_PER_DAY):
            for index in outbound.get(minute, ()):
                firing = firings[index]
                key = (firing[2], firing[4])
                shipped = min(firing[5], max(0.0, level.get(key, 0.0)))
                in_flight[index] = shipped
                if shipped:
                    put(key, -shipped, True)
            for index in inbound.get(minute, ()):
                firing = firings[index]
                if firing[3] not in simulated:
                    continue
                shipped = in_flight.get(index)
                if shipped:
                    put((firing[3], firing[4]), shipped, False)
            for key, per_minute, draining in rates:
                put(key, per_minute, draining)
            for key, per_minute in manual_at[minute]:
                put(key, per_minute, per_minute < 0)
            if state["measuring"]:
                for key, value in level.items():
                    if value < lows.get(key, value):
                        lows[key] = value
                    if value > highs.get(key, value):
                        highs[key] = value
        if settled_day >= 0:
            break
        if all(abs(level[key] - opening.get(key, 0.0)) < 1.0 for key in level):
            settled_day = day

    rows = {
        key: {
            "low": lows.get(key, level.get(key, 0.0)),
            "high": highs.get(key, level.get(key, 0.0)),
            "daily_net": nets.get(key, 0.0),
        }
        for key in sorted(level)
    }
    return rows, breaches, settled_day >= 0
