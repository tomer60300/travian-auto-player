"""Allocation modes resolved into per-village shipping rates.

The one thing this module exists to get right is **ship, not target**. Known
issue #1 in ``docs/25-resource-distribution-planner.md`` is shipping a village's
*target* when the correct cargo is the *gap* between its target and what it
already produces -- an error made twice by hand. So :class:`VillageAllocation`
computes ``ship_per_hour`` itself and there is no way to obtain a target without
the ship figure sitting beside it.

Rates only. Turning a rate into an integer cargo per send, with sum-preserving
rounding, belongs to the route stage -- rounding here would compound across
cycle lengths.

Sign convention: ``ship_per_hour`` is **positive into** the village. A negative
value means the village sends that much away. Crop production may itself be
negative for army villages, which is the case that makes sustain mode necessary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

# Rates are floats; treat anything under this as zero rather than warning about
# a rounding artifact of the percentage arithmetic. Conservation scales this by
# the account total, because summing twenty five-figure rates accumulates more
# float error than a fixed absolute tolerance allows for.
EPSILON = 1e-6
CONSERVATION_RELATIVE_TOLERANCE = 1e-9


class Resource(StrEnum):
    LUMBER = "lumber"
    CLAY = "clay"
    IRON = "iron"
    CROP = "crop"


class AllocationMode(StrEnum):
    PERCENTAGE = "percentage"
    """Share of total account production of this resource, 0-100."""

    ABSOLUTE = "absolute"
    """Fixed retention rate per hour."""

    SUSTAIN = "sustain"
    """Cover a negative production deficit plus ``value`` percent headroom."""

    REMAINDER = "remainder"
    """Absorb everything unallocated. Exactly one village per resource."""

    KEEP = "keep"
    """Neither send nor receive. The default for unlisted villages."""


class AllocationError(ValueError):
    """The allocation set cannot be resolved as written."""


@dataclass(frozen=True)
class Allocation:
    """One village's instruction for one resource."""

    mode: AllocationMode
    value: float = 0.0

    def __post_init__(self) -> None:
        if self.mode is AllocationMode.PERCENTAGE and not 0 <= self.value <= 100:
            raise AllocationError(f"percentage must be 0-100, got {self.value}")
        if self.mode is AllocationMode.SUSTAIN and self.value < 0:
            raise AllocationError(f"sustain headroom cannot be negative, got {self.value}")


@dataclass(frozen=True)
class VillageAllocation:
    """What one village keeps, and what must move to make that true."""

    village_id: int
    mode: AllocationMode
    own_per_hour: float
    target_per_hour: float

    @property
    def ship_per_hour(self) -> float:
        """Rate that must arrive (positive) or leave (negative). The cargo.

        Never ship ``target_per_hour``; a village already produces
        ``own_per_hour`` of its own target.
        """
        return self.target_per_hour - self.own_per_hour

    @property
    def is_receiver(self) -> bool:
        return self.ship_per_hour > EPSILON

    @property
    def is_sender(self) -> bool:
        return self.ship_per_hour < -EPSILON


@dataclass(frozen=True)
class ResourcePlan:
    """Resolved allocation for one resource across the account."""

    resource: Resource
    total_production: float
    villages: tuple[VillageAllocation, ...]
    remainder_village_id: int | None
    unallocated: float
    warnings: tuple[str, ...]

    @property
    def is_conserved(self) -> bool:
        """True when shipping nets to zero -- nothing created or destroyed."""
        tolerance = max(EPSILON, abs(self.total_production) * CONSERVATION_RELATIVE_TOLERANCE)
        return abs(sum(v.ship_per_hour for v in self.villages)) <= tolerance

    @property
    def receivers(self) -> tuple[VillageAllocation, ...]:
        return tuple(v for v in self.villages if v.is_receiver)

    @property
    def senders(self) -> tuple[VillageAllocation, ...]:
        return tuple(v for v in self.villages if v.is_sender)


def _explicit_target(allocation: Allocation, own: float, total: float) -> float:
    """Retention rate this allocation asks for, before the remainder is settled."""
    if allocation.mode is AllocationMode.PERCENTAGE:
        return total * allocation.value / 100.0
    if allocation.mode is AllocationMode.ABSOLUTE:
        return allocation.value
    if allocation.mode is AllocationMode.SUSTAIN:
        # Ship the whole deficit plus headroom, so the village ends up positive
        # by exactly the headroom. A village that is not in deficit needs
        # nothing: sustain is a floor, not a top-up.
        if own >= 0:
            return own
        return -own * allocation.value / 100.0
    return own  # KEEP


def resolve_resource(
    resource: Resource,
    productions: Mapping[int, float],
    allocations: Mapping[int, Allocation],
) -> ResourcePlan:
    """Resolve one resource's allocations into per-village shipping rates.

    Args:
        resource: which resource this plan covers.
        productions: village id -> own net production per hour. May be negative
            for crop.
        allocations: village id -> allocation. Villages absent from this mapping
            default to :attr:`AllocationMode.KEEP` and neither send nor receive.

    Returns:
        A :class:`ResourcePlan`. Problems that leave the plan usable but wrong
        (unassigned slack, over-allocation) are reported in ``warnings`` rather
        than raised, so the UI can show them in place; problems that make the
        plan meaningless raise.

    Raises:
        AllocationError: more than one remainder village, an allocation for a
            village with no production figure, or a percentage allocation
            against a negative account total.
    """
    unknown = set(allocations) - set(productions)
    if unknown:
        raise AllocationError(
            f"allocations reference villages with no production: {sorted(unknown)}"
        )

    remainder_ids = [vid for vid, a in allocations.items() if a.mode is AllocationMode.REMAINDER]
    if len(remainder_ids) > 1:
        raise AllocationError(
            f"{resource.value}: exactly one remainder village is allowed, got "
            f"{sorted(remainder_ids)}. Slack has to land somewhere specific."
        )
    remainder_id = remainder_ids[0] if remainder_ids else None

    total = sum(productions.values())
    warnings: list[str] = []

    # A percentage of a negative total is meaningless: 30% of an account that is
    # net -4,000 crop/h is a target of -1,200, which reads as an instruction to
    # ship crop away from a village that is already starving. The resolved
    # routes would be wrong, not merely noisy, so this raises rather than warns.
    if total < 0 and any(a.mode is AllocationMode.PERCENTAGE for a in allocations.values()):
        raise AllocationError(
            f"{resource.value}: account production is negative ({total:.0f}/h), so "
            f"percentage targets resolve to negative amounts. Use absolute or "
            f"sustain targets until the account is net positive."
        )

    targets: dict[int, float] = {}
    for vid, own in productions.items():
        allocation = allocations.get(vid, Allocation(AllocationMode.KEEP))
        if allocation.mode is AllocationMode.REMAINDER:
            continue  # settled below, once everything else is known
        if allocation.mode is AllocationMode.SUSTAIN and own >= 0:
            warnings.append(
                f"village {vid} is set to sustain but its {resource.value} "
                f"production is not negative ({own:.0f}/h); nothing to sustain"
            )
        targets[vid] = _explicit_target(allocation, own, total)

    unallocated = total - sum(targets.values())

    if remainder_id is not None:
        targets[remainder_id] = unallocated
        if unallocated < -EPSILON:
            warnings.append(
                f"{resource.value}: allocations exceed production by "
                f"{-unallocated:.0f}/h, so the remainder village {remainder_id} "
                f"would have to send more than it has"
            )
    elif abs(unallocated) > EPSILON:
        warnings.append(
            f"{resource.value}: {unallocated:.0f}/h is unallocated and no "
            f"remainder village is set, so it will pile up wherever it is produced"
        )

    villages = tuple(
        VillageAllocation(
            village_id=vid,
            mode=allocations.get(vid, Allocation(AllocationMode.KEEP)).mode,
            own_per_hour=own,
            target_per_hour=targets[vid],
        )
        for vid, own in sorted(productions.items())
    )

    return ResourcePlan(
        resource=resource,
        total_production=total,
        villages=villages,
        remainder_village_id=remainder_id,
        unallocated=unallocated,
        warnings=tuple(warnings),
    )
