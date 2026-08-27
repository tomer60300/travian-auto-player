"""The night profile has to be derivable without me in the room.

Every night profile handed to the operator so far came out of a script in a
scratchpad. That made the account dependent on one person being available, which
is a worse defect than any of the arithmetic it computed.

These pin the properties that make the derivation worth shipping: it reads the
baseline the operator re-establishes rather than a snapshot's stock, it never
over-claims the account, consumers break even so the profile does not drift, and
demand it cannot meet is reported instead of quietly dropped.
"""

import pytest

from travian_api.services.distribution.allocation import AllocationMode, Resource
from travian_api.services.distribution.night_profile import (
    NightVillage,
    derive_night_profile,
)

HUB = 1
ARMY = 2
FAR = 3


def _village(vid, name, x, y, *, crop=0.0, lumber=0.0, wh=160_000, gr=160_000, to=10, merch=20):
    return NightVillage(
        village_id=vid,
        name=name,
        x=x,
        y=y,
        merchants_total=merch,
        trade_office_level=to,
        warehouse_capacity=wh,
        granary_capacity=gr,
        production={Resource.CROP: crop, Resource.LUMBER: lumber},
    )


def _account():
    return [
        _village(HUB, "hub", 0, 0, crop=60_000.0, lumber=13_000.0, wh=1_200_000, gr=800_000, to=19),
        _village(ARMY, "army", 2, 0, crop=-20_000.0, lumber=2_000.0),
        _village(FAR, "far", 60, 0, crop=5_000.0, lumber=1_800.0),
    ]


def _derive(**kw):
    defaults = dict(
        window_hours=8.0,
        map_span=401,
        speed_fields_per_hour=12.0,
        day_retention={Resource.LUMBER: {ARMY: 7_700.0}},
        hub_id=HUB,
        consumer_ids=[ARMY],
    )
    return derive_night_profile(_account(), **{**defaults, **kw})


class TestItDependsOnCapacityNotOnCurrentStock:
    def test_no_village_stock_is_needed_at_all(self):
        # The signature is the argument: NightVillage carries capacities and
        # production and no stock, so a profile cannot silently depend on the
        # minute a snapshot was taken.
        assert "stock" not in NightVillage.__dataclass_fields__

    def test_the_baseline_moves_every_ceiling(self):
        tight = _derive(baseline_fill=0.30)
        loose = _derive(baseline_fill=0.10)
        # A lower baseline leaves more room, so more may accumulate.
        assert (
            loose.allocations[Resource.LUMBER][FAR].value
            >= tight.allocations[Resource.LUMBER][FAR].value
        )


class TestConsumersBreakEven:
    def test_a_crop_consumer_retains_nothing(self):
        # Ending the night at the fill it started is what stops the profile
        # drifting; a consumer told to fill up would need rebuilding nightly.
        assert _derive().allocations[Resource.CROP][ARMY].value == 0.0

    def test_its_consumption_becomes_demand_someone_covers(self):
        crop = _derive().allocations[Resource.CROP]
        # The hub makes 60,000 and the army eats 20,000, so the hub must retain
        # less than it produces -- i.e. ship the difference.
        assert crop[HUB].value < 60_000


class TestItNeverOverClaimsTheAccount:
    def test_the_crop_retentions_do_not_exceed_production(self):
        profile = _derive()
        produced = sum(v.production[Resource.CROP] for v in _account())
        claimed = sum(a.value for a in profile.allocations[Resource.CROP].values())
        assert claimed <= produced + 1e-6, (
            "a remainder village cannot ship what the account does not make"
        )

    def test_a_tribute_is_taken_out_of_the_pool_not_added_to_it(self):
        with_tribute = _derive(tribute_per_hour=10_000.0, tribute_at=(60, 0))
        without = _derive()
        claimed_with = sum(a.value for a in with_tribute.allocations[Resource.CROP].values())
        claimed_without = sum(a.value for a in without.allocations[Resource.CROP].values())
        assert claimed_with < claimed_without, "the obligation leaves less to retain"

    def test_the_rounding_residual_is_absorbed_rather_than_left_negative(self):
        # Integer retentions cannot sum to a fractional production total, and the
        # planner reads ANY negative residual as over-claiming.
        villages = [
            _village(HUB, "hub", 0, 0, crop=60_000.7, lumber=13_000.0, wh=1_200_000, gr=800_000),
            _village(ARMY, "army", 2, 0, crop=-20_000.3, lumber=2_000.0),
            _village(FAR, "far", 60, 0, crop=5_000.4, lumber=1_800.0),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            map_span=401,
            speed_fields_per_hour=12.0,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[ARMY],
        )
        produced = sum(v.production[Resource.CROP] for v in villages)
        claimed = sum(a.value for a in profile.allocations[Resource.CROP].values())
        assert produced - claimed >= 0


class TestEveryVillageIsAccountedFor:
    def test_no_village_is_left_out_of_any_resource(self):
        profile = _derive()
        for resource, entries in profile.allocations.items():
            assert set(entries) == {HUB, ARMY, FAR}, f"{resource.value} is missing a village"

    def test_the_hub_absorbs_the_materials(self):
        assert _derive().allocations[Resource.LUMBER][HUB].mode is AllocationMode.REMAINDER, (
            "at night the hub's consumers fill up and it has the room"
        )


class TestUnmeetableDemandIsReported:
    def test_a_tribute_nobody_can_cover_is_stated(self):
        # Silence here would read as a covered obligation, and the operator would
        # find out from an ally rather than from the plan.
        profile = _derive(tribute_per_hour=500_000.0, tribute_at=(60, 0))
        assert profile.unmet[Resource.CROP] > 0

    def test_a_coverable_one_reports_nothing_outstanding(self):
        profile = _derive(tribute_per_hour=1_000.0, tribute_at=(60, 0))
        assert profile.unmet[Resource.CROP] == pytest.approx(0.0)
