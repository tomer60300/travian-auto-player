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

from .findings import Category, Finding

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


def village_label(village_id: int, names: Mapping[int, str] | None = None) -> str:
    """How a village is named to the operator.

    Every message a human reads goes through here. A village id is an internal
    handle -- nobody running the account knows which village 53629 is, and a
    warning they cannot act on is barely better than no warning. Falls back to
    the id only when there is genuinely no name to use, and says so.
    """
    name = (names or {}).get(village_id)
    return name if name else f"village {village_id}"


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
        if self.mode is AllocationMode.ABSOLUTE and self.value < 0:
            raise AllocationError(f"absolute retention cannot be negative, got {self.value}")


@dataclass(frozen=True)
class VillageAllocation:
    """What one village keeps, and what must move to make that true."""

    village_id: int
    mode: AllocationMode
    own_per_hour: float
    target_per_hour: float
    supplement_per_hour: float = 0.0
    """Extra supply this village can draw on beyond its production, per hour.

    A warehouse the operator keeps stocked by NPC trading is real supply: the
    village can ship from it, so the plan may ask it to. Defaults to zero, which
    is the whole existing planner.
    """
    consumption_per_hour: float = 0.0
    """What this village SPENDS per hour. Not a claim on the account pool.

    ``target_per_hour`` says what must be here; this says what leaves again
    through the building queue and the troop upkeep. They were one number, and
    the surviving meaning was accumulation: an army village told to hold
    14,751 lumber an hour was modelled as stockpiling all of it, so the plan
    reported 354,024/day (= 14,751 x 24) lost at its warehouse cap for a
    village that in fact spends every unit. Defaults to zero, which is the
    whole existing planner: net then IS the target, exactly as before.
    """

    @property
    def available_per_hour(self) -> float:
        """What the village can actually put on a cart: production plus stock."""
        return self.own_per_hour + self.supplement_per_hour

    @property
    def net_per_hour(self) -> float:
        """Rate this village's STORE moves at once the plan runs.

        ``available_per_hour + ship_per_hour`` is ``target_per_hour`` by
        construction -- closing that gap is the only thing shipping does -- so
        this is the target less what the village spends. Equal figures leave the
        store level, which is what an operator entering a consumption profile
        against a gross target is asking for, and it is the number the overflow
        and starvation checks need.

        Not a substitute for :attr:`ship_per_hour`, which is the cargo and is
        unaffected: known issue #1 is still that the sheet must carry the gap.
        """
        return self.available_per_hour + self.ship_per_hour - self.consumption_per_hour

    @property
    def ship_per_hour(self) -> float:
        """Rate that must arrive (positive) or leave (negative). The cargo.

        Never ship ``target_per_hour``; a village already has
        ``available_per_hour`` of its own target. Measured against *available*
        rather than production, which is what lets a village with a stock floor
        ship more than it makes -- the point of the supplement.
        """
        return self.target_per_hour - self.available_per_hour

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
    """Real production. Never includes the supplement -- every caller that
    reports "production" to the operator reads this."""
    total_supplement: float
    """Stock-funded supply across the account, carried apart from production so
    neither figure can be mistaken for the other."""
    villages: tuple[VillageAllocation, ...]
    remainder_village_id: int | None
    unallocated: float
    findings: tuple[Finding, ...]

    @property
    def warnings(self) -> tuple[str, ...]:
        """The findings as the flat prose list every caller has always read."""
        return tuple(f.message for f in self.findings)

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


def _explicit_target(allocation: Allocation, own: float, total: float, available: float) -> float:
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
    # KEEP means neither send nor receive, so the target is what the village
    # already has -- INCLUDING any stock it draws on. Returning production here
    # would make a keep village ship its whole allowance to the remainder.
    return available


def resolve_resource(
    resource: Resource,
    productions: Mapping[int, float],
    allocations: Mapping[int, Allocation],
    names: Mapping[int, str] | None = None,
    supplement: Mapping[int, float] | None = None,
    consumption: Mapping[int, float] | None = None,
) -> ResourcePlan:
    """Resolve one resource's allocations into per-village shipping rates.

    Args:
        resource: which resource this plan covers.
        productions: village id -> own net production per hour. May be negative
            for crop.
        allocations: village id -> allocation. Villages absent from this mapping
            default to :attr:`AllocationMode.KEEP` and neither send nor receive.
        supplement: village id -> extra supply per hour beyond production, from
            stock the operator keeps topped up. Raises available, never
            production. Materials only in practice -- a granary is not NPC-fed.
        consumption: village id -> what the village SPENDS per hour. Changes
            :attr:`VillageAllocation.net_per_hour` and nothing else: it is not a
            claim on the account pool, so the targets, the remainder and the
            cargo are all untouched. Absent means zero, which reproduces the
            pre-consumption planner exactly.

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
            "allocations reference villages with no production: "
            + ", ".join(village_label(vid, names) for vid in sorted(unknown))
        )

    extra: Mapping[int, float] = supplement or {}
    unknown_supplement = set(extra) - set(productions)
    if unknown_supplement:
        raise AllocationError(
            "supplement references villages with no production: "
            + ", ".join(village_label(vid, names) for vid in sorted(unknown_supplement))
        )
    negative = sorted(vid for vid, amount in extra.items() if amount < 0)
    if negative:
        raise AllocationError(
            "supplement cannot be negative: "
            + ", ".join(village_label(vid, names) for vid in negative)
        )

    spend: Mapping[int, float] = consumption or {}
    unknown_consumption = set(spend) - set(productions)
    if unknown_consumption:
        raise AllocationError(
            "consumption references villages with no production: "
            + ", ".join(village_label(vid, names) for vid in sorted(unknown_consumption))
        )
    # Refused rather than read as extra production. Inferring consumption from a
    # negative rate was the rejected alternative: the statistics page reports
    # materials GROSS, so a village consuming lumber still reads positive and
    # there is no sign anywhere to invert.
    overspending = sorted(vid for vid, amount in spend.items() if amount < 0)
    if overspending:
        raise AllocationError(
            "consumption cannot be negative: "
            + ", ".join(village_label(vid, names) for vid in overspending)
        )

    remainder_ids = [vid for vid, a in allocations.items() if a.mode is AllocationMode.REMAINDER]
    if len(remainder_ids) > 1:
        raise AllocationError(
            f"{resource.value}: exactly one remainder village is allowed, got "
            + ", ".join(village_label(vid, names) for vid in sorted(remainder_ids))
            + ". Slack has to land somewhere specific."
        )
    remainder_id = remainder_ids[0] if remainder_ids else None

    total = sum(productions.values())
    # Percentage targets stay a share of PRODUCTION, as their docstring says: a
    # share of the stock as well would move every village's target whenever the
    # operator changed one village's floor.
    total_supplement = sum(extra.values())
    total_available = total + total_supplement
    findings: list[Finding] = []

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
            label = village_label(vid, names)
            findings.append(
                Finding(
                    category=Category.SUSTAIN_NOOP,
                    message=(
                        f"{label} is set to sustain but its {resource.value} "
                        f"production is not negative ({own:.0f}/h); nothing to sustain"
                    ),
                    detail=f"{label} — {own:+,.0f}/h",
                    village=label,
                    resource=resource,
                )
            )
        targets[vid] = _explicit_target(allocation, own, total, own + extra.get(vid, 0.0))

    unallocated = total_available - sum(targets.values())

    if remainder_id is not None:
        # The remainder holds whatever is left. When over-allocated this goes
        # negative, which the day-check needs verbatim to model the sender
        # draining its stock. The PLAN must not treat that negative target as
        # sustainable surplus, though: the optimizer caps each sender's shippable
        # surplus at its own production, so the shortfall surfaces there and the
        # plan is reported infeasible rather than emitting an impossible rate.
        targets[remainder_id] = unallocated
        if unallocated < -EPSILON:
            label = village_label(remainder_id, names)
            findings.append(
                Finding(
                    category=Category.OVER_ALLOCATED,
                    message=(
                        f"{resource.value}: allocations exceed "
                        f"{'available supply' if total_supplement else 'production'} by "
                        f"{-unallocated:.0f}/h, so the remainder village {label} "
                        f"would have to send more than it has"
                    ),
                    detail=f"{label} — short {-unallocated:,.0f}/h",
                    village=label,
                    resource=resource,
                )
            )
    elif abs(unallocated) > EPSILON:
        findings.append(
            Finding(
                category=Category.UNALLOCATED,
                message=(
                    f"{resource.value}: {unallocated:.0f}/h is unallocated and no "
                    f"remainder village is set, so it will pile up wherever it is produced"
                ),
                detail=f"{unallocated:+,.0f}/h unassigned",
                resource=resource,
            )
        )

    villages = tuple(
        VillageAllocation(
            village_id=vid,
            mode=allocations.get(vid, Allocation(AllocationMode.KEEP)).mode,
            own_per_hour=own,
            target_per_hour=targets[vid],
            supplement_per_hour=extra.get(vid, 0.0),
            consumption_per_hour=spend.get(vid, 0.0),
        )
        for vid, own in sorted(productions.items())
    )

    return ResourcePlan(
        resource=resource,
        total_production=total,
        total_supplement=total_supplement,
        villages=villages,
        remainder_village_id=remainder_id,
        unallocated=unallocated,
        findings=tuple(findings),
    )
