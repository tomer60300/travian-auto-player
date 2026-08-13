"""Merchant capacity and the cost of running a trade route.

Two review findings from ``docs/25-resource-distribution-planner.md`` shape this
module:

**R1 — the capacity constants are disputed.** The profiling doc used
``base 2200`` with ``+20%`` per Trade Office level; published Teuton values are
``base 1000`` and ``+10%`` (``+20%`` is the Roman rate). Both errors overstate
capacity, which under-provisions merchants -- the unsafe direction. Rather than
pick a side, capacity lives in one injectable :class:`MerchantModel` that can be
*derived from observation* via :func:`calibrate`. Nothing else in the planner is
allowed to hardcode a capacity.

**R5 — cycles should divide 24 hours.** Otherwise the schedule has no repeating
daily period and cannot be written down as a beat. :data:`DAILY_BEAT_CYCLES` is
the default; :data:`ALL_CYCLES` remains available so the optimizer can quantify
what the restriction costs instead of hiding it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Cycle lengths that divide a day, so the schedule repeats every 24h (R5).
DAILY_BEAT_CYCLES: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 24)

# Every cycle Gold Club accepts. Use to measure what DAILY_BEAT_CYCLES costs.
ALL_CYCLES: tuple[int, ...] = tuple(range(1, 25))


@dataclass(frozen=True)
class MerchantModel:
    """How much one merchant carries, and how fast it travels.

    ``capacity = base_capacity * (1 + bonus_per_trade_office_level * level)``

    Warning:
        The stock constants below are **published values, unverified for any
        particular server**. Trade artifacts multiply merchant capacity and can
        be captured or lost, and server speed scales it. Prefer
        :func:`calibrate` on real observations over trusting a default.
    """

    base_capacity: int
    bonus_per_trade_office_level: float
    speed_fields_per_hour: float

    def __post_init__(self) -> None:
        if self.base_capacity <= 0:
            raise ValueError(f"base_capacity must be positive, got {self.base_capacity}")
        if self.bonus_per_trade_office_level < 0:
            raise ValueError("bonus_per_trade_office_level cannot be negative")

    def capacity(self, trade_office_level: int = 0) -> int:
        """Carrying capacity of one merchant, rounded **down**.

        Flooring is deliberate. Understating capacity over-provisions merchants,
        which wastes some; overstating it under-provisions, which silently
        breaches the village's merchant budget. Only the first is recoverable.
        """
        if trade_office_level < 0:
            raise ValueError(f"trade office level cannot be negative, got {trade_office_level}")
        scaled = self.base_capacity * (1 + self.bonus_per_trade_office_level * trade_office_level)
        return math.floor(scaled)


# Published stock values at 1x. UNVERIFIED for this account -- see R1.
STOCK_TEUTON = MerchantModel(
    base_capacity=1000, bonus_per_trade_office_level=0.10, speed_fields_per_hour=12.0
)
STOCK_ROMAN = MerchantModel(
    base_capacity=500, bonus_per_trade_office_level=0.20, speed_fields_per_hour=16.0
)
STOCK_GAUL = MerchantModel(
    base_capacity=750, bonus_per_trade_office_level=0.10, speed_fields_per_hour=24.0
)


@dataclass(frozen=True)
class CapacityObservation:
    """A merchant capacity read straight off a village's marketplace page."""

    trade_office_level: int
    capacity: int


class CalibrationError(ValueError):
    """Observations cannot be explained by one base-and-bonus model."""


def calibrate(
    observations: Sequence[CapacityObservation],
    speed_fields_per_hour: float,
    *,
    tolerance: float = 1.0,
) -> MerchantModel:
    """Derive :class:`MerchantModel` from marketplace readings. Resolves R1.

    Two observations at *different* Trade Office levels determine both unknowns
    in ``capacity = base * (1 + k * level)``:

        k    = (c_a - c_b) / (c_b * a - c_a * b)
        base = c_a / (1 + k * a)

    Any further observations are checked against the solution rather than
    averaged into it. A mismatch is raised, not smoothed away: it means capacity
    is not a single account-wide function of Trade Office level -- most likely a
    Trade artifact affecting some villages and not others -- and silently fitting
    a line through that would reintroduce exactly the error R1 describes.

    Args:
        observations: at least two readings spanning two Trade Office levels.
        speed_fields_per_hour: merchant speed for the tribe.
        tolerance: allowed absolute deviation, in resources, before raising.

    Raises:
        CalibrationError: fewer than two distinct levels, or inconsistent data.
    """
    if len(observations) < 2:
        raise CalibrationError("need at least two observations to solve for base and bonus")

    levels = {o.trade_office_level for o in observations}
    if len(levels) < 2:
        raise CalibrationError(
            f"observations must span two different Trade Office levels, all are {levels.pop()}"
        )

    low = min(observations, key=lambda o: o.trade_office_level)
    high = max(observations, key=lambda o: o.trade_office_level)

    a, c_a = low.trade_office_level, low.capacity
    b, c_b = high.trade_office_level, high.capacity

    denominator = c_b * a - c_a * b
    if denominator == 0:
        raise CalibrationError("degenerate observations: cannot solve for the bonus")

    bonus = (c_a - c_b) / denominator
    base = c_a / (1 + bonus * a)

    if base <= 0 or bonus < 0:
        raise CalibrationError(
            f"observations imply an impossible model (base={base:.1f}, bonus={bonus:.4f})"
        )

    model = MerchantModel(
        base_capacity=round(base),
        bonus_per_trade_office_level=bonus,
        speed_fields_per_hour=speed_fields_per_hour,
    )

    for observation in observations:
        predicted = model.capacity(observation.trade_office_level)
        if abs(predicted - observation.capacity) > tolerance:
            raise CalibrationError(
                f"observation TO={observation.trade_office_level} capacity="
                f"{observation.capacity} does not fit the model derived from the "
                f"others (predicted {predicted}). Capacity may vary per village "
                f"-- a Trade artifact would do this."
            )
    return model


@dataclass(frozen=True)
class RouteCost:
    """What one trade route costs in merchants, at a given cycle length."""

    cycle_hours: int
    batch: int
    merchants_per_send: int
    sets_in_flight: int

    @property
    def merchants_committed(self) -> int:
        """Merchants this route occupies permanently at the sender."""
        return self.merchants_per_send * self.sets_in_flight


def route_cost(
    hourly_cargo: float,
    cycle_hours: int,
    round_trip_minutes: float,
    merchant_capacity: int,
) -> RouteCost:
    """Merchants committed by one route at one cycle length.

    A merchant is busy for the whole round trip, so if the trip outlasts the
    cycle several sets are in flight at once and each needs its own merchants::

        batch  = hourly_cargo * cycle_hours
        send   = ceil(batch / merchant_capacity)
        sets   = ceil(round_trip_minutes / (cycle_hours * 60))
        total  = send * sets

    Both ceilings mean ``total`` is **not monotonic** in ``cycle_hours``: a
    longer cycle needs more merchants per send but fewer sets, and which wins
    flips back and forth. Never reason about a direction -- use
    :func:`cheapest_cycle`.
    """
    if cycle_hours <= 0:
        raise ValueError(f"cycle_hours must be positive, got {cycle_hours}")
    if merchant_capacity <= 0:
        raise ValueError(f"merchant_capacity must be positive, got {merchant_capacity}")
    if hourly_cargo < 0:
        raise ValueError(f"hourly_cargo cannot be negative, got {hourly_cargo}")

    batch = math.ceil(hourly_cargo * cycle_hours)
    return RouteCost(
        cycle_hours=cycle_hours,
        batch=batch,
        merchants_per_send=math.ceil(batch / merchant_capacity),
        sets_in_flight=math.ceil(round_trip_minutes / (cycle_hours * 60)),
    )


def cycle_sweep(
    hourly_cargo: float,
    round_trip_minutes: float,
    merchant_capacity: int,
    cycles: Sequence[int] = DAILY_BEAT_CYCLES,
) -> list[RouteCost]:
    """Cost at every candidate cycle, in the order given.

    The UI shows this curve rather than only the winner: the operator planned
    these routes by hand and will not trust a cycle choice they cannot see the
    reasoning for.
    """
    return [
        route_cost(hourly_cargo, cycle, round_trip_minutes, merchant_capacity) for cycle in cycles
    ]


def cheapest_cycle(
    hourly_cargo: float,
    round_trip_minutes: float,
    merchant_capacity: int,
    cycles: Sequence[int] = DAILY_BEAT_CYCLES,
) -> RouteCost:
    """Cycle length committing the fewest merchants; shortest cycle wins ties.

    Tie-breaking on the shorter cycle is deliberate: for equal merchant cost it
    delivers sooner, which is objective 2 (latency) in the optimizer.
    """
    if not cycles:
        raise ValueError("cycles must not be empty")
    return min(
        cycle_sweep(hourly_cargo, round_trip_minutes, merchant_capacity, cycles),
        key=lambda cost: (cost.merchants_committed, cost.cycle_hours),
    )
