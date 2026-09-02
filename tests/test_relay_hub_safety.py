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

import dataclasses

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
    relay wants. ``ALTERNATIVE`` is solvent and further off-axis, so refusing the
    midpoint leaves the optimizer somewhere to go -- without it a passing test
    could mean no relay was possible at all, rather than that the sink was
    correctly refused.

    ``ALTERNATIVE``'s offset is load-bearing in BOTH directions, so do not treat
    it as a free parameter:

    * Too near the axis and the hub choice is a coin-flip. At ``y=25`` the two
      candidates sat 60 and 65 fields from ``STRANDED``, close enough that the
      winner was decided by the objective's tie-break rather than by geometry --
      so the break-even case picked ``MIDPOINT`` at merchant base 2,200 and
      ``ALTERNATIVE`` at 2,500, for no reason the test was interested in.
    * Too far and the refusal cases go vacuous. At ``y=60`` the ``-1`` case
      produced no relay whatsoever, which would have turned "the sink was
      refused" into "nothing was attempted" while still passing.

    ``y=40`` is inside both bounds: the midpoint wins on geometry when it is
    eligible, and ``ALTERNATIVE`` still gets chosen whenever it is refused.
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
            40,
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


# A village whose production and target agree, so it neither sends nor receives
# crop and sits OUTSIDE the crop flow graph. The canonical midway hub of profile
# section 8.5 looks exactly like this: it grows what it keeps and ships nothing.
_OUTSIDE_THE_CROP_GRAPH = Allocation(AllocationMode.ABSOLUTE, 0.0)


def test_a_crop_neutral_village_outside_the_crop_graph_may_relay() -> None:
    """Hub candidates come from the whole account, not from the crop graph.

    Until 2026-09-02 the hub search drew its candidates from the villages that
    already carried crop, so the canonical midway hub -- no crop flow of its own
    -- could never be chosen, while the same village with any flow at all was
    found at once (the audit's known-defect pair). Solvency and merchants are the
    guards that matter; carrying crop was never one of them.
    """
    villages, plans = _account(0.0, _OUTSIDE_THE_CROP_GRAPH)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT in _relay_hubs(plan), (
        f"village {MIDPOINT} breaks even, has 20 merchants and sits halfway to the "
        f"capital, yet was not made the hub because it carries no crop of its own"
    )


# A village the operator never gave a crop instruction. `_plan_account` drops
# explicit KEEP entries because they mean exactly what an absent entry means, so
# this is what a freshly settled village looks like to the planner.
_NOT_IN_THE_CROP_PLAN = Allocation(AllocationMode.KEEP)


def test_a_village_with_no_crop_allocation_is_not_conscripted_as_a_hub() -> None:
    """Widening the candidates to the whole account went one village too far.

    The midway hub of section 8.5 has an allocation that nets to zero flow; a
    village nobody has said anything about has no allocation at all, and the
    two are different things. Conscripting the second means the operator finds
    27,000/h of someone else's crop routed into a granary they never sized for
    it, on a village they may have founded an hour ago -- and every crop route
    in the account rewritten to make it happen.
    """
    villages, plans = _account(0.0, _NOT_IN_THE_CROP_PLAN)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT not in _relay_hubs(plan), (
        f"village {MIDPOINT} carries no crop instruction of its own and was still "
        f"made infrastructure because a relay through it priced slightly cheaper"
    )
    assert _relay_hubs(plan), (
        "no relay at all -- the case is vacuous unless the search still had "
        "somewhere legitimate to go"
    )


def test_settling_a_village_does_not_rewrite_the_crop_routes() -> None:
    """Known issue #10, which the widening deleted the answer to.

    A re-plan on unchanged input must produce an identical route set or the diff
    against the live configuration is meaningless -- and /execute acts on that
    diff, deleting and recreating real Gold Club rows. Adding ONE village with
    no crop allocation moved every leg and changed the hub, so a settle the
    operator had not finished thinking about rewrote the whole sheet. It cannot
    now: a village with no crop instruction is not a candidate, so the account
    it joins plans exactly as it did before.
    """
    # MIDPOINT is insolvent here, so the relay legitimately runs through the
    # off-axis ALTERNATIVE and the axis is free for the newcomer to look better
    # on. Without that the incumbent hub wins on distance anyway and the test
    # would pass while demonstrating nothing.
    villages, plans = _account(-1.0, _NOT_IN_THE_CROP_PLAN)
    before = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)
    assert _relay_hubs(before) == {ALTERNATIVE}, _relay_hubs(before)

    # Squarely on the haul the relay is trying to shorten, solvent, with
    # merchants to spare: everything the widened candidate set asked of a hub,
    # and nothing the operator ever said about crop.
    newcomer = 99
    villages[newcomer] = VillageState(
        newcomer, 58, 0, merchant_count=20, trade_office_level=10, crop_per_hour=0.0
    )
    plans_after = {
        Resource.CROP: resolve_resource(
            Resource.CROP,
            {
                CAPITAL: 0.0,
                MIDPOINT: -1.0,
                ALTERNATIVE: 1200.0,
                STRANDED: 9000.0,
                newcomer: 0.0,
            },
            {
                CAPITAL: Allocation(AllocationMode.REMAINDER),
                ALTERNATIVE: Allocation(AllocationMode.ABSOLUTE, 0.0),
                STRANDED: Allocation(AllocationMode.ABSOLUTE, 0.0),
            },
        )
    }
    after = build_plan(villages, plans_after, GEOMETRY, MODEL, max_latency_hours=None)

    def _crop_legs(plan):
        return sorted(
            (route.origin, route.destination, round(route.cargo_per_hour[Resource.CROP], 6))
            for route in plan.routes
            if route.cargo_per_hour.get(Resource.CROP, 0.0)
        )

    assert newcomer not in _relay_hubs(after)
    assert _crop_legs(after) == _crop_legs(before), (
        "settling one village rewrote the crop route set; the /execute diff would "
        "delete and recreate rows for villages the operator never touched"
    )


def test_an_unreadable_rate_outside_the_crop_graph_is_still_refused() -> None:
    """Widening the candidates must not widen past the solvency guard."""
    villages, plans = _account(None, _OUTSIDE_THE_CROP_GRAPH)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT not in _relay_hubs(plan), (
        f"village {MIDPOINT} has an unreadable crop rate and was still made a "
        f"relay hub once villages outside the crop graph became candidates"
    )


def test_a_village_with_no_merchants_is_never_a_relay_hub() -> None:
    """A hub staffs the onward leg itself; a village with no merchants cannot."""
    villages, plans = _account(0.0, _OUTSIDE_THE_CROP_GRAPH)
    villages[MIDPOINT] = dataclasses.replace(villages[MIDPOINT], merchant_count=0)

    plan = build_plan(villages, plans, GEOMETRY, MODEL, max_latency_hours=None)

    assert MIDPOINT not in _relay_hubs(plan), (
        f"village {MIDPOINT} has no merchants and was still made a relay hub -- "
        f"the plan carries a leg nobody can send"
    )
