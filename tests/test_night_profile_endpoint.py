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

from travian_api.services.distribution.allocation import Resource
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
