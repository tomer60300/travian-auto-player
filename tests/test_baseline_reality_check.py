"""The typed baseline fill, checked against the stores actually fetched.

`baseline_fill` is the one number the account cannot supply: it is where the
operator expects their stores to BE when the night starts, and the whole
derivation rests on it -- every ceiling is
``(target_fill - baseline_fill) x capacity / window_hours``. It is deliberately
NOT read from the snapshot, because a baseline the operator re-establishes each
night makes a profile that holds for weeks while a baseline measured from
whatever a snapshot caught goes stale within the hour. That is the operator's
call and it stays theirs.

What went wrong in practice: the derivation assumed 30% and village 02's
granary was at its cap, burning ~1.5M crop/day. So it reserved 46,670 crop/h to
"fill 02 toward 80%" -- crop poured into a store that destroys it on arrival --
and the account came up 5,598/h short of a tribute it could otherwise have paid.
Nothing said a word, because nothing compared the assumption to the fetched
stocks sitting in the same request.

These tests pin the warning, not a behaviour change: the derivation still obeys
the typed baseline exactly. The operator keeps the decision and gains the fact.
"""

import asyncio

import pytest
from fastapi import HTTPException

from travian_api.web.routes import distribution as dist
from travian_api.web.routes.distribution import NightProfileRequest

from .test_distribution_audit import USER

NIGHT = (23 * 60, 7 * 60)


def _village(vid, name, x, *, crop_stock, granary=100_000, crop=3000, fill=0.30):
    # Every store starts at `fill` of its capacity so a test about CROP is not
    # perturbed by lumber sitting at some unrelated fraction -- the check looks
    # at warehouses as well as granaries, and an uncontrolled fixture made the
    # first version of these tests fail for a reason that had nothing to do
    # with what they were asserting.
    warehouse = 80_000
    return {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": 0,
        "merchants_total": 20,
        "merchants_free": 20,
        "lumber_per_hour": 2000,
        "clay_per_hour": 1000,
        "iron_per_hour": 1000,
        "crop_per_hour": crop,
        "lumber_stock": round(warehouse * fill),
        "clay_stock": round(warehouse * fill),
        "iron_stock": round(warehouse * fill),
        "crop_stock": crop_stock,
        "warehouse_capacity": warehouse,
        "granary_capacity": granary,
    }


def _derive(baseline: float, *, hub_stock: int):
    body = NightProfileRequest.model_validate(
        {
            "snapshot": [
                # The hub, whose CROP fill is the variable under test; every
                # other store sits exactly at the typed baseline.
                _village(1, "02", 0, crop_stock=hub_stock, fill=baseline),
                _village(
                    2, "farm", 10, crop_stock=round(100_000 * baseline), crop=8000, fill=baseline
                ),
                _village(
                    3, "army", 20, crop_stock=round(100_000 * baseline), crop=-2000, fill=baseline
                ),
            ],
            "config": [{"village_id": v, "trade_office_level": 10} for v in (1, 2, 3)],
            "allocations": {"crop": {"1": {"mode": "remainder", "value": 0}}},
            "dispatch_window": NIGHT,
            "baseline_fill": baseline,
            "target_fill": 0.8,
        }
    )
    return asyncio.run(dist.post_night_profile(body, USER))


def _fill_warnings(res):
    return [w for w in res.warnings if "fuller than the" in w or "emptier than the" in w]


class TestItSaysWhenTheAssumptionContradictsTheSnapshot:
    def test_a_store_already_past_the_target_is_named(self):
        # The live case: baseline says 30%, the granary is at 95% and losing
        # crop at its cap. Reserving room to "fill it" burns the reservation.
        res = _derive(0.30, hub_stock=95_000)

        warned = _fill_warnings(res)
        assert warned, f"no fill warning among {res.warnings}"
        assert "02" in " ".join(warned)
        assert "95%" in " ".join(warned), warned

    def test_the_derivation_still_obeys_the_typed_baseline(self):
        """A warning, not a correction. The operator owns this number: two runs
        differing only in the fetched STOCK must derive the same allocations,
        because stock is deliberately not an input to the ceiling."""
        low = _derive(0.30, hub_stock=95_000)
        high = _derive(0.30, hub_stock=5_000)

        assert low.allocations == high.allocations

    def test_a_snapshot_matching_the_baseline_says_nothing(self):
        # 30% typed, ~30% measured: silence is the correct output. A warning
        # that fires on the normal case is one nobody reads on the bad case.
        res = _derive(0.30, hub_stock=30_000)

        assert _fill_warnings(res) == [], res.warnings

    def test_a_store_far_emptier_than_assumed_is_named_too(self):
        """The other direction is also worth knowing: assuming stores are half
        full when they are nearly empty means the night ships toward a target it
        will not reach, and the operator wakes to less than the profile promised.
        """
        res = _derive(0.60, hub_stock=2_000)

        warned = _fill_warnings(res)
        assert warned, f"no fill warning among {res.warnings}"
        assert "emptier" in " ".join(warned)

    def test_an_unreadable_capacity_is_refused_not_guessed(self):
        """Found by writing this very test: a None capacity reached the ceiling
        arithmetic and raised TypeError, killing the whole derivation. Every
        night ceiling is a FRACTION of the capacity, so a guess would decide how
        much crop is shipped into that store overnight -- refused by name, the
        way an unreadable crop balance already is."""
        body = NightProfileRequest.model_validate(
            {
                "snapshot": [
                    {**_village(1, "02", 0, crop_stock=95_000), "granary_capacity": None},
                    _village(2, "farm", 10, crop_stock=30_000, crop=8000),
                ],
                "config": [{"village_id": v, "trade_office_level": 10} for v in (1, 2)],
                "allocations": {"crop": {"1": {"mode": "remainder", "value": 0}}},
                "dispatch_window": NIGHT,
                "baseline_fill": 0.30,
                "target_fill": 0.8,
            }
        )

        with pytest.raises(HTTPException) as caught:
            asyncio.run(dist.post_night_profile(body, USER))

        assert caught.value.status_code == 422
        assert "02" in caught.value.detail, caught.value.detail

    def test_an_unreadable_warehouse_capacity_is_refused_too(self):
        """The other half of the same guard, and the one a type check reported as
        a live 500: `VillageSnapshot.warehouse_capacity` is `int | None`
        ("None when the capacity page was not needed by the crop read"),
        `NightVillage` is an unvalidated dataclass declaring `int`, and the
        ceiling arithmetic multiplies it. The granary case was pinned; this one
        rode on the same `or`, so deleting half the condition broke nothing."""
        body = NightProfileRequest.model_validate(
            {
                "snapshot": [
                    {**_village(1, "02", 0, crop_stock=95_000), "warehouse_capacity": None},
                    _village(2, "farm", 10, crop_stock=30_000, crop=8000),
                ],
                "config": [{"village_id": v, "trade_office_level": 10} for v in (1, 2)],
                "allocations": {"crop": {"1": {"mode": "remainder", "value": 0}}},
                "dispatch_window": NIGHT,
                "baseline_fill": 0.30,
                "target_fill": 0.8,
            }
        )

        with pytest.raises(HTTPException) as caught:
            asyncio.run(dist.post_night_profile(body, USER))

        assert caught.value.status_code == 422
        assert "02" in caught.value.detail, caught.value.detail

    @pytest.mark.parametrize("stock", [59_000, 61_000])
    def test_the_threshold_is_not_hair_trigger(self, stock):
        """Stores drift constantly; a check that fires at every few percent is
        noise. Within a generous band of the typed baseline it stays quiet."""
        res = _derive(0.60, hub_stock=stock)

        assert _fill_warnings(res) == [], res.warnings
