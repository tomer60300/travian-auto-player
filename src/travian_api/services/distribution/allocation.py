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


MATERIALS = (Resource.LUMBER, Resource.CLAY, Resource.IRON)
"""The three resources that are not crop, in the game's own order.

Here rather than in one of the modules that needs it, because four of them now
do -- the night profile, the optimizer's relay tier, the storage replay and the
web layer -- and each of the rules involved is a rule about "a material, not
crop": a granary is not NPC-fed, crop consumption is already net of upkeep, and
crop may relay through a sub-hub where a material may only cross a DECLARED one.
``night_profile.MATERIALS`` re-exports this so the older import keeps working.
"""


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
    npc_allowance_per_hour: float = 0.0
    """Most this village COULD convert into this resource per hour by NPC.

    A cap, and nothing is obliged to use it. Section 7's balancing: the operator
    exchanges what the village retains of the resources it is not shipping --
    clay and crop at the hub -- into the one it is short of, 1:1. Zero when no
    floor was declared, and zero while nobody is at the marketplace.

    Deliberately not a share of the warehouse spread over the window. That was
    the previous model and it made a shorter window RAISE the claim, because a
    level divided by fewer hours is a bigger rate. See ``npc.py``.
    """
    npc_draw_per_hour: float = 0.0
    """How much of that allowance this plan actually spends. Never an addend.

    Consumed only against unmet demand -- a negative retention target, which is
    the rate the village would have to ship beyond its production -- so a floor
    on a quiet village costs exactly nothing. The old model added the whole
    allowance to what the village had available, and every non-KEEP mode duly
    shipped it away.
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
    def net_per_hour(self) -> float:
        """Rate this village's STORE moves at once the plan runs.

        ``own + draw + ship`` is ``target_per_hour`` by construction -- closing
        that gap is the only thing shipping does -- so this is the target less
        what the village spends. Equal figures leave the store level, which is
        what an operator entering a consumption profile against a gross target
        is asking for, and it is the number the overflow and starvation checks
        need.

        Not a substitute for :attr:`ship_per_hour`, which is the cargo and is
        unaffected: known issue #1 is still that the sheet must carry the gap.
        """
        return self.target_per_hour - self.consumption_per_hour

    @property
    def ship_per_hour(self) -> float:
        """Rate that must arrive (positive) or leave (negative). The cargo.

        ``target - own - draw``. Never ship ``target_per_hour``: a village
        already produces some of its own target, and where NPC balancing funds
        the rest that too is already there rather than shipped in. The draw is
        subtracted, not added to a notion of "available", so it can only ever
        reduce the cargo -- which is what makes a floor on a village that needs
        nothing free.
        """
        return self.target_per_hour - self.own_per_hour - self.npc_draw_per_hour

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
    """Real production. Never includes the NPC draw -- every caller that
    reports "production" to the operator reads this."""
    total_npc_allowance: float
    """Conversion this resource COULD be funded by, account-wide. A ceiling."""
    total_npc_draw: float
    """Conversion the plan actually spends, account-wide. Carried apart from
    production so neither figure can be mistaken for the other: the account
    does not PRODUCE what its operator converts by hand."""
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


def _explicit_target(allocation: Allocation, own: float, total: float) -> float:
    """Retention rate this allocation asks for, before the remainder is settled.

    PRODUCTION semantics throughout, with no notion of "available" anywhere in
    it. KEEP returns own production and a quiet SUSTAIN returns own production,
    so a village with an NPC allowance it does not need has a target identical
    to one with no allowance at all -- and therefore ships nothing. The previous
    model passed ``own + allowance`` in here, which made a floor a standing
    instruction to convert resources and ship them to the remainder.
    """
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
    # produces: `ship = target - own - draw` is then zero for a village with no
    # draw, which is the whole of what KEEP means.
    return own


def resolve_resource(
    resource: Resource,
    productions: Mapping[int, float],
    allocations: Mapping[int, Allocation],
    names: Mapping[int, str] | None = None,
    npc_allowance: Mapping[int, float] | None = None,
    consumption: Mapping[int, float] | None = None,
) -> ResourcePlan:
    """Resolve one resource's allocations into per-village shipping rates.

    Args:
        resource: which resource this plan covers.
        productions: village id -> own net production per hour. May be negative
            for crop.
        allocations: village id -> allocation. Villages absent from this mapping
            default to :attr:`AllocationMode.KEEP` and neither send nor receive.
        npc_allowance: village id -> the MOST this village could convert into
            this resource per hour by NPC (section 7). A cap consumed only
            against unmet demand, never added to what the village has: a
            village listed here that needs nothing draws nothing and ships
            nothing. Presence in this mapping is itself the signal that the
            operator declared a floor, so a village that relied on conversion
            and came up short can be told from one that never asked -- which is
            why zero is a meaningful value here and absence is not the same
            thing. Materials only; a granary is not NPC-fed.
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

    allowance: Mapping[int, float] = npc_allowance or {}
    unknown_allowance = set(allowance) - set(productions)
    if unknown_allowance:
        raise AllocationError(
            "npc_allowance references villages with no production: "
            + ", ".join(village_label(vid, names) for vid in sorted(unknown_allowance))
        )
    negative = sorted(vid for vid, amount in allowance.items() if amount < 0)
    if negative:
        raise AllocationError(
            "npc_allowance cannot be negative: "
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
    # share of what the operator can convert as well would move every village's
    # target whenever they changed one village's floor.
    total_npc_allowance = sum(allowance.values())
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
        targets[vid] = _explicit_target(allocation, own, total)

    # Section 7's draw, in two lines of arithmetic and one insight: a NEGATIVE
    # retention target is exactly the rate a village would have to ship beyond
    # its production, because `-(target - own) - own = -target`. So the unmet
    # demand a conversion may cover is `max(0, -target)` and nothing else -- and
    # a village whose target is non-negative (it keeps what it makes, or is
    # being shipped to) draws nothing however large its allowance.
    #
    # The remainder village is the case that matters, and it is settled with the
    # same formula: its no-draw target is `total - explicit`, which goes negative
    # exactly when the account over-claims.
    no_draw_remainder = total - sum(targets.values())
    draws: dict[int, float] = {}
    for vid, cap in allowance.items():
        retention = no_draw_remainder if vid == remainder_id else targets.get(vid, 0.0)
        need = max(0.0, -retention)
        draws[vid] = min(need, cap)
        # Fail loudly rather than silently degrade. The plan below still emits
        # what the conversion CAN fund, because half-funded routes are what the
        # operator will actually see, but the verdict refuses on this finding:
        # cargo resting on trading nobody can do is not a plan that merely
        # misses a target.
        if need > cap + EPSILON:
            label = village_label(vid, names)
            findings.append(
                Finding(
                    category=Category.NPC_CAPACITY_SHORT,
                    message=(
                        f"{label} must ship {need:,.0f}/h of {resource.value} beyond its "
                        f"production, but NPC can only convert {cap:,.0f}/h from the "
                        f"feedstock it retains -- {need - cap:,.0f}/h short, so those "
                        f"routes will arrive short"
                    ),
                    detail=f"{label} — short {need - cap:,.0f}/h of {resource.value}",
                    village=label,
                    resource=resource,
                )
            )
    total_npc_draw = sum(draws.values())

    unallocated = total + total_npc_draw - sum(targets.values())

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
                        f"{'production and NPC conversion' if total_npc_draw else 'production'}"
                        f" by {-unallocated:.0f}/h, so the remainder village {label} "
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
            npc_allowance_per_hour=allowance.get(vid, 0.0),
            npc_draw_per_hour=draws.get(vid, 0.0),
            consumption_per_hour=spend.get(vid, 0.0),
        )
        for vid, own in sorted(productions.items())
    )

    return ResourcePlan(
        resource=resource,
        total_production=total,
        total_npc_allowance=total_npc_allowance,
        total_npc_draw=total_npc_draw,
        villages=villages,
        remainder_village_id=remainder_id,
        unallocated=unallocated,
        findings=tuple(findings),
    )
