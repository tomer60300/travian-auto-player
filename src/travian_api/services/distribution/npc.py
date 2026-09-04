"""Section 7: NPC balancing -- what the operator's gold can and cannot fund.

The NPC merchant exchanges one resource type for another **1:1 inside a single
village** (3 gold, minimum 50 resources, no cooldown, Marketplace required --
mechanics reference I.6.4). It conserves the village's total, so it can neither
create resources nor exceed a store's cap. The operator's own rule for this
account, spec section 7 verbatim:

    Performed at 02 only. Triggers: wood is low, or crop exceeds 700,000.
    Direction: convert from surplus (clay, crop) into wood. No daily conversion
    cap -- the operator handles gold. Crop surplus banks at 02 as feedstock.

The planner does not press the button. It sizes what pressing it can fund, and
reports when the operator should.

Three separations do the work, and each one is a bug the previous build made:

**A level is not a rate.** ``stock_floor_fraction`` names a BUFFER LEVEL --
``fraction x warehouse_capacity``, materials only, warehouse only, because a
granary is not NPC-fed. It is never a source of supply. The old code spread
that level over the dispatch window (``capacity x fraction / window_hours``),
so *halving the window doubled the claim*: an 8-hour night profile asked 45,000
an hour off the same warehouse a 16-hour day profile asked 22,500 of.

**The conversion rate comes from the feedstock, and is a cap, never an
addend.** What a village can convert per hour is what it RETAINS per hour of
the resources it is not drawing on -- clay and crop at 02. A rate derived from
rates, so no window length can move it. And it is spent only against unmet
demand: ``ship = target - own - draw``, where the draw is bounded by the
allowance AND by what the village is genuinely short of. So a floor on a quiet
village costs nothing, where the old addend had every non-KEEP mode ship the
whole allowance away.

**The reservoir is finite.** A replay that tops a store back to its floor
however often it is asked is an infinite reservoir, and it reported cargo
nobody could have funded. Here the top-up comes out of a budget that accrues at
the allowance and exhausts, the feedstock store is debited by exactly what it
funded, and the top-up is booked as an inflow so the day's net gain stays true.

Overnight time-shifting -- a feeder banking eight hours and shipping over
sixteen -- is a **different feature** and is not here. Conflating the two is
what produced the original bugs.

Pure functions and frozen records. Nothing here spends a game request.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from .allocation import EPSILON, MATERIALS, AllocationError, Resource, village_label
from .findings import Category, Finding

HOURS_PER_DAY = 24.0

CROP_FEEDSTOCK_TRIGGER = 700_000.0
"""Section 7's second trigger: crop EXCEEDS 700,000 at the balancing village.

A LEVEL, not a rate, and the spec states it as a level -- the crop surplus
(~+80,000/h after obligations) banks at 02 and 700,000 is where the operator
starts converting it. Strictly greater than, because "exceeds" is what the spec
says: 700,000 exactly has not exceeded it.

Reporting only. The planner never converts on the operator's behalf.
"""

MIN_FUNDED_ALLOWANCE_PER_DAY = 1.0
"""Below one resource a DAY, an allowance is float residue rather than a budget.

Only :meth:`NpcReserve.__post_init__` reads it, to decide whether an allowance
with no feedstock behind it is a contradiction to refuse or a rounding artifact
to ignore. Nothing is ever converted at this scale (the game's own NPC minimum
is 50 resources per exchange), so it never changes an outcome.
"""


class TriggerKind(StrEnum):
    """Which of section 7's two triggers fired."""

    WOOD_LOW = "wood_low"
    CROP_BANKED = "crop_banked"


@dataclass(frozen=True)
class NpcPolicy:
    """What the operator declared about NPC balancing, before any arithmetic.

    ``floor_level`` is the buffer LEVEL per village -- ``stock_floor_fraction x
    warehouse_capacity``, in resources, and the caller is the only party that
    knows the capacity. Villages absent from it declared no floor at all, and a
    fraction of ``0.0`` is exactly that absence (``0.0 is None`` at every
    layer), so they never appear here.

    ``attended`` is whether the operator is awake to press the button during
    the hours this plan runs. It is REQUIRED of the caller rather than guessed:
    the account sleeps through the night window, and a default of "attended"
    would fund night routes from trading nobody is doing.

    ``sources`` overrides the derived feedstock for a village. Derived is
    everything the village is NOT drawing on, which is the honest default; an
    override that names a resource the village draws on is a contradiction
    (converting wood into wood) and is refused rather than trimmed.
    """

    # First and undefaulted, which is the whole of the fix: a dataclass default
    # here IS the guess the docstring above refuses, and `attended = True` is
    # the direction that over-commits in silence. Every construction passes it
    # by keyword, so the order costs nothing.
    attended: bool
    floor_level: Mapping[int, float] = field(default_factory=dict)
    sources: Mapping[int, frozenset[Resource]] = field(default_factory=dict)

    @property
    def is_declared(self) -> bool:
        return bool(self.floor_level)


@dataclass(frozen=True)
class NpcReserve:
    """One village's NPC capacity, as the allocation layer and the replays use it.

    ``allowance_per_day`` is the conversion budget the feedstock can fund over a
    day. It is a rate expressed as a daily total and never a share of a store,
    so it does not move when the window does.

    ``sources``/``shares`` say which stores pay for it and in what proportion,
    1:1. The proportion is each source's share of the retention that produced
    the allowance, so the store that funds most of it is debited most.
    """

    village_id: int
    floor_level: float
    allowance_per_day: float
    sources: tuple[Resource, ...] = ()
    shares: tuple[float, ...] = ()
    drawn: frozenset[Resource] = frozenset()
    """Resources this village must ship beyond its production, so cannot fund
    the conversion with. The complement of ``sources`` when nothing overrode it."""

    def __post_init__(self) -> None:
        if self.allowance_per_day < 0:
            raise AllocationError(f"NPC allowance cannot be negative, got {self.allowance_per_day}")
        if len(self.sources) != len(self.shares):
            raise AllocationError(
                f"NPC reserve has {len(self.sources)} feedstock sources and "
                f"{len(self.shares)} shares; they name the same stores"
            )
        if self.allowance_per_day > MIN_FUNDED_ALLOWANCE_PER_DAY and not self.sources:
            raise AllocationError(
                f"NPC allowance of {self.allowance_per_day:,.0f}/day has no feedstock "
                f"to convert from; NPC is an exchange and cannot create resources"
            )
        if Resource.CROP in self.drawn:
            raise AllocationError("a granary is not NPC-fed, so crop can never be drawn")

    @property
    def allowance_per_hour(self) -> float:
        return self.allowance_per_day / HOURS_PER_DAY

    def share_of(self, resource: Resource) -> float:
        """Fraction of every conversion this store pays for. Zero if not a source."""
        for source, share in zip(self.sources, self.shares, strict=True):
            if source is resource:
                return share
        return 0.0


def _need(retention: float) -> float:
    """How much a village must ship BEYOND its production, from its target.

    ``ship = target - own`` and shipping beyond production is
    ``-ship - own = -target``. So a *negative retention target* is exactly the
    unmet demand a draw may cover, and a non-negative one needs nothing: the
    village is either keeping what it makes or being shipped to. This one line
    is why a floor on a quiet village costs nothing.
    """
    return max(0.0, -retention)


def derive_reserves(
    policy: NpcPolicy,
    retention: Mapping[Resource, Mapping[int, float]],
    names: Mapping[int, str] | None = None,
) -> tuple[dict[int, NpcReserve], tuple[Finding, ...]]:
    """Size each declared floor's conversion budget from pass-1 retention.

    Args:
        policy: what the operator declared -- floors, attendance, any override.
        retention: resource -> village -> ``target_per_hour`` from a solve with
            NO draw in it. Retention rather than production deliberately: a
            village can only convert what it KEEPS, and production it ships
            away is not feedstock. A resource absent from this mapping was not
            planned at all; a village absent within a planned resource had a
            rate that could not be read.
        names: how villages are named to the operator.

    Returns:
        The reserves, keyed by village, and any findings raised sizing them.
        A village whose feedstock includes an unreadable rate gets an allowance
        of zero and an ``UNREADABLE_RATE`` finding -- an allowance quietly
        computed from the readable half would overstate nothing and understate
        the risk, and section 7's rule is to fail loudly.

    Raises:
        AllocationError: an explicit ``sources`` override names a resource the
            village is drawing on.
    """
    reserves: dict[int, NpcReserve] = {}
    findings: list[Finding] = []

    for vid, floor_level in sorted(policy.floor_level.items()):
        drawn = frozenset(
            resource
            for resource in MATERIALS
            if _need(retention.get(resource, {}).get(vid, 0.0)) > EPSILON
        )
        override = policy.sources.get(vid)
        if override is None:
            sources = frozenset(r for r in Resource if r not in drawn)
        else:
            clash = sorted(override & drawn, key=lambda r: r.value)
            if clash:
                raise AllocationError(
                    f"{village_label(vid, names)}: NPC feedstock names "
                    + ", ".join(r.value for r in clash)
                    + ", which this village is already drawing on -- NPC exchanges "
                    "one resource for another and cannot convert a resource into itself"
                )
            sources = override

        # A rate that could not be read is not a zero. Crop is the one nullable
        # rate in the snapshot and it is the account's largest feedstock, so a
        # missing reading there is the difference between a funded plan and an
        # unfunded one.
        unreadable = sorted(
            (r for r in sources if r in retention and vid not in retention[r]),
            key=lambda r: r.value,
        )
        if unreadable:
            label = village_label(vid, names)
            findings.append(
                Finding(
                    category=Category.UNREADABLE_RATE,
                    message=(
                        f"{label} has a stock floor but no readable "
                        + ", ".join(r.value for r in unreadable)
                        + " rate, so its NPC feedstock cannot be sized; the floor "
                        "funds nothing in this plan"
                    ),
                    detail=f"{label} — " + ", ".join(r.value for r in unreadable),
                    village=label,
                )
            )
            reserves[vid] = NpcReserve(
                village_id=vid, floor_level=floor_level, allowance_per_day=0.0, drawn=drawn
            )
            continue

        contribution = {
            resource: max(0.0, retention.get(resource, {}).get(vid, 0.0))
            for resource in sorted(sources, key=lambda r: r.value)
        }
        feedstock_per_hour = sum(contribution.values())
        # Asleep is not a rate of zero feedstock -- the crop is still growing --
        # it is a rate of zero CONVERSION, because nobody is at the marketplace.
        allowance_per_hour = feedstock_per_hour if policy.attended else 0.0
        paying = {r: amount for r, amount in contribution.items() if amount > 0.0}
        total = sum(paying.values())
        reserves[vid] = NpcReserve(
            village_id=vid,
            floor_level=floor_level,
            allowance_per_day=allowance_per_hour * HOURS_PER_DAY,
            sources=tuple(paying),
            shares=tuple(amount / total for amount in paying.values()) if total else (),
            drawn=drawn,
        )

    return reserves, tuple(findings)


def draw_allowance(
    reserves: Mapping[int, NpcReserve],
    retention: Mapping[Resource, Mapping[int, float]],
) -> dict[Resource, dict[int, float]]:
    """Split each village's one budget across the materials it draws on.

    One reserve funds every material at that village, so the budget has to be
    apportioned before the per-resource solve can use it. Proportional to need,
    which is the only split no resource ordering biases.

    Every floored village appears under every material even at zero: the
    allocation layer needs the DECLARATION, not just the number, to tell a
    village that relied on NPC and came up short from one that never asked.
    Crop is never here -- a granary is not NPC-fed.
    """
    allowance: dict[Resource, dict[int, float]] = {r: {} for r in MATERIALS}
    for vid, reserve in reserves.items():
        needs = {
            resource: _need(retention.get(resource, {}).get(vid, 0.0)) for resource in MATERIALS
        }
        total = sum(needs.values())
        for resource in MATERIALS:
            allowance[resource][vid] = (
                reserve.allowance_per_hour * needs[resource] / total if total > 0 else 0.0
            )
    return allowance


@dataclass(frozen=True)
class NpcTrigger:
    """One of section 7's two triggers, fired. Advice, never an action.

    ``level`` is what the store holds and ``threshold`` what it was measured
    against; ``projected`` says whether it was where the plan LEAVES the store
    after a day rather than where the snapshot found it.
    """

    village_id: int
    kind: TriggerKind
    resource: Resource
    level: float
    threshold: float
    projected: bool


def _projected(level: float, net_per_hour: float, capacity: float | None) -> float:
    """Where a day of the plan leaves this store, clamped as the game clamps it."""
    after = level + net_per_hour * HOURS_PER_DAY
    if capacity is not None:
        after = min(after, capacity)
    return max(0.0, after)


def evaluate_triggers(
    reserves: Mapping[int, NpcReserve],
    stocks: Mapping[int, Mapping[Resource, float]],
    capacities: Mapping[int, Mapping[Resource, float]],
    net_per_hour: Mapping[int, Mapping[Resource, float]],
) -> tuple[NpcTrigger, ...]:
    """Section 7's two triggers, read off the plan's own emitted figures.

    Read from the EMITTED rows rather than from allocation intent: ``net_per_hour``
    is what each store does once the routes run and the conversion has been paid
    for, so a store the plan drains is reported as the plan leaves it and not as
    the snapshot happened to catch it.

    **"Crop exceeds 700,000"** is a level and needs no interpretation.

    **"Wood is low" has no number in the spec.** The reading chosen here is the
    village's OWN declared floor: ``stock_floor_fraction x warehouse_capacity``
    on lumber, at or below which the buffer is gone. It is grounded rather than
    invented -- section 5 states 02's warehouse is kept "at least 25% stocked on
    wood", which IS the floor -- and it needs no new constant, so nothing here
    is a number nobody chose. A village with no declared floor has no wood-low
    reading at all and is not guessed at.
    """
    triggers: list[NpcTrigger] = []
    for vid, reserve in sorted(reserves.items()):
        held = stocks.get(vid, {})
        caps = capacities.get(vid, {})
        nets = net_per_hour.get(vid, {})

        wood_now = held.get(Resource.LUMBER)
        if wood_now is not None and reserve.floor_level > 0:
            wood_after = _projected(
                wood_now, nets.get(Resource.LUMBER, 0.0), caps.get(Resource.LUMBER)
            )
            lowest = min(wood_now, wood_after)
            if lowest <= reserve.floor_level:
                triggers.append(
                    NpcTrigger(
                        village_id=vid,
                        kind=TriggerKind.WOOD_LOW,
                        resource=Resource.LUMBER,
                        level=lowest,
                        threshold=reserve.floor_level,
                        projected=wood_after < wood_now,
                    )
                )

        crop_now = held.get(Resource.CROP)
        if crop_now is not None:
            crop_after = _projected(crop_now, nets.get(Resource.CROP, 0.0), caps.get(Resource.CROP))
            highest = max(crop_now, crop_after)
            if highest > CROP_FEEDSTOCK_TRIGGER:
                triggers.append(
                    NpcTrigger(
                        village_id=vid,
                        kind=TriggerKind.CROP_BANKED,
                        resource=Resource.CROP,
                        level=highest,
                        threshold=CROP_FEEDSTOCK_TRIGGER,
                        projected=crop_after > crop_now,
                    )
                )
    return tuple(triggers)


def trigger_findings(
    triggers: tuple[NpcTrigger, ...], names: Mapping[int, str] | None = None
) -> tuple[Finding, ...]:
    """The triggers as findings. Two categories, because they say opposite things.

    Wood low is a shortage about to under-deliver routes; crop banked is a
    surplus waiting to be spent. Severity belongs to the category here, so they
    cannot share one.
    """
    out: list[Finding] = []
    for trigger in triggers:
        label = village_label(trigger.village_id, names)
        when = "the plan leaves it at" if trigger.projected else "it holds"
        if trigger.kind is TriggerKind.WOOD_LOW:
            out.append(
                Finding(
                    category=Category.NPC_WOOD_LOW,
                    message=(
                        f"{label} is low on wood: {when} {trigger.level:,.0f}, at or "
                        f"below its {trigger.threshold:,.0f} floor -- section 7's first "
                        f"NPC trigger, so convert clay or crop into lumber there"
                    ),
                    detail=f"{label} — {trigger.level:,.0f} of {trigger.threshold:,.0f} floor",
                    village=label,
                    resource=trigger.resource,
                )
            )
        else:
            out.append(
                Finding(
                    category=Category.NPC_CROP_BANKED,
                    message=(
                        f"{label} has crop banked past section 7's {trigger.threshold:,.0f} "
                        f"trigger: {when} {trigger.level:,.0f}, which is NPC feedstock "
                        f"standing idle"
                    ),
                    detail=f"{label} — {trigger.level:,.0f} banked",
                    village=label,
                    resource=trigger.resource,
                )
            )
    return tuple(out)
