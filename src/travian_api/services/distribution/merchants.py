"""Merchant capacity and the cost of running a trade route.

Two review findings from ``docs/25-resource-distribution-planner.md`` shape this
module:

**R1 — RESOLVED, and the review was wrong.** The review argued the profile's
``base 2200`` / ``+20%`` per Trade Office level had to be mistaken, because
published Teuton values are ``base 1000`` / ``+10%``. A live reading settled it:
a TO 13 village carries **7,920** per merchant, which is exactly
``2200 * (1 + 0.2 * 13)``. Stock Teuton would have given 2,300. The profile was
right and this server is not stock.

The seam stays regardless. Capacity lives in one injectable
:class:`MerchantModel` and can be *derived from observation* via
:func:`calibrate`, because the measured model is still only pinned by a single
data point -- any ``base * (1 + 13k) = 7920`` fits it, and a Trade artifact can
change it mid-server. Nothing else in the planner may hardcode a capacity.

**R5 — cycles should divide 24 hours.** Otherwise the schedule has no repeating
daily period and cannot be written down as a beat. :data:`DAILY_BEAT_CYCLES` is
the default; :data:`ALL_CYCLES` remains available so the optimizer can quantify
what the restriction costs instead of hiding it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Slack subtracted before any ceil of a cargo batch, so float dust just above
# an integer does not round a boundary-exact batch up to a phantom unit.
CEIL_DUST_TOLERANCE = 1e-6

# Cycle lengths that divide a day, so the schedule repeats every 24h (R5).
DAILY_BEAT_CYCLES: tuple[int, ...] = (1, 2, 3, 4, 6, 8, 12, 24)

# Every cycle Gold Club accepts. Use to measure what DAILY_BEAT_CYCLES costs.
ALL_CYCLES: tuple[int, ...] = tuple(range(1, 25))

# Minimum gap between the two Trade Office levels used to calibrate. The game
# reports a floored capacity, so with adjacent levels that rounding is a large
# share of the difference between the readings and the solve is ill-conditioned.
# Measured over 570 synthetic models, adjacent levels mis-predict capacity at
# TO 20 by up to 19 and *overstate* it in 8% of cases -- the unsafe direction.
# A gap of 3 cuts that to 6; a Trade-Office-free sample removes it entirely.
MIN_CALIBRATION_SEPARATION = 3


@dataclass(frozen=True)
class MerchantModel:
    """How much one merchant carries.

    ``capacity = base_capacity * (1 + bonus_per_trade_office_level * level)``

    Merchant *speed* deliberately lives on :class:`~.geometry.MapGeometry`
    instead, which is the only thing that needs it. Holding it in both places
    would let the two disagree.

    Warning:
        Do not assume a published constant applies. Europe 2 does not follow
        stock Teuton values, Trade artifacts multiply capacity and can be
        captured or lost, and server speed scales it. Prefer :func:`calibrate`.
    """

    base_capacity: int
    bonus_per_trade_office_level: float

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


# Measured on Europe 2 (Teuton): a TO 13 village carries 7,920, and
# 2200 * (1 + 0.2 * 13) == 7920 exactly. Still only one data point -- other
# (base, bonus) pairs also satisfy it -- so re-derive with calibrate() when a
# village at a different Trade Office level is to hand.
EUROPE2_TEUTON = MerchantModel(base_capacity=2200, bonus_per_trade_office_level=0.20)

# Published stock Teuton values at 1x, kept only as the counter-example: they
# predict 2,300 at TO 13 where the game reports 7,920, which is how we know this
# server is not stock. Roman and Gaul figures are deliberately absent -- the tool
# is single-account, and an unused constant is one more thing to get wrong.
STOCK_TEUTON = MerchantModel(base_capacity=1000, bonus_per_trade_office_level=0.10)


@dataclass(frozen=True)
class CapacityObservation:
    """A merchant capacity read straight off a village's marketplace page."""

    trade_office_level: int
    capacity: int


class CalibrationError(ValueError):
    """Observations cannot be explained by one base-and-bonus model."""


def calibrate(
    observations: Sequence[CapacityObservation],
    *,
    tolerance: float = 1.0,
) -> MerchantModel:
    """Derive :class:`MerchantModel` from marketplace readings. Resolves R1.

    Two observations at *different* Trade Office levels determine both unknowns
    in ``capacity = base * (1 + k * level)``:

        k    = (c_a - c_b) / (c_b * a - c_a * b)
        base = c_a / (1 + k * a)

    **Include a village with no Trade Office if you can.** Its capacity *is* the
    base, so no inversion is needed and the residual error becomes one-sided in
    the safe direction -- verified over 600 synthetic models, where that path
    never once overstated capacity. Without such a sample the levels must be at
    least :data:`MIN_CALIBRATION_SEPARATION` apart, because the game reports a
    floored capacity and close readings make the solve ill-conditioned.

    Any further observations are checked against the solution rather than
    averaged into it. A mismatch is raised, not smoothed away: it means capacity
    is not a single account-wide function of Trade Office level -- most likely a
    Trade artifact affecting some villages and not others -- and silently fitting
    a line through that would reintroduce exactly the error R1 describes.

    Args:
        observations: at least two readings spanning two Trade Office levels.
        tolerance: allowed absolute deviation, in resources, before raising.

    Raises:
        CalibrationError: fewer than two distinct levels, levels too close to
            solve reliably, or observations that no single model explains.
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

    if a == 0:
        # A Trade-Office-free village reports the base directly, so there is no
        # inversion and no ill-conditioning.
        base = float(c_a)
        bonus = (c_b / base - 1) / b
    else:
        if b - a < MIN_CALIBRATION_SEPARATION:
            raise CalibrationError(
                f"Trade Office levels {a} and {b} are only {b - a} apart, which "
                f"cannot be solved reliably: the game floors the capacity it "
                f"reports, so with close levels that rounding dominates the "
                f"difference between the readings and the fit can overstate "
                f"capacity -- the direction that breaches merchant budgets. Use "
                f"levels at least {MIN_CALIBRATION_SEPARATION} apart, or read a "
                f"village with no Trade Office (its capacity is the base)."
            )
        denominator = c_b * a - c_a * b
        if denominator == 0:
            raise CalibrationError("degenerate observations: cannot solve for the bonus")

        bonus = (c_a - c_b) / denominator
        base = c_a / (1 + bonus * a)

    if base <= 0 or bonus < 0:
        raise CalibrationError(
            f"observations imply an impossible model (base={base:.1f}, bonus={bonus:.4f})"
        )

    model = MerchantModel(base_capacity=round(base), bonus_per_trade_office_level=bonus)

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

    # ceil with a dust tolerance. Breakpoint transfers deliberately land flow
    # rates exactly on capacity boundaries, and the arithmetic that produces
    # them (k * capacity / cycle - rate) leaves ~1e-12 of float dust ABOVE the
    # integer. A bare ceil amplifies that dust into a whole extra unit of
    # cargo -- and worse, a whole extra merchant per send. Genuine fractional
    # batches are never this close to an integer (the smallest real fraction is
    # capacity/cycle granularity, orders of magnitude above the tolerance).
    batch = math.ceil(hourly_cargo * cycle_hours - CEIL_DUST_TOLERANCE)
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

    Note:
        There is deliberately no budget parameter. Because this already returns
        the minimum-merchant cycle, that cycle is also the most affordable one:
        if it does not fit a village's spare merchants, no cycle does. Filtering
        by a budget could therefore never change the answer. Feasibility is the
        caller's decision -- compare :attr:`RouteCost.merchants_committed`
        against the budget and escalate per the optimizer's ladder. Known issue
        #6 is that comparison being skipped, and no signature here can make it
        for you.
    """
    if not cycles:
        raise ValueError("cycles must not be empty")

    # Deliberately not `min(cycle_sweep(...))`. This is the hottest function in
    # the planner -- the route search calls it millions of times per plan, and
    # building a RouteCost for every candidate only to discard all but one was
    # measurably the largest single cost in generating a plan. The arithmetic
    # below is exactly route_cost's, kept in step with it, and only the winner
    # is materialised. cycle_sweep still exists for the UI, which wants the
    # whole curve.
    best: tuple[int, int, int, int, int] | None = None  # committed, cycle, batch, send, sets
    for cycle_hours in cycles:
        # Same checks in the same order as route_cost, so an invalid argument
        # still raises the same error rather than a different one.
        if cycle_hours <= 0:
            raise ValueError(f"cycle_hours must be positive, got {cycle_hours}")
        if merchant_capacity <= 0:
            raise ValueError(f"merchant_capacity must be positive, got {merchant_capacity}")
        if hourly_cargo < 0:
            raise ValueError(f"hourly_cargo cannot be negative, got {hourly_cargo}")

        batch = math.ceil(hourly_cargo * cycle_hours - CEIL_DUST_TOLERANCE)
        send = math.ceil(batch / merchant_capacity)
        sets = math.ceil(round_trip_minutes / (cycle_hours * 60))
        candidate = (send * sets, cycle_hours, batch, send, sets)
        if best is None or candidate[:2] < best[:2]:
            best = candidate

    _committed, cycle_hours, batch, send, sets = best
    return RouteCost(
        cycle_hours=cycle_hours,
        batch=batch,
        merchants_per_send=send,
        sets_in_flight=sets,
    )
