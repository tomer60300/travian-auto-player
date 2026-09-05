"""Section 7: NPC balancing.

The previous build of stock-funded supply was wrong in three ways that three
independent reviewers converged on, and it was replaced rather than patched.
These tests pin all three so none can come back:

* a **level** modelled as a **rate** (``capacity x fraction / window_hours``,
  so a shorter window RAISED the claim);
* supply that was **compulsory** -- an addend to ``available`` that every
  non-KEEP mode shipped away, so a floor on a quiet village cost the account
  the whole allowance;
* an **infinite reservoir** in the replay (``max(floor, ...)``), which funded
  every departure however often it was asked.
"""

import asyncio
import dataclasses

import pytest
from fastapi import HTTPException

from travian_api.services.distribution.allocation import (
    MATERIALS,
    Allocation,
    AllocationError,
    AllocationMode,
    Resource,
    ResourcePlan,
    VillageAllocation,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.npc import (
    MIN_FUNDED_ALLOWANCE_PER_DAY,
    NpcPolicy,
    NpcReserve,
    TriggerKind,
    _projected,
    derive_reserves,
    draw_allowance,
    evaluate_triggers,
)
from travian_api.services.distribution.optimizer import VillageState
from travian_api.services.distribution.planner import (
    DistributionPlan,
    PlannerConfig,
    craft_plan,
)
from travian_api.web.routes import distribution as dist
from travian_api.web.routes.distribution import PlanRequest, post_plan

HUB, NEAR, FAR = 20002, 20011, 20003

DAY_16H = [7 * 60, 23 * 60]
DAY_8H = [7 * 60, 15 * 60]


def _village(vid, name, x, y, *, lumber=0.0, crop=0.0, merchants=20, **overrides):
    base = {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": merchants,
        "merchants_free": merchants,
        "lumber_per_hour": lumber,
        "clay_per_hour": 0,
        "iron_per_hour": 0,
        "crop_per_hour": crop,
    }
    base.update(overrides)
    return base


def _payload(*, window, floor=0.30, capacity=1_200_000, hub_crop=20_000.0, feedstock=None, **hub):
    """02 makes 6,000/h of lumber against 18,000/h claimed: short by 12,000/h.

    The shortfall is a RATE and so is the feedstock that covers it -- 02 retains
    20,000/h of crop, which is what it can convert. Neither figure mentions the
    warehouse capacity or the length of the window, which is the whole point.
    """
    snapshot = [
        _village(
            HUB,
            "02",
            0,
            0,
            lumber=6000,
            crop=hub_crop,
            merchants=200,
            warehouse_capacity=capacity,
            **hub,
        ),
        _village(NEAR, "11", -5, 0),
        _village(FAR, "03", 5, 0),
    ]
    config = {"village_id": HUB, "stock_floor_fraction": floor}
    if feedstock is not None:
        config["npc_feedstock"] = feedstock
    return {
        "snapshot": snapshot,
        "allocations": {
            "lumber": {
                str(HUB): {"mode": "remainder"},
                str(NEAR): {"mode": "absolute", "value": 10_000},
                str(FAR): {"mode": "absolute", "value": 8_000},
            }
        },
        "config": [config],
        "dispatch_window": window,
        "prune_to_window": True,
        "npc_attended": True,
    }


def _plan(**kw):
    return asyncio.run(post_plan(PlanRequest.model_validate(_payload(**kw))))


def _lumber(res):
    return next(u for u in res.unallocated if u.resource is Resource.LUMBER)


def _findings(res, category):
    return [
        f for group in res.diagnostics.groups if group.category == category for f in group.findings
    ]


class TestALevelIsNotARate:
    """The original bug, pinned. Halving the window must not move the claim.

    ``stock_floor_fraction`` is a BUFFER LEVEL. The old code turned it into a
    rate by dividing by the window -- 0.30 x 1,200,000 / 16h = 22,500/h over a
    day profile and 45,000/h over an 8-hour one, off the same warehouse. The
    NPC allowance is derived from RATES instead (what the village retains of
    the resources it is not drawing on), so no window length can touch it.
    """

    def test_halving_the_window_does_not_change_the_claim(self):
        long_day = _lumber(_plan(window=DAY_16H))
        short_day = _lumber(_plan(window=DAY_8H))

        assert long_day.unallocated == pytest.approx(short_day.unallocated)

    def test_the_capacity_does_not_change_the_claim_either(self):
        """A level is scaled by capacity; a rate is not. Ten times the
        warehouse must fund exactly the same conversion."""
        small = _lumber(_plan(window=DAY_16H, capacity=120_000))
        large = _lumber(_plan(window=DAY_16H, capacity=1_200_000))

        assert small.unallocated == pytest.approx(large.unallocated)

    def test_the_draw_is_the_shortfall_and_nothing_more(self):
        """12,000/h short, 20,000/h of feedstock: the draw is 12,000/h, and the
        remainder village lands on exactly zero rather than on a surplus it
        was handed by its own warehouse."""
        lumber = _lumber(_plan(window=DAY_16H))

        assert lumber.total_npc_draw == pytest.approx(12_000)
        assert lumber.total_npc_allowance == pytest.approx(20_000)
        assert lumber.unallocated == pytest.approx(0.0)


class TestNpcAttendanceIsExplicit:
    """The operator sleeps through the night window.

    A guessed default would fund night routes from trading nobody is doing, so
    attendance is stated per segment and refused when a floor is set and the
    request carries windows.
    """

    def test_a_floor_with_a_window_and_no_attendance_is_a_422(self):
        payload = _payload(window=DAY_16H)
        del payload["npc_attended"]

        with pytest.raises(Exception) as exc:
            PlanRequest.model_validate(payload)

        assert "npc_attended" in str(exc.value)

    def test_an_unattended_window_has_no_allowance_at_all(self):
        payload = _payload(window=DAY_16H)
        payload["npc_attended"] = False

        res = asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        lumber = _lumber(res)
        assert lumber.total_npc_allowance == 0.0
        assert lumber.total_npc_draw == 0.0

    def test_round_the_clock_is_unattended_unless_the_operator_says_otherwise(self):
        """The bypass, and the direction of the default that closes it.

        A request with no window used to skip the guard entirely and then
        default to ATTENDED, justified as "round the clock has no night hours
        to mis-fund". Backwards: such a set has all 24 hours, including the
        eight nobody is at the Marketplace, and Travian offers nothing to
        confine a repeat interval to part of the day -- which is exactly what
        WINDOW_NOT_ENFORCEABLE and window_pruning exist to say.

        Nothing downstream caught it. `simulate_day` tops the store up at every
        departure minute including 03:00, and NPC_CAPACITY_SHORT is measured
        against that same optimistic cap, so the default WAS the guard.

        It is not a 422: a setup DOCUMENT is validated through this model and
        carries no window at all -- windows live on its profiles -- so
        demanding an answer here would refuse to save a good setup. Unattended
        under-delivers and reports it; attended over-commits in silence.
        """
        payload = _payload(window=None)
        del payload["npc_attended"]

        # Accepted, unlike the windowed case above.
        res = asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        lumber = _lumber(res)
        assert lumber.total_npc_allowance == 0.0, (
            "a round-the-clock set must not be credited conversion nobody performed"
        )
        assert lumber.total_npc_draw == 0.0

    def test_round_the_clock_still_takes_the_operator_at_their_word(self):
        payload = _payload(window=None)
        payload["npc_attended"] = True

        res = asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        assert _lumber(res).total_npc_allowance > 0.0


class TestZeroIsNoneAtEveryLayer:
    """`0.0` is not a floor, at the schema, the policy and the solve alike."""

    def test_a_zero_floor_needs_no_attendance_declaration(self):
        """Nothing about NPC applies, the attendance requirement included."""
        payload = _payload(window=DAY_16H, floor=0.0)
        del payload["npc_attended"]

        res = asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        assert _lumber(res).total_npc_allowance == 0.0

    def test_a_zero_fraction_declares_no_reserve(self):
        res = asyncio.run(post_plan(PlanRequest.model_validate(_payload(window=None, floor=0.0))))

        assert res.npc_reserves == []
        assert res.npc_triggers == []
        assert _lumber(res).total_npc_allowance == 0.0

    def test_a_zero_fraction_needs_no_capacity_reading(self):
        """A floor of 0.0 has no level to work out, so the 422 that a real floor
        would raise against a missing warehouse capacity must not fire."""
        payload = _payload(window=DAY_16H, floor=0.0, capacity=None)
        del payload["npc_attended"]

        res = asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        assert res.npc_reserves == []

    def test_an_empty_policy_declares_nothing(self):
        # RE-SEEDED: `attended` lost its `= True` default, so both constructions
        # now state it. What is asserted is unchanged and is not about
        # attendance -- `is_declared` reads `floor_level` and nothing else, so
        # the same two cases (no floors, one floor) still answer False and True,
        # and the value passed here cannot move either.
        assert NpcPolicy(attended=False).is_declared is False
        assert NpcPolicy(attended=False, floor_level={HUB: 1.0}).is_declared is True


class TestNpcCapacityShort:
    def test_a_draw_beyond_the_feedstock_fails_loudly(self):
        """5,000/h of crop retained against 12,000/h of wood to fund: 7,000/h
        short. The spec's rule is fail loudly, not silently degrade."""
        res = _plan(window=DAY_16H, hub_crop=5_000.0)

        short = _findings(res, "npc_capacity_short")
        assert len(short) == 1, res.warnings
        assert short[0].severity == "critical"
        assert "7,000" in short[0].message
        assert res.feasible is False

    def test_it_is_weighed_rather_than_left_to_the_operator(self):
        """In `_WEIGHED_CRITICALS`, so the verdict reports it as a BLOCKER and
        not as an unweighed critical the operator has to notice."""
        res = _plan(window=DAY_16H, hub_crop=5_000.0)

        assert res.verdict.executable is False
        assert res.verdict.clean is False
        assert any("7,000/h short" in b for b in res.verdict.blockers), res.verdict.blockers
        assert "npc_capacity_short" not in res.verdict.unweighed
        assert any("NPC conversion" in c for c in res.verdict.covers), res.verdict.covers

    def test_an_unreadable_feedstock_rate_funds_nothing_and_says_so(self):
        """Crop is the one nullable rate in the snapshot and the account's
        largest feedstock. Unreadable, the allowance is zero rather than sized
        off the readable half."""
        res = _plan(window=DAY_16H, hub_crop=None)

        unreadable = [f for f in _findings(res, "unreadable_rate") if f.village == "02"]
        assert unreadable, res.warnings
        assert "NPC feedstock cannot be sized" in unreadable[0].message
        assert next(r for r in res.npc_reserves if r.village_id == HUB).allowance_per_day == 0.0
        assert _findings(res, "npc_capacity_short"), res.warnings


class TestTheFeedstockOverride:
    def test_an_explicit_source_replaces_the_derivation(self):
        """Named clay, 02 retains 0/h of clay, so the conversion funds nothing
        -- even though the crop it was not allowed to use is sitting there."""
        res = _plan(window=DAY_16H, feedstock=["clay"])

        reserve = next(r for r in res.npc_reserves if r.village_id == HUB)
        assert reserve.allowance_per_day == 0.0
        assert reserve.feedstock == []
        assert _findings(res, "npc_capacity_short"), res.warnings

    def test_naming_the_resource_being_drawn_is_refused(self):
        """NPC exchanges one resource for another; converting wood into wood is
        not a thing the game offers."""
        with pytest.raises(HTTPException) as exc:
            _plan(window=DAY_16H, feedstock=["lumber"])

        assert exc.value.status_code == 400
        assert "lumber" in exc.value.detail
        assert "cannot convert a resource into itself" in exc.value.detail


def _reserve(**kw):
    """A funded reserve: 24,000/day, so 1,000/h, paid for out of crop."""
    base = dict(
        village_id=HUB,
        floor_level=360_000.0,
        allowance_per_day=24_000.0,
        sources=(Resource.CROP,),
        shares=(1.0,),
    )
    base.update(kw)
    return NpcReserve(**base)


class TestOneBudgetIsSplitAcrossTheMaterialsThatNeedIt:
    """`draw_allowance` apportions ONE reserve across every material.

    Every existing case is short on exactly one material, so the divisor never
    divides and dropping `* needs[resource] / total` changed nothing anywhere in
    the suite. As the real code that hands the whole budget to lumber AND the
    whole budget to clay AND the whole budget to iron -- up to three times the
    conversion the feedstock can pay for -- and the plan reports routes as
    funded that arrive short.
    """

    def _draw(self, **needs):
        retention = {resource: {HUB: -amount} for resource, amount in needs.items()}
        return draw_allowance({HUB: _reserve()}, retention)

    def test_the_parts_sum_to_the_whole_budget(self):
        draw = self._draw(**{Resource.LUMBER: 3_000.0, Resource.CLAY: 1_000.0})

        assert draw[Resource.LUMBER][HUB] + draw[Resource.CLAY][HUB] == pytest.approx(1_000.0)

    def test_the_split_is_proportional_to_need(self):
        # 3:1 of need is 3:1 of the budget.
        draw = self._draw(**{Resource.LUMBER: 3_000.0, Resource.CLAY: 1_000.0})

        assert draw[Resource.LUMBER][HUB] == pytest.approx(750.0)
        assert draw[Resource.CLAY][HUB] == pytest.approx(250.0)

    def test_a_material_that_needs_nothing_gets_nothing(self):
        draw = self._draw(**{Resource.LUMBER: 3_000.0, Resource.CLAY: 1_000.0})

        assert draw[Resource.IRON][HUB] == 0.0

    def test_one_short_material_still_gets_the_whole_budget(self):
        # The control, and the shape every previous case had.
        draw = self._draw(**{Resource.LUMBER: 3_000.0})

        assert draw[Resource.LUMBER][HUB] == pytest.approx(1_000.0)


class TestAFloorOnAQuietVillageCostsNothing:
    """`_need` reads a NEGATIVE retention as demand and nothing else.

    "This one line is why a floor on a quiet village costs nothing", says the
    module. Read as `abs(retention)`, a village KEEPING 5,000/h of every
    material claims 5,000/h of conversion it does not need -- and
    `derive_reserves` then marks it as DRAWING on that material, which removes
    the material from its own feedstock set.
    """

    def _retention(self, amount):
        return {resource: {HUB: amount} for resource in MATERIALS}

    def test_a_village_keeping_what_it_makes_draws_nothing(self):
        draw = draw_allowance({HUB: _reserve()}, self._retention(5_000.0))

        assert [draw[resource][HUB] for resource in MATERIALS] == [0.0, 0.0, 0.0]

    def test_and_is_not_recorded_as_drawing_on_anything(self):
        reserves, findings = derive_reserves(
            NpcPolicy(floor_level={HUB: 360_000.0}, attended=True),
            self._retention(5_000.0),
        )

        assert reserves[HUB].drawn == frozenset()
        assert findings == ()

    def test_a_village_shipping_beyond_production_still_draws(self):
        # The control: a negative retention IS demand.
        draw = draw_allowance({HUB: _reserve()}, self._retention(-5_000.0))

        assert all(draw[resource][HUB] > 0 for resource in MATERIALS)


class TestTheReserveRefusesWhatNpcCannotDo:
    """Two rules stated in four docstrings across two modules and asserted
    nowhere: both guards could be deleted with the whole suite still green."""

    def test_crop_can_never_be_drawn(self):
        # A granary is not NPC-fed. Drawn crop would take the account's largest
        # feedstock out of its own sources and fund the conversion from itself.
        with pytest.raises(AllocationError) as caught:
            NpcReserve(
                village_id=HUB,
                floor_level=360_000.0,
                allowance_per_day=0.0,
                drawn=frozenset({Resource.CROP}),
            )

        assert "granary" in str(caught.value)

    def test_an_allowance_with_no_feedstock_is_refused(self):
        # NPC is an exchange: it cannot create resources.
        with pytest.raises(AllocationError) as caught:
            NpcReserve(
                village_id=HUB,
                floor_level=360_000.0,
                allowance_per_day=50_000.0,
                sources=(),
                shares=(),
            )

        assert "cannot create resources" in str(caught.value)

    def test_float_residue_below_the_funded_minimum_is_still_allowed(self):
        # The paired case that keeps the guard from being "no sources, ever":
        # below one resource a DAY the allowance is rounding, not a budget.
        reserve = NpcReserve(
            village_id=HUB,
            floor_level=360_000.0,
            allowance_per_day=MIN_FUNDED_ALLOWANCE_PER_DAY / 2,
            sources=(),
            shares=(),
        )

        assert reserve.sources == ()


class TestAProjectedStoreNeverGoesBelowEmpty:
    """`_projected` clamps at zero the way the game does.

    The figure it returns is what the wood-low trigger and the day-check status
    both PRINT, so a store the plan drains past empty was reported at a negative
    level.
    """

    def test_a_store_the_plan_empties_reads_as_empty(self):
        assert _projected(10_000.0, -10_000.0, None) == 0.0

    def test_a_store_the_plan_drains_far_past_empty_still_reads_as_empty(self):
        assert _projected(10_000.0, -100_000.0, None) == 0.0

    def test_the_capacity_clamp_still_applies_at_the_top(self):
        assert _projected(10_000.0, 10_000.0, 50_000.0) == 50_000.0


class TestWoodLowReadsTheLowerOfNowAndProjected:
    """The trigger exists for the store the plan is about to empty.

    Its crop mirror IS covered (`crop_banked` reads the HIGHER of the two and a
    test proves it); wood was not, so reading `max` instead of `min` silenced
    exactly the case section 7's first trigger is for -- a warehouse above its
    floor now and below it by 07:00.
    """

    def _triggers(self, *, now, net):
        return evaluate_triggers(
            reserves={HUB: _reserve(allowance_per_day=0.0, sources=(), shares=())},
            stocks={HUB: {Resource.LUMBER: now}},
            capacities={HUB: {Resource.LUMBER: 1_200_000.0}},
            net_per_hour={HUB: {Resource.LUMBER: net}},
        )

    def test_a_store_above_its_floor_now_and_below_it_by_morning_fires(self):
        # 400,000 now, draining 5,000/h: 280,000 after a day, under the 360,000
        # floor. "Now" alone says nothing is wrong.
        fired = self._triggers(now=400_000.0, net=-5_000.0)

        assert [t.kind for t in fired] == [TriggerKind.WOOD_LOW]
        assert fired[0].projected is True
        assert fired[0].level == pytest.approx(280_000.0)

    def test_a_store_that_stays_above_its_floor_says_nothing(self):
        assert self._triggers(now=400_000.0, net=0.0) == ()


class TestTheTwoTriggers:
    """Section 7's triggers: wood is low, OR crop exceeds 700,000.

    Reports about when the OPERATOR should trade. The planner never presses the
    button, so neither of these changes a single route.
    """

    def test_wood_at_its_floor_is_low(self):
        """The reading chosen for "wood is low", which the spec gives no number
        for: the village's own declared floor. 30% of 1,200,000 is 360,000, and
        a store sitting exactly on its buffer has nothing spare."""
        res = _plan(window=DAY_16H, lumber_stock=360_000)

        low = _findings(res, "npc_wood_low")
        assert len(low) == 1, res.warnings
        assert low[0].severity == "warning"
        assert "360,000" in low[0].message
        row = next(t for t in res.npc_triggers if t.kind == "wood_low")
        assert (row.village_id, row.level, row.threshold) == (HUB, 360_000, 360_000)

    def test_wood_above_its_floor_is_not_low(self):
        res = _plan(window=DAY_16H, lumber_stock=360_001)

        assert _findings(res, "npc_wood_low") == []
        assert [t for t in res.npc_triggers if t.kind == "wood_low"] == []

    def test_a_village_with_no_floor_has_no_wood_reading_at_all(self):
        """The number is the account's, so a village that never stated one is
        not guessed at."""
        res = asyncio.run(post_plan(PlanRequest.model_validate(_payload(window=None, floor=0.0))))

        assert _findings(res, "npc_wood_low") == []

    def test_crop_at_exactly_the_trigger_has_not_exceeded_it(self):
        """ "Exceeds 700,000" is strict, so the boundary itself does not fire.
        The granary is set to its own stock so the projection cannot move it and
        this is a clean test of the level."""
        res = _plan(
            window=DAY_16H,
            crop_stock=700_000,
            granary_capacity=700_000,
            lumber_stock=500_000,
        )

        assert _findings(res, "npc_crop_banked") == [], res.warnings

    def test_one_unit_past_the_trigger_fires(self):
        res = _plan(
            window=DAY_16H,
            crop_stock=700_001,
            granary_capacity=700_001,
            lumber_stock=500_000,
        )

        banked = _findings(res, "npc_crop_banked")
        assert len(banked) == 1, res.warnings
        assert banked[0].severity == "note"
        assert "700,001" in banked[0].message
        row = next(t for t in res.npc_triggers if t.kind == "crop_banked")
        assert (row.level, row.threshold, row.projected) == (700_001, 700_000, False)

    def test_crop_the_plan_banks_past_the_trigger_fires_as_projected(self):
        """Read off the emitted plan, not the snapshot: 02 retains 50,000/h and
        converts 12,000/h of it into wood, so the granary banks 38,000/h --
        912,000 over a day, which is past the trigger even from empty."""
        res = _plan(
            window=DAY_16H,
            hub_crop=50_000.0,
            granary_capacity=2_000_000,
            lumber_stock=500_000,
        )

        row = next(t for t in res.npc_triggers if t.kind == "crop_banked")
        assert row.projected is True
        assert row.level == pytest.approx(24 * (50_000 - 12_000))

    def test_the_conversion_debit_keeps_the_projection_honest(self):
        """The same account with the conversion NOT drawn on banks the whole
        50,000/h. The 12,000/h difference is the feedstock debit, and without it
        the trigger would report crop the operator has already spent."""
        drawing = _plan(
            window=DAY_16H,
            hub_crop=50_000.0,
            granary_capacity=2_000_000,
            lumber_stock=500_000,
        )
        # Claims below 02's own 6,000/h production: nothing is short, so nothing
        # is converted and the granary keeps every unit.
        quiet_payload = _payload(
            window=DAY_16H,
            hub_crop=50_000.0,
            granary_capacity=2_000_000,
            lumber_stock=500_000,
        )
        quiet_payload["allocations"]["lumber"][str(NEAR)] = {"mode": "absolute", "value": 1_000}
        quiet_payload["allocations"]["lumber"][str(FAR)] = {"mode": "absolute", "value": 2_000}
        quiet = asyncio.run(post_plan(PlanRequest.model_validate(quiet_payload)))

        drawn_row = next(t for t in drawing.npc_triggers if t.kind == "crop_banked")
        quiet_row = next(t for t in quiet.npc_triggers if t.kind == "crop_banked")
        assert quiet_row.level - drawn_row.level == pytest.approx(24 * 12_000)


# ── Both store checks read the same stores ───────────────────────────────────


def _floored_plan():
    """The account above with real stores, so both store checks have one to read.

    02 draws 12,000/h of lumber, funded out of the 20,000/h of crop it retains
    -- the one resource it produces and is not drawing on, so it pays for the
    whole conversion.
    """
    body = PlanRequest.model_validate(
        _payload(
            window=DAY_16H,
            granary_capacity=800_000,
            crop_stock=100_000,
            lumber_stock=100_000,
        )
    )
    return body, asyncio.run(dist._plan_account(body)).plan


def _continuous_net_rates(body, plan):
    """The net rate ``_storage_findings`` hands the continuous store check.

    Recorded off ``store_status`` itself rather than recomputed here: the figure
    under test is the one that function is actually given, and a test that
    rebuilt it would only be checking its own arithmetic.
    """
    real = dist.store_status
    seen: dict[int, dict[Resource, float]] = {}

    def recording(village_id, resource, stock, capacity, net_per_hour, *args, **kw):
        seen.setdefault(village_id, {})[resource] = net_per_hour
        return real(village_id, resource, stock, capacity, net_per_hour, *args, **kw)

    dist.store_status = recording
    try:
        dist._storage_findings(body, plan, body.dispatch_window)
    finally:
        dist.store_status = real
    return seen


class TestBothStoreChecksNetTheConversion:
    """The NPC trade moves resources INSIDE one village, so none of it is cargo.

    `_npc_store_state` nets it and says why; `_storage_findings`, forty lines
    below, built its rate from own production plus route cargo alone. So the
    continuous check read 02's warehouse as draining at exactly the rate the
    conversion fills it, on a store the plan leaves level, and read its granary
    as banking every unit of crop while 12,000/h of it is traded away.
    """

    def test_the_drawn_store_is_credited_the_draw(self):
        body, plan = _floored_plan()
        _, _, trigger = dist._npc_store_state(body, plan)

        continuous = _continuous_net_rates(body, plan)

        # own 6,000 + drawn 12,000 - shipped 18,000 = 0: the store is level.
        # Without the draw it reads as -12,000/h, which is -draw exactly.
        assert trigger[HUB][Resource.LUMBER] == pytest.approx(0.0)
        assert continuous[HUB][Resource.LUMBER] == pytest.approx(0.0)

    def test_the_feedstock_store_is_debited_what_the_conversion_spent(self):
        body, plan = _floored_plan()
        _, _, trigger = dist._npc_store_state(body, plan)

        continuous = _continuous_net_rates(body, plan)

        # 20,000/h retained, 12,000/h of it converted into wood: +8,000/h. Crop
        # is the only feedstock, so its share of every conversion is 1.0.
        assert trigger[HUB][Resource.CROP] == pytest.approx(8_000.0)
        assert continuous[HUB][Resource.CROP] == pytest.approx(8_000.0)

    def test_the_two_checks_agree_on_every_store_of_a_floored_village(self):
        body, plan = _floored_plan()
        _, _, trigger = dist._npc_store_state(body, plan)

        continuous = _continuous_net_rates(body, plan)

        for resource in Resource:
            assert continuous[HUB][resource] == pytest.approx(trigger[HUB][resource]), resource


class TestOnlyAMaterialDrawIsAConversion:
    """`_npc_store_deltas` charges the feedstock stores for what was converted,
    and only a MATERIAL draw is a conversion -- crop is never drawn, because a
    granary is not NPC-fed.

    The guard cannot be reached through `_plan_account`: `NpcReserve` refuses
    `drawn={CROP}` outright, so no real plan carries a crop draw. That makes it
    a guard whose invariant lives in another module, and pinning it against the
    plan builder would only be re-testing the refusal. Built by hand instead,
    which is the only way to ask this function the question: if a crop draw ever
    did reach here, the village's own iron and crop stores must not be debited
    for a trade nobody made.
    """

    CROP_DRAW = 1_000.0

    def _plan(self):
        def allocation_for(resource: Resource, draw: float) -> ResourcePlan:
            return ResourcePlan(
                resource=resource,
                total_production=0.0,
                total_npc_allowance=0.0,
                total_npc_draw=draw,
                villages=(
                    VillageAllocation(
                        village_id=HUB,
                        mode=AllocationMode.ABSOLUTE,
                        own_per_hour=0.0,
                        target_per_hour=0.0,
                        npc_draw_per_hour=draw,
                    ),
                ),
                remainder_village_id=None,
                unallocated=0.0,
                findings=(),
            )

        return DistributionPlan(
            npc={
                HUB: NpcReserve(
                    village_id=HUB,
                    floor_level=40_000.0,
                    allowance_per_day=24_000.0,
                    sources=(Resource.CROP, Resource.IRON),
                    shares=(0.5, 0.5),
                    drawn=frozenset({Resource.LUMBER}),
                )
            },
            resource_plans={
                resource: allocation_for(
                    resource, self.CROP_DRAW if resource is Resource.CROP else 0.0
                )
                for resource in Resource
            },
        )

    def test_the_granary_is_credited_the_draw_in_full(self):
        deltas = dist._npc_store_deltas(self._plan())

        assert deltas[HUB][Resource.CROP] == pytest.approx(self.CROP_DRAW), (
            "a crop draw is a credit, never netted against a conversion it did not pay for"
        )

    def test_no_feedstock_store_pays_for_it(self):
        deltas = dist._npc_store_deltas(self._plan())

        for resource in MATERIALS:
            assert deltas[HUB][resource] == pytest.approx(0.0), resource


class TestAttendanceCannotBeDefaulted:
    """The docstring says it: "It is REQUIRED of the caller rather than
    guessed: the account sleeps through the night window, and a default of
    'attended' would fund night routes from trading nobody is doing."

    The dataclass supplied exactly that guess. Unreachable in the dangerous
    direction today -- the one defaulted construction sat in `craft_plan`, where
    `floor_level` is empty so `attended` is never read -- but a loaded gun for
    the next library caller, and the direction it points is the one section 7's
    whole guard is about: unattended under-delivers and reports it, attended
    over-commits in silence.
    """

    def test_a_policy_cannot_be_built_without_saying(self):
        with pytest.raises(TypeError):
            NpcPolicy(floor_level={HUB: 1.0})

    def test_the_field_carries_no_default_at_all(self):
        (attended,) = [f for f in dataclasses.fields(NpcPolicy) if f.name == "attended"]

        assert attended.default is dataclasses.MISSING
        assert attended.default_factory is dataclasses.MISSING

    def test_the_planner_no_longer_builds_a_guessed_one(self):
        """`policy = npc or NpcPolicy()` in `craft_plan` was the only defaulted
        construction. A library caller declaring no policy at all must still
        get a plan -- through the single-pass solve, not through an invented
        declaration that now cannot even be built."""
        villages = {
            HUB: VillageState(village_id=HUB, x=0, y=0, merchant_count=20, trade_office_level=0),
            NEAR: VillageState(village_id=NEAR, x=5, y=0, merchant_count=20, trade_office_level=0),
        }
        plan = craft_plan(
            villages,
            {Resource.LUMBER: {HUB: 4_000.0, NEAR: 0.0}},
            {
                Resource.LUMBER: {
                    HUB: Allocation(AllocationMode.ABSOLUTE, 0.0),
                    NEAR: Allocation(AllocationMode.REMAINDER),
                }
            },
            PlannerConfig(
                geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
                merchant_model=EUROPE2_TEUTON,
            ),
        )

        assert plan.npc == {}
        assert plan.rows, "the plan still routes 4,000/h of lumber to 11"
