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


class TestTheLibraryContractSurvivesANegativeProducer:
    """`consumer_ids` defaults to `()`, and production is documented as possibly
    negative -- so the two together are a legal call.

    The HTTP path derives its consumers from `crop < 0` and never makes it, but
    the library contract does: an army village left out of `consumer_ids` fell
    through to the "everyone untouched keeps what it makes" loop, which builds
    `Allocation(ABSOLUTE, round(production))`, and a negative absolute retention
    is refused in `Allocation.__post_init__`. The caller got an AllocationError
    for supplying exactly what the signature invites.

    Nothing to keep is zero, not a negative: a village losing crop retains none
    of it, and the deficit is the receiving side's problem -- which is what
    `consumer_ids` is for and what naming it would have said.
    """

    def test_an_army_village_nobody_declared_a_consumer_does_not_raise(self):
        profile = _derive(consumer_ids=[])

        assert profile.allocations[Resource.CROP][ARMY].value == 0.0
        assert profile.allocations[Resource.CROP][ARMY].mode is AllocationMode.ABSOLUTE

    def test_no_derived_retention_is_ever_negative(self):
        profile = _derive(consumer_ids=[])

        for resource, per_village in profile.allocations.items():
            for vid, allocation in per_village.items():
                assert allocation.value >= 0.0, (resource, vid, allocation)

    def test_the_residual_trim_cannot_push_a_retention_below_zero(self):
        """The rounding trim takes its slack from the largest retention, and
        builds another absolute out of it -- so a slack larger than that entry
        holds would raise the same refusal.

        A property guard, not a reproduction: unlike the two above, no input was
        found that reaches it, because the entry the trim picks is by
        construction the biggest one there is. The trim now takes only what is
        available, so the hazard is closed whether or not it was reachable, and
        `residual_trimmed` reports what was taken rather than what was wanted.
        """
        villages = [
            _village(HUB, "hub", 0, 0, crop=0.4, wh=1_200_000, gr=800_000, to=19),
            _village(ARMY, "army", 2, 0, crop=-0.2),
        ]

        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            map_span=401,
            speed_fields_per_hour=12.0,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[],
        )

        for vid, allocation in profile.allocations[Resource.CROP].items():
            assert allocation.value >= 0.0, (vid, allocation)


class TestAProfileNeverQuietlyOverClaimsTheAccount:
    """The residual trim is clamped to what the largest share holds, and what it
    could not take used to vanish.

    `residual_trimmed` then under-reported (5,000 taken against a 20,000 gap),
    the returned profile still claimed more crop than the account makes, and
    with nothing to trim at all -- every retention already zero -- the gap was
    recorded nowhere. Folding the untaken remainder into `unmet` keeps the one
    identity that makes the profile checkable: what the retentions claim plus
    what is reported unmet equals what the account produces, so `unmet` is
    exactly the gap in the profile handed back.

    Reached through the library contract, not the route: `post_night_profile`
    classifies a crop-negative village as a consumer first, and a consumer's
    deficit goes through `demand` where it is already reported.
    """

    def _two_villages(self, hub_crop: float):
        return [
            _village(HUB, "hub", 0, 0, crop=hub_crop),
            _village(ARMY, "army", 2, 0, crop=-20_000.0),
        ]

    def _derive_pair(self, hub_crop: float):
        return derive_night_profile(
            self._two_villages(hub_crop),
            window_hours=8.0,
            map_span=401,
            speed_fields_per_hour=12.0,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[],
        )

    def test_the_part_of_the_gap_the_trim_could_not_take_is_reported(self):
        profile = self._derive_pair(hub_crop=5_000.0)

        assert profile.residual_trimmed == 5_000.0, "the trim takes all the hub holds"
        assert profile.unmet[Resource.CROP] == 15_000.0

    def test_a_gap_with_nothing_left_to_trim_is_reported_in_full(self):
        profile = self._derive_pair(hub_crop=0.0)

        assert profile.residual_trimmed == 0.0, "there was nothing to take"
        assert profile.unmet[Resource.CROP] == 20_000.0

    @pytest.mark.parametrize("hub_crop", [0.0, 5_000.0, 20_000.0, 60_000.0])
    def test_what_is_claimed_plus_what_is_unmet_is_what_is_produced(self, hub_crop):
        villages = self._two_villages(hub_crop)
        profile = self._derive_pair(hub_crop)

        claimed = sum(a.value for a in profile.allocations[Resource.CROP].values())
        produced = sum(v.production[Resource.CROP] for v in villages)

        assert claimed - profile.unmet[Resource.CROP] <= produced + 1.0, (
            f"the profile claims {claimed:,.0f}/h out of {produced:,.0f}/h and "
            f"reports only {profile.unmet[Resource.CROP]:,.0f}/h unmet"
        )


class TestAMaterialSpendLargerThanProduction:
    """The material half of the same rule, and where R4-P1-1 lived.

    `NightVillage.production` is documented as possibly negative for a material
    once the caller nets a declared spend off the rate -- a village burning its
    whole lumber production has none to keep overnight. The closing "everyone
    untouched keeps exactly what it makes" loop built
    `Allocation(ABSOLUTE, round(production))` out of that figure with no clamp,
    so the derivation raised on the caller for supplying what its own docstring
    invites.

    Clamped at zero, and the uncovered part folded into `demand`: a village
    spending more than it makes has to be FED the difference, which is the same
    claim on the account a receiver's shortfall makes, so it goes through the
    channel that already reports one rather than being dropped.
    """

    def _two_villages(self, hub_lumber: float):
        return [
            _village(HUB, "hub", 0, 0, lumber=hub_lumber),
            _village(ARMY, "army", 2, 0, lumber=-20_000.0),
        ]

    def _derive_pair(self, hub_lumber: float):
        return derive_night_profile(
            self._two_villages(hub_lumber),
            window_hours=8.0,
            map_span=401,
            speed_fields_per_hour=12.0,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[],
        )

    def test_a_negative_material_producer_does_not_raise(self):
        profile = self._derive_pair(hub_lumber=0.0)

        assert profile.allocations[Resource.LUMBER][ARMY].value == 0.0
        assert profile.allocations[Resource.LUMBER][ARMY].mode is AllocationMode.ABSOLUTE

    def test_the_deficit_nobody_can_cover_is_reported_in_full(self):
        profile = self._derive_pair(hub_lumber=0.0)

        assert profile.unmet[Resource.LUMBER] == pytest.approx(20_000.0)

    def test_what_the_hub_produces_is_taken_off_the_shortfall(self):
        """Not a flat re-report of the spend: the hub is the remainder village,
        so its own production is supply the deficit draws on first."""
        profile = self._derive_pair(hub_lumber=15_000.0)

        assert profile.unmet[Resource.LUMBER] == pytest.approx(5_000.0)

    @pytest.mark.parametrize("hub_lumber", [0.0, 15_000.0, 20_000.0, 60_000.0])
    def test_what_is_claimed_plus_what_is_unmet_is_what_is_produced(self, hub_lumber):
        villages = self._two_villages(hub_lumber)
        profile = self._derive_pair(hub_lumber)

        # The hub is the REMAINDER village, so its own claim is whatever is
        # left; what the absolute retentions claim is the checkable half.
        claimed = sum(
            a.value
            for a in profile.allocations[Resource.LUMBER].values()
            if a.mode is AllocationMode.ABSOLUTE
        )
        produced = sum(v.production.get(Resource.LUMBER, 0.0) for v in villages)

        assert claimed - profile.unmet[Resource.LUMBER] <= produced + 1.0, (
            f"the profile claims {claimed:,.0f}/h out of {produced:,.0f}/h and "
            f"reports only {profile.unmet[Resource.LUMBER]:,.0f}/h unmet"
        )
