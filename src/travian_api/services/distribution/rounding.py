"""Sum-preserving integer rounding for cargo amounts.

A send carries whole resources, but the rates that produce it are fractional.
Rounding each resource independently changes the total, which either invents
resources or loses them -- profile section 12 requires that rounding "must not
create or destroy resources".

Largest-remainder (Hare-Niemeyer): floor everything, then hand the leftover
units to the largest fractional parts. The result sums to exactly
``round(sum(values))`` and each entry is within one of its exact value.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping
from typing import TypeVar

K = TypeVar("K", bound=Hashable)


def round_preserving_total(
    values: Mapping[K, float], *, target_total: int | None = None, min_each: int = 0
) -> dict[K, int]:
    """Round *values* to integers whose sum is *target_total*.

    The default target is ``round(sum(values))``. The planner passes
    ``ceil(batch)`` instead: merchants are budgeted for the ceiling in
    ``route_cost``, so rounding the sheet down would under-deliver on every
    cycle while the reserved merchants ride part-empty.

    Ties in the fractional part are broken by the key's sorted order, so the
    result is deterministic -- a re-plan on unchanged input must produce an
    identical setup sheet or the diff against the live routes is noise.

    ``min_each`` floors every NONZERO input at that many units, and then the sum
    may exceed ``target_total`` by up to ``min_each`` per floored key. Off by
    default, because largest-remainder rounding is otherwise exactly right.

    The planner passes ``min_each=1`` for cargo, where the quantities are not
    interchangeable and the rounded result is not a per-send approximation but
    the cargo of every send, forever: preserving only the aggregate let a 0.6
    lumber + 0.4 crop mix ship as one lumber and no crop, so the crop was never
    delivered at all while the lumber was permanently overdrawn. One unit is the
    smallest thing a route can carry, so a sub-unit rate is necessarily
    overshot -- but it is overshot by less than a unit, and it arrives.

    Raises:
        ValueError: if any value is negative. Cargo is never negative, and the
            largest-remainder argument does not carry over to mixed signs.
    """
    if any(value < 0 for value in values.values()):
        raise ValueError(f"cargo amounts cannot be negative: {dict(values)}")
    if not values:
        return {}

    total = round(sum(values.values())) if target_total is None else target_total
    floors = {key: math.floor(value) for key, value in values.items()}
    shortfall = total - sum(floors.values())

    if shortfall < 0:
        # The documented guarantee is that the result sums to `total`, and with
        # a negative shortfall `floors` already exceeds it -- handing back
        # `floors` would quietly ship more than the merchants were budgeted for.
        # No caller can reach this today (target_total is a ceil of a value at
        # or above the floor sum), so it is an invariant violation, not a case
        # to absorb.
        raise ValueError(
            f"target_total {total} is below the floor sum {sum(floors.values())}; "
            f"it cannot be met without reducing an amount below its floor"
        )
    if shortfall == 0:
        return floors

    # Hand out the leftover units to the largest fractional parts first.
    ranked = sorted(
        values,
        key=lambda key: (-(values[key] - floors[key]), _sort_key(key)),
    )
    for key in ranked[:shortfall]:
        floors[key] += 1
    if min_each:
        # Satisfied by REDISTRIBUTION, not addition. Adding after the total is
        # fixed can push the send past a merchant boundary that route_cost already
        # budgeted for -- 23,999.49 + 0.3 + 0.2 targets 24,000, fits two 12,000
        # merchants, and shipping 24,002 needs three. The sheet would then
        # understate its own cost and breach the village's budget invisibly.
        #
        # So take from the largest entries and give to the starved ones: the sum
        # is untouched, every requested resource travels, and the donor loses a
        # unit or two out of thousands. Only keys with a nonzero input are floored
        # -- a route asked to carry lumber must not start carrying crop.
        wanted = [key for key, value in values.items() if value > 0]
        starved = [key for key in wanted if floors[key] < min_each]
        for key in starved:
            need = min_each - floors[key]
            # Largest first, and never below its own floor of min_each: robbing a
            # donor down to zero would recreate the bug in the other direction.
            donors = sorted(
                (k for k in wanted if k not in starved and floors[k] > min_each),
                key=lambda k: (-floors[k], _sort_key(k)),
            )
            for donor in donors:
                if need <= 0:
                    break
                spare = floors[donor] - min_each
                moved = min(spare, need)
                floors[donor] -= moved
                floors[key] += moved
                need -= moved
            if need > 0:
                # Nothing to redistribute: the total is smaller than the number of
                # resources asking for a unit, so no arrangement can give each one.
                # Exceeding the target is the only option left, and the route is
                # carrying almost nothing for it to matter.
                floors[key] += need
    return floors


def _sort_key(key: Hashable) -> str:
    """Stable ordering for tie-breaks across heterogeneous key types."""
    return str(key)
