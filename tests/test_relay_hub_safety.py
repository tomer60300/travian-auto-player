"""A relay hub must survive its own inbound leg failing.

The danger is asymmetric, and that asymmetry is the whole rule. A hub whose crop
production is non-negative cannot be harmed by relaying: if the leg refilling it
is late, or fails outright, it forwards less and holds its own. A hub whose
production is negative -- troops eating more than the fields grow -- is draining
already, and shipping crop out on top of that turns a slow loss into an empty
granary and starving troops.

Found on a live account. Two of seven night relay chains forwarded through
villages producing -3,037/h and -874/h. The second held 32,597 crop, forwarded
8,410/h, and its refill was a SINGLE route on an eight-hour cycle: one missed
send and it ran dry in three and a half hours with a hammer sitting in it.

Why the optimizer could not see it: :class:`VillageState` carried no production
figure at all, so the hub filter could only ask whether a village carried crop
and had merchants. Both are true of a sink.

The fixture mirrors the live shape rather than inventing one -- the hub is a net
RECEIVER of crop that is nonetheless made to forward, which is exactly how the
live hub qualified.
"""

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
    resolve_resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import VillageState, build_plan

GEOMETRY = MapGeometry(span=401, speed_fields_per_hour=12.0)
MODEL = EUROPE2_TEUTON

CAPITAL, MIDPOINT, ALTERNATIVE, STRANDED = 1, 2, 4, 3


def _account(midpoint_crop_per_hour: float | None, midpoint_alloc: Allocation):
    """A stranded village that cannot afford its own haul, and a midpoint.

    The midpoint sits halfway to the capital, so on distance alone it is the hub
    relay wants. ``ALTERNATIVE`` is solvent and only slightly further off-axis,
    so refusing the midpoint leaves the optimizer somewhere to go -- without it
    a passing test could mean no relay was possible at all, rather than that the
    sink was correctly refused.
    """
    villages = {
        CAPITAL: VillageState(
            CAPITAL, 0, 0, merchant_count=20, trade_office_level=15, crop_per_hour=0.0
        ),
        MIDPOINT: VillageState(
            MIDPOINT,
            60,
            0,
            merchant_count=20,
            trade_office_level=10,
            crop_per_hour=midpoint_crop_per_hour,
        ),
        ALTERNATIVE: VillageState(
            ALTERNATIVE,
            60,
            25,
            merchant_count=20,
            trade_office_level=10,
            crop_per_hour=1200.0,
        ),
        STRANDED: VillageState(
            STRANDED, 120, 0, merchant_count=6, trade_office_level=10, crop_per_hour=9000.0
        ),
    }
    # None stands in as zero for the allocation maths only. The allocation layer
    # has no opinion on whether a rate was readable, and conflating the two here
    # would hide which layer the guard actually lives in.
    midpoint_production = 0.0 if midpoint_crop_per_hour is None else midpoint_crop_per_hour
    plans = {
        Resource.CROP: resolve_resource(
            Resource.CROP,
            {
                CAPITAL: 0.0,
                MIDPOINT: midpoint_production,
                ALTERNATIVE: 1200.0,
                STRANDED: 9000.0,
            },
            {
                CAPITAL: Allocation(AllocationMode.REMAINDER),
                MIDPOINT: midpoint_alloc,
                ALTERNATIVE: Allocation(AllocationMode.ABSOLUTE, 0.0),
                STRANDED: Allocation(AllocationMode.ABSOLUTE, 0.0),
            },
        )
    }
    return villages, plans


def _relay_hubs(plan) -> set[int]:
    """The hubs the planner itself classified, not topology that resembles one.

    Two independent flows through one village are deliberately NOT a relay in
    this codebase, so asking the plan is the only honest question.
    """
    return {relay.hub for relay in plan.relays}


# A sink covers its deficit by receiving; SUSTAIN is how the model says that.
_SUSTAIN = Allocation(AllocationMode.SUSTAIN, 100.0)
# A village that receives while breaking even, so it sits inside the crop graph
# and is therefore eligible -- the only way to test the >= 0 boundary at all.
_RECEIVES_WHILE_BREAKING_EVEN = Allocation(AllocationMode.ABSOLUTE, 3000.0)


def test_the_midpoint_is_genuinely_the_hub_relay_wants() -> None:
    """Control. Without this, every assertion below could pass merely because
    the fixture produces no relay whatsoever."""
    villages, plans = _account(1200.0, Allocation(AllocationMode.ABSOLUTE, 0.0))

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT in _relay_hubs(plan), (
        "a solvent midpoint was not chosen as a relay hub, so this fixture "
        "cannot show anything about refusing it when it is a sink"
    )


@pytest.mark.parametrize(
    "sink_rate",
    [-3037.0, -874.0, -1.0],
    ids=["live_hammer_village", "live_second_hub", "barely_negative"],
)
def test_a_crop_negative_village_is_never_a_relay_hub(sink_rate: float) -> None:
    villages, plans = _account(sink_rate, _SUSTAIN)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT not in _relay_hubs(plan), (
        f"village {MIDPOINT} produces {sink_rate:+.0f} crop/h and was still made "
        f"to forward crop. When its inbound leg slips it funds the relay from a "
        f"falling granary, which is how a village with troops in it dies."
    )


def test_an_unreadable_rate_is_not_treated_as_safe() -> None:
    """Unknown is not zero. UNREADABLE_RATE is CRITICAL in this codebase
    precisely because guessing is the dangerous act, and a relay hub is the last
    place to take the optimistic reading."""
    villages, plans = _account(None, _RECEIVES_WHILE_BREAKING_EVEN)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT not in _relay_hubs(plan), (
        f"village {MIDPOINT} has an unreadable crop rate and was still made a "
        f"relay hub -- an unknown rate was read as a safe one"
    )


def test_a_break_even_village_may_still_relay() -> None:
    """The threshold is >= 0, and the boundary is not pedantic.

    Relaying cannot drain a village that breaks even: a late refill makes it
    forward less, nothing worse. Excluding break-even villages would rule out
    the canonical midway hub and buy no safety at all.
    """
    villages, plans = _account(0.0, _RECEIVES_WHILE_BREAKING_EVEN)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT in _relay_hubs(plan), (
        "a break-even village was refused as a hub; the guard is stricter than "
        "the danger it exists to prevent"
    )
