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

import pytest
from fastapi import HTTPException

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.npc import NpcPolicy
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
        assert NpcPolicy().is_declared is False
        assert NpcPolicy(floor_level={HUB: 1.0}).is_declared is True


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
