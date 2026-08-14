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


def round_preserving_total(values: Mapping[K, float]) -> dict[K, int]:
    """Round *values* to integers whose sum is ``round(sum(values))``.

    Ties in the fractional part are broken by the key's sorted order, so the
    result is deterministic -- a re-plan on unchanged input must produce an
    identical setup sheet or the diff against the live routes is noise.

    Raises:
        ValueError: if any value is negative. Cargo is never negative, and the
            largest-remainder argument does not carry over to mixed signs.
    """
    if any(value < 0 for value in values.values()):
        raise ValueError(f"cargo amounts cannot be negative: {dict(values)}")
    if not values:
        return {}

    floors = {key: math.floor(value) for key, value in values.items()}
    shortfall = round(sum(values.values())) - sum(floors.values())

    if shortfall <= 0:
        return floors

    # Hand out the leftover units to the largest fractional parts first.
    ranked = sorted(
        values,
        key=lambda key: (-(values[key] - floors[key]), _sort_key(key)),
    )
    for key in ranked[:shortfall]:
        floors[key] += 1
    return floors


def _sort_key(key: Hashable) -> str:
    """Stable ordering for tie-breaks across heterogeneous key types."""
    return str(key)
