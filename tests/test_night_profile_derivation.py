"""The night profile has to be derivable without me in the room.

Every night profile handed to the operator so far came out of a script in a
scratchpad. That made the account dependent on one person being available, which
is a worse defect than any of the arithmetic it computed.

These pin the properties that make the derivation worth shipping: it reads the
baseline the operator re-establishes rather than a snapshot's stock, it never
over-claims the account, consumers break even so the profile does not drift, and
demand it cannot meet is reported instead of quietly dropped.
"""

import inspect

import pytest

from travian_api.services.distribution import night_profile
from travian_api.services.distribution.allocation import AllocationMode, Resource
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON, MerchantModel
from travian_api.services.distribution.night_profile import (
    NightVillage,
    derive_night_profile,
)

HUB = 1
ARMY = 2
FAR = 3


def _village(
    vid, name, x, y, *, crop=0.0, lumber=0.0, wh=160_000, gr=160_000, to=10, merch=20, cap=None
):
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
        max_busy_merchants=cap,
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
        geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
        merchant_model=EUROPE2_TEUTON,
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
        # 4 fields out, so the obligation is actually payable: at (60|0) it is a
        # 10h round trip in an 8h night and the account rightly keeps the crop.
        with_tribute = _derive(tribute_per_hour=10_000.0, tribute_at=(4, 0))
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
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
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
        # 4 fields: a 40-minute round trip, so the 1,000/h really is coverable.
        # It used to be asserted at (60|0) -- a 10h round trip inside an 8h
        # night, which no fleet of any size can make -- and passed because the
        # demand-weighted mean hop booked ten trips' worth of a journey nobody
        # can complete. The premise contradicted the module's own
        # no-partial-trip rule; see the case below for what (60|0) means.
        profile = _derive(tribute_per_hour=1_000.0, tribute_at=(4, 0))
        assert profile.unmet[Resource.CROP] == pytest.approx(0.0)

    def test_a_tribute_no_round_trip_reaches_is_outstanding_however_small(self):
        # 60 fields at 12 f/h is a 10h round trip. Zero complete trips in an 8h
        # window means zero crop delivered, whatever the fleet -- so the whole
        # obligation is outstanding, and a small one is exactly the case a
        # weighted mean over a NEAR consumer used to swallow.
        profile = _derive(tribute_per_hour=1_000.0, tribute_at=(60, 0))
        assert profile.unmet[Resource.CROP] == pytest.approx(1_000.0)

    def test_the_hub_still_keeps_what_it_cannot_deliver(self):
        # And it is not quietly shed: crop shipped nowhere is crop lost.
        with_far = _derive(tribute_per_hour=1_000.0, tribute_at=(60, 0))
        without = _derive()
        assert (
            with_far.allocations[Resource.CROP][HUB].value
            == without.allocations[Resource.CROP][HUB].value
        )


class TestTheCropDrawIsOrderedByWhereTheCropGoes:
    """Ordering the crop draw by distance to the HUB, or to the tribute, spends
    merchants for nothing.

    Coverage is order-invariant -- `give = min(own, demand, shed_limit)` and
    `shed_limit` reads nothing either loop mutates -- so `unmet` cannot move.
    COST is not. The hub is not where crop goes: the crop pass ships to the
    crop-negative villages and to the tribute, and on this account the hub is a
    crop sender rather than a sink. So the draw picked the village nearest the
    hub and paid for a long haul that a village next door to the hammer would
    have made in ten minutes.

    The right cost already existed one function away: the demand-weighted mean
    hop `shed_limit` derives from the same destination set.
    """

    ARMY_X = 20
    NEAR_HUB = 21  # 2 fields from the hub, 18 from the hammer
    NEAR_ARMY = 22  # 19 fields from the hub, 1 from the hammer

    def _derive(self, villages, **kw):
        return derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            **kw,
        )

    def _account(self):
        return [
            _village(HUB, "hub", 0, 0, crop=0.0),
            _village(ARMY, "hammer", self.ARMY_X, 0, crop=-20_000.0),
            # Granaries large enough that neither is a FORCED sender: this is
            # about the order the draw picks them in, not about overflow.
            _village(self.NEAR_HUB, "A", 2, 0, crop=20_000.0, gr=1_600_000),
            _village(self.NEAR_ARMY, "B", 19, 0, crop=20_000.0, gr=1_600_000),
        ]

    def test_the_supplier_next_to_the_hammer_is_drawn_first(self):
        # A is 2 fields from the hub and 18 from the hammer: a 3h round trip,
        # two turnarounds. B is 19 from the hub and ONE from the hammer: ten
        # minutes, forty-eight turnarounds. Hub-ordering drew A.
        profile = self._derive(self._account(), consumer_ids=[ARMY])

        assert profile.drawn_in[Resource.CROP] == [self.NEAR_ARMY]
        assert profile.allocations[Resource.CROP][self.NEAR_HUB].value == pytest.approx(20_000.0)
        assert profile.allocations[Resource.CROP][self.NEAR_ARMY].value == pytest.approx(0.0)

    def test_coverage_is_the_same_either_way(self):
        # The reduction is in merchants, not in what the night delivers.
        assert self._derive(self._account(), consumer_ids=[ARMY]).unmet[
            Resource.CROP
        ] == pytest.approx(0.0)

    def test_a_small_tribute_does_not_order_the_whole_account(self):
        """The tribute branch is worse than the hub branch, not better.

        40,000/h of consumers beside a 1,000/h obligation ordered every supplier
        by the 1,000/h destination. A is one field from the tribute and 39 from
        the hammer -- one round trip in the night; B is one field from the
        hammer and 39 from the tribute. The obligation worth 2.4% of the demand
        decided who shipped.
        """
        villages = [
            _village(HUB, "hub", 0, 0, crop=0.0),
            _village(ARMY, "hammer", -20, 0, crop=-40_000.0),
            _village(self.NEAR_HUB, "A", 19, 0, crop=41_000.0, gr=1_600_000),
            _village(self.NEAR_ARMY, "B", -19, 0, crop=41_000.0, gr=1_600_000),
        ]
        profile = self._derive(
            villages,
            consumer_ids=[ARMY],
            tribute_per_hour=1_000.0,
            tribute_at=(20, 0),
        )

        assert profile.drawn_in[Resource.CROP][0] == self.NEAR_ARMY


class TestNoDestinationIsPromisedAFractionOfATrip:
    """Merchant-hours conservation is necessary, not sufficient.

    `S <= fleet * capacity / (2 * sum(w_i * hop_i))` lets merchant-time split
    fractionally across trips of different lengths, and a merchant cannot make
    0.6 of a trip. Two consumers needing the same amount, one 2 fields away and
    one 30, conserve merchant-hours exactly at 60,750/h -- and the far one needs
    26.7 trips of 5h each while eighteen merchants can make eighteen. So a
    hammer's whole 9,750/h shortfall read as covered.

    The per-destination bound is `S <= fleet * capacity * floor(H / (2*hop_i)) /
    (w_i * H)` for every destination, taken alongside the weighted mean rather
    than instead of it -- `max` over the hops was tried and zeroes every
    reachable destination whenever one is unreachable.
    """

    NEAR = 11
    FARR = 12

    def _villages(self):
        # Trade Office 13 puts a merchant at 9,000; 20 merchants less the
        # reserve of 2 is a fleet of 18. The hub's granary is large enough that
        # its ceiling does not force it, so the draw is what bounds it.
        return [
            _village(HUB, "hub", 0, 0, crop=100_000.0, gr=1_600_000, to=13),
            _village(self.NEAR, "near", 2, 0, crop=-30_000.0, to=13),
            _village(self.FARR, "hammer", 30, 0, crop=-30_000.0, to=13),
        ]

    def _profile(self):
        return derive_night_profile(
            self._villages(),
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[self.NEAR, self.FARR],
        )

    def test_the_shortfall_the_mean_hid_is_reported(self):
        # 18 merchants x 9,000 x 1 complete trip to the hammer = 162,000 over
        # the night, and the hammer's half of the split is what caps the whole
        # send: 40,500/h against 60,000/h of demand.
        assert self._profile().unmet[Resource.CROP] == pytest.approx(19_500.0)

    def test_the_hub_keeps_what_it_cannot_deliver(self):
        assert self._profile().allocations[Resource.CROP][HUB].value == pytest.approx(59_500.0)

    def test_every_destination_gets_a_whole_number_of_trips(self):
        """The property, checked directly rather than through the total.

        For each destination: the trips its share needs must not exceed the
        trips the fleet can make to it.
        """
        import math

        profile = self._profile()
        hub = self._villages()[0]
        shipped = 100_000.0 - profile.allocations[Resource.CROP][HUB].value
        window, capacity, fleet = 8.0, 9_000.0, 18
        claims = {self.NEAR: 30_000.0, self.FARR: 30_000.0}
        hops = {self.NEAR: 2 / 12.0, self.FARR: 30 / 12.0}
        total_claim = sum(claims.values())
        for vid, claim in claims.items():
            share = claim / total_claim
            needed = math.ceil(shipped * window * share / capacity)
            available = fleet * int(window // (2 * hops[vid]))
            assert needed <= available, (vid, needed, available)
        assert hub.production[Resource.CROP] == 100_000.0

    def test_one_destination_is_unchanged_by_the_new_bound(self):
        """With a single destination the share is 1 and the two bounds are the
        same formula, so nothing with one destination may move."""
        villages = [
            _village(HUB, "hub", 0, 0, crop=100_000.0, gr=1_600_000, to=13),
            _village(self.FARR, "hammer", 30, 0, crop=-30_000.0, to=13),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[self.FARR],
        )

        # 18 x 9,000 x 1 trip / 8h = 20,250/h, all of it to the hammer.
        assert profile.allocations[Resource.CROP][HUB].value == pytest.approx(79_750.0)
        assert profile.unmet[Resource.CROP] == pytest.approx(9_750.0)

    def test_an_unreachable_destination_does_not_zero_the_reachable_ones(self):
        """The regression `max` over the hops caused, pinned from the other side.

        An ally 60 fields off is a 10h round trip: unpayable at any fleet size.
        It must remove its OWN claim from what the night can deliver and leave
        the consumer 2 fields away exactly as shippable as it was.
        """
        # A granary large enough that the hub's own ceiling does not force it,
        # so the draw is the only thing deciding what it sheds.
        villages = [
            _village(HUB, "hub", 0, 0, crop=100_000.0, gr=4_000_000, to=13),
            _village(self.NEAR, "near", 2, 0, crop=-20_000.0, to=13),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[self.NEAR],
            tribute_per_hour=1_000.0,
            tribute_at=(60, 0),
        )

        assert profile.allocations[Resource.CROP][HUB].value == pytest.approx(80_000.0)
        assert profile.unmet[Resource.CROP] == pytest.approx(1_000.0)


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
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
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
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
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
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
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


class TestTheCapBoundsWhatADrawnInVillageMayShip:
    """A retention below production is a promise to ship the difference.

    ``shed_limit`` prices that promise -- fleet x what a merchant carries x the
    trips it fits in the window -- and under a cap the fleet that matters is the
    cap. The FORCED sender was held to it from the start, through ``capped()``;
    the two DRAW passes and the return visit to the forced senders were not, so
    a village capped at nothing was handed a retention of nothing and told to
    ship its whole production anyway -- exactly the retention
    ``NightVillage.max_busy_merchants`` says it exists to prevent. The gap was
    the fleet's as well as the cap's, and predates the cap.

    RE-SEEDED for the 25%/60% fills (2026-09-03). The tighter room between
    baseline and target moves every ceiling here, and on the crop side it moved
    the SHAPE and not just the figures: at 50% of the granary the hub was forced
    to shed 10,000/h and the army's 20,000/h deficit needed a draw, while at 35%
    it sheds 25,000/h and covers the whole deficit on its own -- so the crop
    draw and the return visit to the forced senders, which are what two of these
    tests are about, stopped happening at all. The army's deficit is 35,000/h
    here for exactly that reason: it keeps 10,000/h outstanding after the forced
    pass, which is the situation the tests were written to exercise. Everything
    asserted below is recomputed from the new ceilings, not adjusted until it
    passed.
    """

    # FAR makes 40,000 lumber into a 1,200,000 warehouse, so the night's own
    # ceiling (52,500/h) is no constraint and the day plan's 6,000 is what it
    # would keep.
    #
    # RE-SEEDED again (shed limit measured to the HUB). FAR is 6 fields from
    # the hub and 4 from its nearest neighbour, and its cargo goes to the hub:
    # 6 fields at 12 f/h is a 1h round trip, so 8 turnarounds in the window,
    # not the 12 the neighbour distance implied. At Trade Office 10 a merchant
    # carries 7,500, so the shed limit is 135,000/h with the fleet free
    # (18 x 7,500 x 8 / 8), 15,000/h held to two merchants, and nothing at all
    # at zero -- three answers either side of the 34,000/h the draw wants of
    # it. The old figures were 202,500 and 22,500 off 12 trips; the fixture did
    # not move, the distance being measured did.
    def _account(self, cap, hub_cap):
        return [
            _village(
                HUB,
                "hub",
                0,
                0,
                crop=60_000.0,
                lumber=-40_000.0,
                wh=1_200_000,
                gr=800_000,
                to=19,
                cap=hub_cap,
            ),
            _village(ARMY, "army", 2, 0, crop=-35_000.0, lumber=2_000.0),
            _village(FAR, "far", 6, 0, crop=5_000.0, lumber=40_000.0, wh=1_200_000, cap=cap),
        ]

    def _derive(self, *, cap=None, hub_cap=None):
        return derive_night_profile(
            self._account(cap, hub_cap),
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={Resource.LUMBER: {ARMY: 7_700.0, FAR: 6_000.0}},
            hub_id=HUB,
            consumer_ids=[ARMY],
        )

    def test_with_the_fleet_free_the_draw_is_what_it_always_was(self):
        profile = self._derive()

        assert profile.drawn_in[Resource.LUMBER] == [FAR]
        assert profile.allocations[Resource.LUMBER][FAR].value == 6_000.0

    def test_a_material_draw_stops_at_what_the_cap_can_carry(self):
        profile = self._derive(cap=2)

        # 15,000/h is all two merchants move to the HUB in the window, so the
        # other 25,000 stays here instead of being promised.
        assert profile.allocations[Resource.LUMBER][FAR].value == 25_000.0
        # And what the cap put out of reach is reported: the hub wants 40,000 of
        # lumber it does not make, plus the 5,000 the army needs delivered to
        # reach its 7,000 ceiling from a production of 2,000 -- 45,000 of demand
        # against 15,000 shipped.
        assert profile.unmet[Resource.LUMBER] == pytest.approx(30_000.0)

    def test_a_cap_of_zero_draws_nothing_and_reports_the_gap(self):
        profile = self._derive(cap=0)

        assert profile.allocations[Resource.LUMBER][FAR].value == 40_000.0
        assert profile.drawn_in[Resource.LUMBER] == []
        # Reported, never hidden: the whole 45,000/h the cap put out of reach.
        assert profile.unmet[Resource.LUMBER] == pytest.approx(45_000.0)

    def test_a_village_that_cannot_get_home_by_morning_sheds_nothing(self):
        """Section 6: no merchant may still be out at 07:00.

        The shed limit used to floor its trip count at one, so a village whose
        round trip does not fit the window was credited a whole trip anyway --
        promising cargo that would provably still be in the air at the profile
        switch, which is the one thing section 6 forbids outright. Nothing else
        catches it here: the derivation is what the operator writes into the
        active profile, and `NIGHT_OVERRUN` is raised by the PLANNER, on routes
        this profile has already claimed are shippable.
        """
        # 60 fields from the hub at 12 f/h is 5h each way: a 10h round trip
        # inside an 8h night. One trip is not available at any fleet size.
        far_away = [
            _village(HUB, "hub", 0, 0, lumber=-40_000.0, wh=1_200_000, gr=800_000, to=19),
            _village(ARMY, "army", 2, 0, crop=-35_000.0, lumber=2_000.0),
            _village(FAR, "far", 60, 0, lumber=40_000.0, wh=1_200_000),
        ]
        profile = derive_night_profile(
            far_away,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={Resource.LUMBER: {FAR: 6_000.0}},
            hub_id=HUB,
            consumer_ids=[ARMY],
        )

        # It keeps everything it makes, is never drawn on, and the whole gap is
        # reported rather than papered over with a trip that cannot happen.
        # The gap is the hub's own 40,000/h deficit and nothing else: only FAR
        # carries a day retention here, so the army -- room 7,000/h against
        # production 2,000/h -- books no top-up of its own.
        assert profile.allocations[Resource.LUMBER][FAR].value == 40_000.0
        assert profile.drawn_in[Resource.LUMBER] == []
        assert profile.unmet[Resource.LUMBER] == pytest.approx(40_000.0)

    def test_the_crop_draw_honours_the_cap_too(self):
        """Materials and crop draw down separate branches, so one fix does not
        imply the other.

        That premise was FALSE for a while and is true again. Both branches were
        pointed at the hub, so they measured one distance and shared one bound;
        each now measures where its OWN cargo goes, and in this fixture those
        differ -- FAR is 6 fields from the hub it sends lumber to (a 1h round
        trip, 8 turnarounds, 135,000/h) and 4 from the ARMY it sends crop to
        (0h40, 12 turnarounds, 202,500/h). Both are far above the 5,000/h FAR
        makes, which is the point: the cap is what decides here, and it has to
        decide on the crop branch as well.
        """
        assert self._derive().allocations[Resource.CROP][FAR].value == 0.0
        assert self._derive(cap=0).allocations[Resource.CROP][FAR].value == 5_000.0

    def test_coming_back_to_a_forced_sender_stays_inside_its_cap(self):
        """The second crop pass takes MORE off a village already shedding.

        It exists because a ceiling is not a floor under what a village may
        give. It is still bounded by what the village can move: at zero the hub
        keeps every unit it makes, and the army's deficit is reported instead.
        """
        # 35,000 off the forced pass (its 35% ceiling), then 5,000 more on the
        # return visit -- all that is left of the army's deficit once FAR's own
        # 5,000 has been drawn.
        assert self._derive().allocations[Resource.CROP][HUB].value == 30_000.0

        grounded = self._derive(hub_cap=0)

        assert grounded.allocations[Resource.CROP][HUB].value == 60_000.0
        assert grounded.unmet[Resource.CROP] == pytest.approx(30_000.0)


# ── Capacity and geometry are injected, never re-derived here ────────────────


class TestCapacityAndGeometryAreInjected:
    """Operator ruling section 1: capacity lives behind ONE injectable
    `MerchantModel` with `calibrate()`, and nothing else in the planner may
    hardcode a capacity -- a hardcoded one anywhere else IS a finding.

    This module took `merchant_base_capacity=2500` and
    `trade_office_bonus_per_level=0.2` as primitives and re-derived
    `base * (1 + k * level)` inline. The numbers were right and the HTTP path
    passed correct ones; the defect is that a `calibrate()` reading would
    update `merchants.py` and silently leave this copy stale. Geometry the
    same: a private `_wrapped_fields` re-implemented `MapGeometry`'s
    `min(raw, span - raw)`.
    """

    def test_it_takes_a_merchant_model_and_a_map_geometry(self):
        params = inspect.signature(derive_night_profile).parameters

        assert "merchant_model" in params, "capacity must arrive as the one injectable model"
        assert "geometry" in params, "distance must arrive as the one map model"
        for primitive in (
            "merchant_base_capacity",
            "trade_office_bonus_per_level",
            "speed_fields_per_hour",
            "map_span",
        ):
            assert primitive not in params, (
                f"{primitive} is a second place to state something the injected model states"
            )

    def test_the_reserve_comes_from_the_optimizers_own_constant(self):
        """It defaulted to a bare `2` beside `DEFAULT_MERCHANT_RESERVE`."""
        assert (
            inspect.signature(derive_night_profile).parameters["merchant_reserve"].default
            is night_profile.DEFAULT_MERCHANT_RESERVE
        )
        assert night_profile.DEFAULT_MERCHANT_RESERVE == 2

    def test_no_capacity_arithmetic_is_left_in_the_module(self):
        """The inline `base * (1 + bonus * level)` and the hand-rolled torus,
        by the text of them. Both now live in one place each."""
        source = inspect.getsource(night_profile)

        assert "2500" not in source
        assert "trade_office_bonus_per_level" not in source
        assert "_wrapped_fields" not in source

    @staticmethod
    def _forced_sender(model):
        """One sender half an hour from the hub, so the shed limit binds.

        18 merchants (20 less the reserve of 2) making 8 round trips in an
        8-hour night sheds `18 x capacity` an hour. Its granary ceiling is
        (0.60 - 0.25) x 160,000 / 8 = 7,000/h, far under its own 100,000/h, so
        it is a forced sender and its retention is `own - shed_limit`.

        RE-SEEDED (crop is bounded by where crop goes). The hub is named the
        crop CONSUMER here, and its rate made negative to be one. It was
        neither, and the sender's crop therefore had no declared destination at
        all -- which the derivation now reads as shedding nothing, so the
        arithmetic these four tests exist to check never ran. The distance being
        measured is unchanged: 6 fields, the sender to the hub, exactly as
        before. Nothing else moved, and no expected figure did.
        """
        villages = [
            _village(HUB, "hub", 0, 0, crop=-20_000.0, wh=1_200_000, gr=800_000, to=0),
            _village(ARMY, "sender", 6, 0, crop=100_000.0, to=0),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=model,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[HUB],
        )
        return profile.allocations[Resource.CROP][ARMY].value

    def test_the_injected_model_is_what_sizes_the_shed_limit(self):
        # 18 x 2,500 = 45,000/h shed, so 100,000 - 45,000 = 55,000 is retained.
        assert self._forced_sender(EUROPE2_TEUTON) == pytest.approx(55_000.0)

    def test_a_recalibrated_model_moves_it_with_no_other_change(self):
        # Half the capacity carries half the cargo: 18 x 1,250 = 22,500/h shed,
        # so 100,000 - 22,500 = 77,500 is retained. This is the whole point of
        # the seam -- one `calibrate()` reading has to reach here too.
        half = MerchantModel(base_capacity=1_250, bonus_per_trade_office_level=0.20)

        assert self._forced_sender(half) == pytest.approx(77_500.0)

    def test_the_trade_office_bonus_comes_from_the_model_too(self):
        """A Trade Office 10 village carries 2,500 x (1 + 0.2 x 10) = 7,500,
        three times the base -- so 18 of them shed 135,000/h, more than the
        village makes, and the ceiling is what is left holding it: 7,000.

        Hub crop negative and named a consumer for the reason `_forced_sender`
        states: crop with nowhere declared to go sheds nothing, and then this
        measures no capacity at all.
        """
        villages = [
            _village(HUB, "hub", 0, 0, crop=-20_000.0, wh=1_200_000, gr=800_000, to=0),
            _village(ARMY, "sender", 6, 0, crop=100_000.0, to=10),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[HUB],
        )

        assert profile.allocations[Resource.CROP][ARMY].value == pytest.approx(7_000.0)

    def test_the_odd_span_boundary_is_the_map_geometrys_answer(self):
        """The one place a hand-rolled torus and `MapGeometry` could differ.

        `_wrapped_fields` mod-ed and compared against `span / 2` (200.5 on a
        401-wide world); `MapGeometry` takes `min(raw, span - raw)`. They agree
        everywhere a village can be, and the boundary is where that is worth
        checking: 201 apart IS 200 apart, and 205 apart is 196 -- so on this
        map the farther-looking village is the nearer one.
        """
        geometry = MapGeometry(span=401, speed_fields_per_hour=12.0)

        assert geometry.distance((-100, 0), (100, 0)) == pytest.approx(200.0)
        assert geometry.distance((-100, 0), (101, 0)) == pytest.approx(200.0)
        assert geometry.distance((-100, 0), (105, 0)) == pytest.approx(196.0)

    def test_the_derivation_measures_around_the_seam_and_not_across_it(self):
        """A hub at (-200|0) and a sender at (199|0) are two fields apart, not
        399. Two fields is 24 round trips in an 8-hour night, so 18 merchants
        shed 135,000/h and the sender's 100,000/h is held down to its 7,000/h
        ceiling. Measured flat the round trip does not fit the night at all,
        the shed limit is zero, and the same village is told to keep every
        unit it makes.

        Hub crop negative and named a consumer for the reason `_forced_sender`
        states. It has to be the hub here and not a third village: the point of
        the fixture is that sender and destination sit on OPPOSITE edges, and
        the only tile two fields from (199|0) across the seam is the hub's.
        """
        villages = [
            _village(HUB, "hub", -200, 0, crop=-20_000.0, wh=1_200_000, gr=800_000, to=0),
            _village(ARMY, "across-the-seam", 199, 0, crop=100_000.0, to=0),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[HUB],
        )

        assert profile.allocations[Resource.CROP][ARMY].value == pytest.approx(7_000.0)


class TestTheHubIsBoundedByWhereItsCropActuallyGoes:
    """The hub reaches `shed_limit` only as a forced CROP sender.

    Its granary ceiling can sit under its own production, and then the crop pass
    forces it to shed -- but its cargo plainly does not travel to itself, so the
    hub-distance bound reads zero and something else has to stand in. It used to
    be the nearest village of ANY kind, which is exactly the over-estimating
    bound the branch removed for every other sender: a feeder one field away
    yields dozens of turnarounds in an 8h night, the bound never binds, and the
    hub is booked to ship crop to a consumer forty fields away at a next-door
    village's rate.

    The canonical fixture cannot see it, because there the hub's nearest
    neighbour IS the consumer. Here a feeder sits between them.
    """

    def _account(self, consumer_at):
        return [
            _village(HUB, "hub", 0, 0, crop=60_000.0),
            _village(ARMY, "feeder", 1, 0, crop=0.0),
            _village(FAR, "hammer", consumer_at, 0, crop=-40_000.0),
        ]

    def _derive(self, consumer_at):
        return derive_night_profile(
            self._account(consumer_at),
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[FAR],
        )

    def test_the_bound_is_the_distance_to_the_consumer_not_to_the_feeder(self):
        """40 fields at 12 f/h is a 6h40 round trip: ONE complete trip in an 8h
        night, so 18 merchants carrying 7,500 each shed 16,875/h and no more.

        The feeder distance would have credited 47 trips -- 793,125/h, thirteen
        times the hub's whole production -- so the ceiling decided instead and
        the hub was left retaining 7,000/h of the 60,000 it makes.
        """
        profile = self._derive(consumer_at=40)

        assert profile.allocations[Resource.CROP][HUB].value == pytest.approx(43_125.0)
        # 60,000 made less 43,125 kept is the 16,875/h it can actually move,
        # against the hammer's 40,000/h deficit -- so 23,125/h is outstanding
        # and reported rather than booked as covered.
        assert profile.unmet[Resource.CROP] == pytest.approx(23_125.0)

    def test_a_consumer_no_round_trip_reaches_leaves_the_hub_shedding_nothing(self):
        """60 fields is a 10h round trip inside an 8h night: zero complete
        trips, so the honest answer is the hub keeping its whole production and
        the hammer's entire deficit reported unmet. Under the feeder bound this
        was 53,000/h of crop the operator would have written into the active
        profile as shippable."""
        profile = self._derive(consumer_at=60)

        assert profile.allocations[Resource.CROP][HUB].value == pytest.approx(60_000.0)
        assert profile.unmet[Resource.CROP] == pytest.approx(40_000.0)

    def test_a_tribute_is_a_destination_and_a_feeder_is_not(self):
        """`tribute_at` is somewhere the hub's crop really goes, so it counts
        among the destinations; the feeder next door does not, whatever the
        distance says. Measuring the feeder would credit 47 turnarounds against
        a 6h40 haul to the ally."""
        villages = [
            _village(HUB, "hub", 0, 0, crop=60_000.0),
            _village(ARMY, "feeder", 1, 0, crop=0.0),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            tribute_per_hour=80_000.0,
            tribute_at=(40, 0),
        )

        # One trip at 16,875/h, exactly as above: 60,000 - 16,875 = 43,125,
        # leaving 63,125/h of the obligation outstanding and reported.
        assert profile.allocations[Resource.CROP][HUB].value == pytest.approx(43_125.0)
        assert profile.unmet[Resource.CROP] == pytest.approx(63_125.0)


class TestEverySenderIsBoundedByWhereItsOwnCargoGoes:
    """`shed_limit` needs the RESOURCE, because the destination depends on it.

    The hub absorbs surplus MATERIALS, so for lumber, clay and iron the hub
    distance is where a sender's cargo really goes. Crop never reaches it: the
    crop pass ships to the villages in `consumer_ids`, or to `tribute_at`, and
    the hub is a crop SENDER on this account rather than a sink. Measuring
    every sender to the hub therefore bound each crop sender by a village its
    cargo never visits -- the same error the hub's own fallback was written to
    remove, one level down and for every OTHER village.

    It reads wrong in both directions, which is why both are pinned here.
    """

    def _account(self, *, hub_at, feeder_at, hammer_at):
        return [
            _village(HUB, "hub", *hub_at, crop=0.0, gr=800_000),
            _village(ARMY, "feeder", *feeder_at, crop=20_000.0, gr=800_000),
            _village(FAR, "hammer", *hammer_at, crop=-20_000.0, gr=800_000),
        ]

    def _derive(self, **kw):
        return derive_night_profile(
            self._account(**kw),
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[FAR],
        )

    # The feeder is one field from the hub and the hammer is moved out along x,
    # so the hub distance says the same thing at every row and the real haul
    # says three different things. 18 merchants (20 less the reserve) carry
    # 7,500 each at Trade Office 10, so the shed limit is `18 x 7,500 x trips /
    # 8`:
    #
    #   (5|0)   -> 4 fields, 0h40 round trip, 12 trips -> 202,500/h
    #   (40|0)  -> 39 fields, 6h30 round trip, 1 trip  ->  16,875/h
    #   (200|0) -> 199 fields, 33h10 round trip, 0 trips ->      0/h
    #
    # Measured to the hub instead it is 810,000/h at all three, which binds
    # nothing: the feeder was booked to ship its whole 20,000/h to a hammer
    # 199 fields away and `unmet` read 0.
    @pytest.mark.parametrize(
        ("hammer_x", "ships", "unmet"),
        [(5, 20_000.0, 0.0), (40, 16_875.0, 3_125.0), (200, 0.0, 20_000.0)],
    )
    def test_a_crop_sender_is_bounded_by_the_haul_to_the_consumer(self, hammer_x, ships, unmet):
        profile = self._derive(hub_at=(0, 0), feeder_at=(1, 0), hammer_at=(hammer_x, 0))

        assert profile.allocations[Resource.CROP][ARMY].value == pytest.approx(20_000.0 - ships)
        assert profile.unmet[Resource.CROP] == pytest.approx(unmet)

    def test_a_ten_minute_haul_to_the_consumer_stays_shippable(self):
        """The other direction, and the one the hub distance made worse.

        The feeder is 199 fields from the hub and ONE field from the hammer it
        actually feeds: a 10-minute round trip, 48 turnarounds in an 8h night.
        Measured to the hub the round trip is 33h10, no trip fits the window at
        all, and a haul the merchants could make forty-eight times over was
        reported unshippable with the hammer's whole deficit unmet.
        """
        profile = self._derive(hub_at=(200, 0), feeder_at=(1, 0), hammer_at=(0, 0))

        assert profile.allocations[Resource.CROP][ARMY].value == pytest.approx(0.0)
        assert profile.drawn_in[Resource.CROP] == [ARMY]
        assert profile.unmet[Resource.CROP] == pytest.approx(0.0)

    def test_a_material_sender_is_still_bounded_by_the_hub(self):
        """The hub IS where surplus materials go, so nothing moves for them.

        The feeder is 199 fields from the hub here, so its lumber cannot be
        shipped in the window whatever the crop consumer next door can take --
        proof the two resources are measured against different destinations
        rather than one standing in for both.
        """
        villages = [
            _village(HUB, "hub", 200, 0, lumber=-20_000.0, wh=1_200_000, gr=800_000),
            _village(ARMY, "feeder", 1, 0, lumber=20_000.0, gr=800_000),
            _village(FAR, "hammer", 0, 0, crop=-20_000.0, gr=800_000),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[FAR],
        )

        assert profile.allocations[Resource.LUMBER][ARMY].value == pytest.approx(20_000.0)
        assert profile.drawn_in[Resource.LUMBER] == []
        assert profile.unmet[Resource.LUMBER] == pytest.approx(20_000.0)

    def test_several_consumers_are_reduced_by_demand_and_not_by_the_nearest(self):
        """The aggregation over a destination SET, which one consumer cannot see.

        Every other test in this class ships to a single consumer, so `min`,
        `max` and a weighted mean all answer the same thing and the reduction
        was never exercised. Two consumers at different distances separate
        them.

        The hub makes 60,000/h with a 7,000/h granary ceiling and 8 shippable
        merchants carrying 2,500 each. A consumer 1 field away needs 100/h (a
        0h10 round trip, 48 turnarounds); one 40 fields away needs 40,000/h (a
        6h40 round trip, ONE turnaround). Weighted by demand the mean hop is
        (100 x 1 + 40,000 x 40) / 40,100 / 12 = 3.3252h, a 6h65 round trip --
        so one trip fits and the limit is 8 x 2,500 x 1 / 8 = 2,500/h.

        Reduced by the NEAREST it was 120,000/h, which bound nothing: the
        ceiling decided instead, the hub was booked to ship 53,000/h -- 21x
        what reaches the destination that needs it -- and the hammer's whole
        40,000/h deficit read as covered with `unmet` at 0.
        """
        villages = [
            _village(HUB, "hub", 0, 0, crop=60_000.0, to=0, merch=10),
            _village(ARMY, "near consumer", 1, 0, crop=-100.0, to=0, merch=10),
            _village(FAR, "far consumer", 40, 0, crop=-40_000.0, to=0, merch=10),
        ]
        profile = derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=HUB,
            consumer_ids=[ARMY, FAR],
        )

        assert profile.allocations[Resource.CROP][HUB].value == pytest.approx(57_500.0)
        # 40,100/h of demand against 2,500/h shippable, reported rather than
        # booked as covered.
        assert profile.unmet[Resource.CROP] == pytest.approx(37_600.0)
