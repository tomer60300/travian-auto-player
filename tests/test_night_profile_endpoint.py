"""Deriving a night profile through the endpoint the page will call.

The derivation is pure and tested on its own; what these cover is the part the
operator actually touches. Two things matter here.

It must not ask for what it can work out. The hub is whichever village the DAY
allocations already send their surplus to, the crop consumers are the villages
with negative crop, the window comes from the profile's own hours and the tribute
from the foreign targets -- asking again would be asking the operator to restate
their own account, and every restatement is a chance to disagree with itself.

And it must show its reasoning. A derivation whose inputs are invisible is one
nobody can check, so the response names the hub it chose, the consumers it found
and anything it could not cover.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from travian_api.services.distribution.allocation import AllocationMode, Resource
from travian_api.web.routes.distribution import (
    ForeignTarget,
    NightProfileRequest,
    post_night_profile,
)

USER = SimpleNamespace(id=1)
HUB, ARMY, FAR = 20002, 20003, 20011
NIGHT = (23 * 60, 7 * 60)


def _village(vid, name, x, y, *, crop, lumber=1750, wh=160_000, gr=160_000, merch=20):
    return {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": merch,
        "merchants_free": merch,
        "lumber_per_hour": lumber,
        "clay_per_hour": 1750,
        "iron_per_hour": 1750,
        "crop_per_hour": crop,
        "warehouse_capacity": wh,
        "granary_capacity": gr,
    }


def _body(**extra):
    payload = {
        "snapshot": [
            _village(HUB, "02", 22, 88, crop=66_000, lumber=13_650, wh=1_200_000, gr=800_000),
            _village(ARMY, "03", 23, 88, crop=-23_000),
            _village(FAR, "11", 13, 72, crop=4_000),
        ],
        "config": [{"village_id": HUB, "trade_office_level": 19}],
        # Read as the DAY profile: the hub is its materials remainder, and the
        # army village's share is what the day already gives it.
        "allocations": {
            "lumber": {
                str(HUB): {"mode": "remainder", "value": 0},
                str(ARMY): {"mode": "absolute", "value": 7700},
                str(FAR): {"mode": "absolute", "value": 827},
            },
            "crop": {str(HUB): {"mode": "remainder", "value": 0}},
        },
        "dispatch_window": list(NIGHT),
        **extra,
    }
    return NightProfileRequest.model_validate(payload)


def _derive(**extra):
    return asyncio.run(post_night_profile(_body(**extra), USER))


class TestItInfersWhatItCan:
    def test_the_hub_is_the_day_profiles_remainder_village(self):
        assert _derive().hub == HUB

    def test_the_consumers_are_the_villages_with_negative_crop(self):
        assert _derive().consumers == ["03"]

    def test_the_window_comes_from_the_profiles_own_hours(self):
        assert _derive().window_hours == pytest.approx(8.0)

    def test_the_tribute_comes_from_the_route_eligible_targets(self):
        body = _body()
        body.foreign_targets = [
            ForeignTarget(name="01Arb", x=46, y=133, crop_per_hour=25_700, route_eligible=True),
            ForeignTarget(name="Manual", x=10, y=10, crop_per_hour=9_000, route_eligible=False),
        ]
        result = asyncio.run(post_night_profile(body, USER))
        assert result.tribute_per_hour == pytest.approx(25_700), (
            "a target that cannot be routed to is not an obligation this can plan"
        )

    def test_the_safety_margin_raises_the_night_obligation_too(self):
        # The plan path and the manual-transfer path both ship
        # `crop_per_hour * (1 + margin/100)`; the night path took the bare rate.
        # The night then frees exactly the promise while the day books the
        # promise plus the margin, so the remainder village drains further than
        # the profile predicts -- or the plan reads OVER_ALLOCATED.
        def _with(margin):
            body = _body()
            body.foreign_targets = [
                ForeignTarget(
                    name="01Arb",
                    x=24,
                    y=88,
                    crop_per_hour=200_000,
                    route_eligible=True,
                    safety_margin_pct=margin,
                )
            ]
            return asyncio.run(post_night_profile(body, USER))

        assert _with(0.0).tribute_per_hour == pytest.approx(200_000.0)
        assert _with(10.0).tribute_per_hour == pytest.approx(220_000.0)
        assert _with(10.0).unmet[Resource.CROP] == pytest.approx(
            _with(0.0).unmet[Resource.CROP] + 20_000.0
        ), "the margin is crop the night has to free, like any other obligation"


class TestEveryForeignTargetIsItsOwnDestination:
    """Two obligations are two places, and only the first one's coordinates were
    ever used.

    `tribute` summed every route-eligible target's rate while `tribute_at` took
    the FIRST one in request order, so a 500/h ally 2 fields away and a 20,000/h
    artifact 60 fields away became 20,500/h priced at the 2-field hop: about
    forty-eight turnarounds credited to a leg that is a 10h round trip in an 8h
    night, and the whole obligation read as covered. Swapping the two entries in
    the request body gave the opposite answer.
    """

    def _targets(self, order):
        near = ForeignTarget(name="WW", x=24, y=88, crop_per_hour=500, route_eligible=True)
        far = ForeignTarget(name="artifact", x=82, y=88, crop_per_hour=20_000, route_eligible=True)
        return [near, far] if order == "near-first" else [far, near]

    def _derive(self, order):
        body = _body()
        body.foreign_targets = self._targets(order)
        return asyncio.run(post_night_profile(body, USER))

    def test_the_answer_does_not_depend_on_the_order_they_were_typed_in(self):
        near_first = self._derive("near-first")
        far_first = self._derive("far-first")

        assert near_first.allocations == far_first.allocations
        assert near_first.unmet == far_first.unmet

    def test_the_far_obligation_is_not_priced_at_the_near_hop(self):
        # 60 fields at 12 f/h is a 10h round trip: nothing reaches it tonight,
        # so its 20,000/h is outstanding however close the other target is.
        result = self._derive("near-first")

        assert result.unmet[Resource.CROP] >= 20_000.0


class TestTheOperatorSuppliesOnlyWhatTheAccountCannot:
    def test_the_baseline_changes_the_answer(self):
        empty = _derive(baseline_fill=0.10)
        full = _derive(baseline_fill=0.50)
        # Asserted on the RECEIVER, deliberately. A village with room to spare
        # keeps exactly what it makes at any baseline -- correctly, since it is
        # neither sending nor receiving -- so it would show nothing. The baseline
        # bites where a village is being filled: 7,700 lumber an hour fits an
        # 8-hour night from 10% and does not from 50%.
        assert (
            empty.allocations[Resource.LUMBER][ARMY].value
            > full.allocations[Resource.LUMBER][ARMY].value
        )

    def test_a_negative_absolute_day_retention_is_refused(self):
        """Found 2026-09-02 while closing the same hole in the plan endpoints.

        This endpoint reads ``alloc.value`` straight off the request instead of
        building an ``Allocation``, so the dataclass guard that now refuses a
        negative ABSOLUTE target never fires here. Left alone, -4,000/h flows in
        as a "retention" and either derives nonsense or surfaces later as a 500
        from inside the derivation. Refuse it at the door, like the plan does.
        """
        body = _body(
            allocations={
                "lumber": {
                    str(HUB): {"mode": "remainder", "value": 0},
                    str(ARMY): {"mode": "absolute", "value": -4000},
                },
                "crop": {str(HUB): {"mode": "remainder", "value": 0}},
            }
        )
        with pytest.raises(HTTPException) as exc:
            asyncio.run(post_night_profile(body, USER))
        assert exc.value.status_code == 400
        assert "-4000" in str(exc.value.detail)
        assert "03" in str(exc.value.detail), "the refusal should name the village"

    def test_a_target_at_or_below_the_baseline_is_refused(self):
        # There would be no room for anything to arrive in, so the whole profile
        # would be a set of zeroes wearing the shape of a plan.
        with pytest.raises(Exception) as caught:
            _body(baseline_fill=0.80, target_fill=0.80)
        assert "baseline" in str(caught.value)


class TestItShowsItsReasoning:
    def test_it_names_the_villages_it_drew_on(self):
        result = _derive()
        assert Resource.CROP in result.drawn_in or Resource.CROP in result.forced_senders

    def test_demand_it_could_not_cover_is_reported_not_swallowed(self):
        body = _body()
        body.foreign_targets = [
            ForeignTarget(
                name="Impossible", x=46, y=133, crop_per_hour=900_000, route_eligible=True
            )
        ]
        result = asyncio.run(post_night_profile(body, USER))
        assert result.unmet.get(Resource.CROP, 0) > 0
        assert any("no village could cover" in w for w in result.warnings)

    def test_an_inferred_hub_says_so(self):
        # A silently chosen hub would move every material route with no
        # explanation, so choosing one without a remainder village must be loud.
        body = _body(allocations={"crop": {str(HUB): {"mode": "absolute", "value": 0}}})
        result = asyncio.run(post_night_profile(body, USER))
        assert any("remainder village" in w for w in result.warnings)


class TestItRefusesRatherThanGuess:
    def test_a_profile_with_no_hours_cannot_be_derived(self):
        # The ceiling IS room divided by hours. Without hours there is no
        # ceiling, and inventing a window would invent the whole profile.
        body = _body()
        body.dispatch_window = None
        with pytest.raises(HTTPException) as caught:
            asyncio.run(post_night_profile(body, USER))
        assert caught.value.status_code == 400
        assert "window" in caught.value.detail

    def test_an_empty_snapshot_is_refused(self):
        body = _body()
        body.snapshot = []
        with pytest.raises(HTTPException) as caught:
            asyncio.run(post_night_profile(body, USER))
        assert caught.value.status_code == 400


class TestConsumptionReachesTheFourthPlanningPath:
    """`/night-profile` inherited `consumption_per_hour` and ignored it (R3-D2).

    This is the path that SEEDS the other three. The page posts the same
    `buildPlanPayload()` body here and writes the derived allocations straight
    into the active profile, so a spend dropped at this endpoint is a spend
    missing from every plan built on the night it produced -- and the operator
    has no way to tell, because the field was accepted without complaint.

    `NightVillage.production` was built from the raw snapshot rates, and the
    unknown-village 422 that `/plan` runs never ran here at all.
    """

    def _derive_with(self, consumption, vid=ARMY):
        return asyncio.run(
            post_night_profile(
                _body(
                    config=[
                        {"village_id": HUB, "trade_office_level": 19},
                        {"village_id": vid, "consumption_per_hour": consumption},
                    ]
                ),
                USER,
            )
        )

    def test_a_spend_of_the_whole_material_production_moves_the_derivation(self):
        """The reviewer's probe. 03 makes 1,750/h of clay and iron; declaring
        that it spends all of it means it has none to keep, and a derivation
        that still hands it 1,750 is one that never read the field."""
        before = _derive()
        after = self._derive_with({"lumber": 1750, "clay": 1750, "iron": 1750})

        for resource in (Resource.CLAY, Resource.IRON):
            assert before.allocations[resource][ARMY].value == pytest.approx(1750)
            assert after.allocations[resource][ARMY].value == pytest.approx(0), (
                f"{resource.value}: the declared spend never reached the derivation"
            )

    def test_a_partial_spend_moves_it_by_exactly_the_spend(self):
        """Net, not floored and not doubled: 1,750 made less 700 spent is
        1,050 to keep."""
        after = self._derive_with({"clay": 700})

        assert after.allocations[Resource.CLAY][ARMY].value == pytest.approx(1050)

    def test_the_crop_derivation_is_untouched_by_a_material_spend(self):
        """Crop cannot be declared at all (R3-D1) and the crop rate is already
        net, so netting materials must not disturb who counts as a consumer."""
        before = _derive()
        after = self._derive_with({"lumber": 1750, "clay": 1750, "iron": 1750})

        assert after.consumers == before.consumers == ["03"]
        assert after.allocations[Resource.CROP] == before.allocations[Resource.CROP]

    def test_a_spend_for_a_village_not_in_the_snapshot_is_refused(self):
        """The same 422 `/plan` raises. A figure attached to an id that is not
        being planned is a typo or a chiefed village, and either way the
        operator's declared spend is not reaching the profile they are reading
        -- which this endpoint answered with a cheerful 200."""
        with pytest.raises(HTTPException) as caught:
            self._derive_with({"lumber": 10}, vid=999)

        assert caught.value.status_code == 422
        assert "999" in str(caught.value.detail)

    def test_a_crop_spend_is_refused_here_too(self):
        """The ruling is at the schema, so it binds this request model as well."""
        with pytest.raises(ValidationError):
            _body(config=[{"village_id": ARMY, "consumption_per_hour": {"crop": 9000}}])


class TestASpendPastProductionCannotKillTheRequest:
    """R4-P1-1. A declared spend one unit above production made the derivation
    raise, and FastAPI turns an unhandled `AllocationError` into a **500**.

    Netting the spend off the material rates (R3-D2) made material production
    negative for the first time, and the derivation's closing "everyone
    untouched keeps exactly what it makes" loop built
    `Allocation(ABSOLUTE, round(production))` out of it -- which
    `Allocation.__post_init__` refuses. 03 makes 1,750/h of clay and iron with
    no explicit day allocation for either, so `{'clay': 1750}` answered 200 and
    `{'clay': 1751}` answered 500: the endpoint died on the boundary its own
    tests stopped at.

    Nothing to keep is zero, not a negative -- the same ruling the crop half
    already carries. And the uncovered part is not dropped: a village spending
    more than it makes must be FED that difference, so it lands in `demand`
    exactly as a receiver's would and is reported in `unmet`.
    """

    def _derive_with(self, consumption, vid=ARMY):
        return asyncio.run(
            post_night_profile(
                _body(
                    config=[
                        {"village_id": HUB, "trade_office_level": 19},
                        {"village_id": vid, "consumption_per_hour": consumption},
                    ]
                ),
                USER,
            )
        )

    # 03's own rate for both, and the two materials the fixture's day profile
    # says nothing about -- so they reach the untouched loop rather than the
    # receiver branch, which is the path that raised.
    OWN = 1750

    @pytest.mark.parametrize("resource", [Resource.CLAY, Resource.IRON], ids=lambda r: r.value)
    @pytest.mark.parametrize("over", [1, OWN], ids=["one-unit-past", "twice-production"])
    def test_a_spend_above_production_still_derives(self, resource, over):
        result = self._derive_with({resource.value: self.OWN + over})

        assert result.allocations[resource][ARMY].value == 0.0, (
            "a village spending more than it makes keeps nothing, not a negative"
        )
        assert result.allocations[resource][ARMY].mode is AllocationMode.ABSOLUTE

    @pytest.mark.parametrize("resource", [Resource.CLAY, Resource.IRON], ids=lambda r: r.value)
    def test_the_part_nobody_can_cover_is_reported_not_dropped(self, resource):
        """The hub makes 1,750/h of each material and the fixture has no other
        source of them, so a 4,000/h spend against a 1,750/h production leaves
        2,250/h to be fed and exactly 500/h of it uncoverable."""
        result = self._derive_with({resource.value: 4_000})

        assert result.unmet[resource] == pytest.approx(500.0)
        assert any("no village could cover" in w for w in result.warnings), result.warnings

    def test_a_spend_the_account_can_cover_reports_nothing_outstanding(self):
        """The other half of the pair: folding the deficit into `demand` must
        not invent a shortfall the hub's own production covers."""
        result = self._derive_with({"clay": self.OWN + 1})

        assert result.unmet == {}


class TestARoleTemplateReachesTheSameCrash:
    """The blast radius of R4-P1-1, which the ledger understated.

    The spend does not have to be typed per village: a role template carries
    one, and `_resolve_roles` merges it into every village that declares the
    role. So ONE template one unit past production takes the endpoint down for
    every village of that role at once -- and section 2's own DEF profile
    (5,168/h of clay) is one missing template allocation away from exactly
    that, because a template that states a spend but no clay ALLOCATION leaves
    its villages in the untouched loop that raised.

    03 and 11 each make 1,750/h of clay, so a `def` template is the whole
    fixture's clay production against one figure.
    """

    def _derive_with(self, spend, *, villages=(ARMY, FAR)):
        return asyncio.run(
            post_night_profile(
                _body(
                    config=[
                        {"village_id": HUB, "trade_office_level": 19},
                        *({"village_id": vid, "role": "def"} for vid in villages),
                    ],
                    # Consumption only, no clay allocation: the shape section
                    # 2's profile has, and the shape that reaches the loop.
                    roles={"def": {"consumption": spend}},
                ),
                USER,
            )
        )

    OWN = 1750

    @pytest.mark.parametrize("over", [1, 3_418], ids=["one-unit-past", "section-2s-def-figure"])
    def test_a_template_spend_above_production_still_derives(self, over):
        result = self._derive_with({"clay": self.OWN + over})

        for vid in (ARMY, FAR):
            assert result.allocations[Resource.CLAY][vid].value == 0.0

    def test_the_uncovered_part_of_a_template_spend_is_reported(self):
        """Both villages of the role are short 3,418/h against the hub's own
        1,750/h of clay, so 5,086/h is what nobody can cover."""
        result = self._derive_with({"clay": self.OWN + 3_418})

        assert result.unmet[Resource.CLAY] == pytest.approx(5_086.0)

    def test_one_village_of_the_role_is_enough_to_reach_it(self):
        result = self._derive_with({"clay": self.OWN + 3_418}, villages=(ARMY,))

        assert result.allocations[Resource.CLAY][ARMY].value == 0.0
        assert result.unmet[Resource.CLAY] == pytest.approx(1_668.0)
