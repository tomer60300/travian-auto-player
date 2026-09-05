"""Constants the frontend keeps its own copy of, pinned by a literal here.

Every value below exists twice: once in Python and once in
`frontend/src`. That is not a defect to tidy away -- the page has to know a
bound before it posts, or the operator types a figure and gets a pydantic 422
that names no control -- but a copy nothing asserts is a copy that has already
drifted. `MAX_TRADE_OFFICE_LEVEL`, `MAX_MERCHANTS_PER_VILLAGE`, the fill pair
and `READABLE_VERSIONS` were already pinned this way on both sides; these are
the ones that were not.

The sharpest is `DEFAULT_MERCHANT_MODEL`. The page seeds from its own constant
and sends **all four figures explicitly**, so changing a default here does not
change what the account plans with -- the old value keeps arriving in the
request body. Those four numbers size every cargo on the account, and 2,500 only
recently stopped being 2,200.

Each test names its twin by symbol and file. A literal, deliberately: asserting
one side against the other's value would pass however far both had drifted from
what the game does.
"""

import pytest

from travian_api.services.distribution.merchants import DAILY_BEAT_CYCLES, EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import (
    DEFAULT_MERCHANT_HEADROOM,
    DEFAULT_MERCHANT_RESERVE,
)
from travian_api.web.routes.distribution import (
    MAX_DAY_SEGMENTS,
    MAX_STOCK_FLOOR_FRACTION,
    ExecuteRequest,
    ForeignTarget,
    NightProfileRequest,
    VillageConfig,
)


def _bound(model, field, key):
    """One declared bound off a pydantic field, or None."""
    for meta in model.model_fields[field].metadata:
        if hasattr(meta, key):
            return getattr(meta, key)
    return None


class TestTheFourMerchantFiguresTheFrontendSendsExplicitly:
    """Twin: `DEFAULT_MERCHANT_MODEL` in `frontend/src/utils/plannerSetup.js`.

    `{base_capacity: 2500, bonus_per_to_level: 0.2, merchant_reserve: 2,
    merchant_headroom: 0.1}` -- and the page sends all four in every plan
    request, so a change made only here is silently overridden with the old
    value.
    """

    def test_the_base_capacity_is_two_thousand_five_hundred(self):
        # Re-read off the game on 2026-09-02. The superseded 2,200 fitted an
        # earlier reading and is not reconciled with this one.
        assert EUROPE2_TEUTON.base_capacity == 2500

    def test_the_trade_office_bonus_is_a_fifth_per_level(self):
        assert EUROPE2_TEUTON.bonus_per_trade_office_level == 0.2

    def test_the_merchant_reserve_is_two(self):
        assert DEFAULT_MERCHANT_RESERVE == 2

    def test_the_merchant_headroom_is_a_tenth(self):
        assert DEFAULT_MERCHANT_HEADROOM == 0.1


class TestTheRepeatIntervalsTravianOffers:
    """Twin: `TRAVIAN_REPEAT_INTERVALS` in
    `frontend/src/pages/ResourcePlanner.jsx`, which fills the cadence dropdown.

    The divisors of 24, because a schedule is only expressible as a daily beat
    if the cycle divides the day. A value on the page that is not here is a
    cadence the request refuses; one here that is not on the page is a cadence
    the operator can never choose.
    """

    def test_the_cycles_are_the_divisors_of_a_day(self):
        assert DAILY_BEAT_CYCLES == (1, 2, 3, 4, 6, 8, 12, 24)


class TestTheBoundsThePageHasToKnowBeforeItPosts:
    """Twins in `frontend/src/utils/plannerSetup.js` and `ResourcePlanner.jsx`.

    Each of these is an attribute-level bound: exceeded, the request answers 422
    naming a field the operator cannot see. The page therefore carries its own
    copy so it can mark the cell, and the copy has to agree.
    """

    def test_a_day_holds_twelve_profiles(self):
        # Twin: `MAX_DAY_SEGMENTS` in plannerSetup.js.
        assert MAX_DAY_SEGMENTS == 12

    def test_the_stock_floor_ceiling_is_ninety_five_percent(self):
        # Twin: `MAX_STOCK_FLOOR_FRACTION` in plannerSetup.js. It was two bare
        # `le=0.95` literals in unrelated field definitions before this.
        assert MAX_STOCK_FLOOR_FRACTION == 0.95

    def test_both_fields_that_carry_it_carry_the_constant(self):
        assert _bound(VillageConfig, "stock_floor_fraction", "le") == MAX_STOCK_FLOOR_FRACTION
        assert _bound(NightProfileRequest, "baseline_fill", "le") == MAX_STOCK_FLOOR_FRACTION

    def test_a_run_may_create_fifty_routes_at_most(self):
        assert _bound(ExecuteRequest, "max_routes_per_run", "le") == 50

    def test_a_run_may_leave_two_thousand_rows_at_most(self):
        assert _bound(ExecuteRequest, "max_game_rows_per_run", "le") == 2000

    def test_a_safety_margin_is_a_percentage(self):
        assert _bound(ForeignTarget, "safety_margin_pct", "le") == 100

    def test_a_store_may_be_assumed_full_at_dawn_but_not_beyond(self):
        assert _bound(NightProfileRequest, "target_fill", "le") == 1.0
        assert _bound(NightProfileRequest, "target_fill", "gt") == 0.0


class TestTheBoundsAreLiveAndNotJustDeclared:
    """The pins above read the declaration; these prove it bites.

    A bound asserted only through `model_fields` would survive the field being
    replaced by one that validates nothing.
    """

    @pytest.mark.parametrize(
        ("field", "value"),
        [("max_routes_per_run", 51), ("max_game_rows_per_run", 2001)],
    )
    def test_a_run_control_past_its_ceiling_is_refused(self, field, value):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ExecuteRequest.model_validate({"snapshot": [], field: value})

    def test_a_safety_margin_past_a_hundred_percent_is_refused(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ForeignTarget(name="ally", x=1, y=2, crop_per_hour=100, safety_margin_pct=101)

    def test_a_store_floor_past_the_ceiling_is_refused(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VillageConfig(village_id=1, stock_floor_fraction=MAX_STOCK_FLOOR_FRACTION + 0.01)
