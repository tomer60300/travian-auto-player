"""The matching rules, tested directly for the first time.

Every reconciliation bug this week -- destination-only matching, per-key
satisfaction, the trim leak, double-counted rows -- lived inside a helper buried
at nesting depth 5 in post_execute's per-village loop, reachable only by
standing up the whole endpoint with a fake service. So each one was found late,
by an endpoint-level test that had to be constructed to reproduce it.

The helpers are pure and now module-level, so their rules can be stated as
facts. Two kinds of key exist on purpose and must not be unified: an OWN village
matches by village id (the page states it), while a FOREIGN target has no id in
the plan -- it is an operator-supplied coordinate with a synthetic negative id
-- so it can only match on coordinates, which are back-derived through the
world's span. Keying everything on coordinates churns every own-village route
whenever the span is wrong; keying everything on ids churns every foreign one
always.
"""

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import ExistingRoute, PlannedRoute
from travian_api.web.routes.distribution import (
    _desired_key,
    _existing_keys,
    _identifiable,
    _is_protected,
    _is_wanted,
    _off_schedule,
    _planned_minutes,
    _row_minute,
)

_EPOCH_DAY = 1787616000  # midnight UTC, so departure_at % 86400 is the minute


def _planned(dest_id, x=40, y=40, *, cycle=1, minute=0, window=None):
    return PlannedRoute(
        origin_village_id=20003,
        dest_village_id=dest_id,
        dest_x=x,
        dest_y=y,
        dest_name="dest",
        cargo={Resource.CROP: 1000},
        cycle_hours=cycle,
        merchants=1,
        dispatch_minute=minute,
        window=window,
    )


def _live(dest_id, x=40, y=40, *, minute=None, route_id=1):
    return ExistingRoute(
        route_id=route_id,
        dest_village_id=dest_id,
        dest_x=x,
        dest_y=y,
        departure_at=None if minute is None else _EPOCH_DAY + minute * 60,
    )


class TestHowARouteIsRecognised:
    def test_an_own_village_is_keyed_by_its_id(self):
        assert _desired_key(_planned(20011, 10, 0)) == 20011

    def test_a_foreign_target_is_keyed_by_coordinates(self):
        # Negative id = an operator-supplied tribute; the plan holds no real id
        # for it, so coordinates are the only handle that exists.
        assert _desired_key(_planned(-1, 46, 133)) == (46, 133)

    def test_a_live_row_offers_both_keys(self):
        assert _existing_keys(_live(20011, 10, 0)) == {20011, (10, 0)}

    def test_an_unplaceable_row_offers_only_its_id(self):
        # dest_x/dest_y are None when the map id could not be placed for this
        # world span. Contributing a coordinate key here would invent a match.
        assert _existing_keys(_live(20011, None, None)) == {20011}

    def test_an_int_key_and_a_tuple_key_cannot_collide(self):
        own = _existing_keys(_live(20011, 10, 0))
        assert 20011 in own and (10, 0) in own
        assert not {20011} & {(10, 0)}


class TestWhetherTheRulesCanJudgeARouteAtAll:
    def test_a_matching_id_is_always_identifiable(self):
        assert _identifiable(_live(20011), {20011}, set())

    def test_an_unplaceable_row_is_unjudgeable_when_the_plan_wants_coordinates(self):
        """The churn bug this exists for: judged "not wanted", the reconciler
        disabled it and created a replacement, every single run. The honest
        answer is "I do not know", and the safe action is to leave it alone."""
        row = _live(99999, None, None)

        assert not _identifiable(row, {20011}, {(46, 133)})

    def test_the_same_row_is_judgeable_when_no_foreign_target_exists(self):
        # With nothing matched by coordinates, an unplaceable row's id is the
        # whole question -- and it can be answered.
        assert _identifiable(_live(99999, None, None), {20011}, set())

    def test_wanted_by_id_and_by_coordinates(self):
        assert _is_wanted(_live(20011, 10, 0), {20011}, set())
        assert _is_wanted(_live(-5, 46, 133), set(), {(46, 133)})
        assert not _is_wanted(_live(77777, 99, 98), {20011}, {(46, 133)})

    def test_protection_uses_the_same_two_keys(self):
        # A hand-made route to a tribute has no usable village id, so naming it
        # by coordinates has to work.
        assert _is_protected(_live(-5, 46, 133), set(), {(46, 133)})
        assert _is_protected(_live(20011, 10, 0), {20011}, set())
        assert not _is_protected(_live(20011, 10, 0), {20012}, {(1, 1)})


class TestWhichMinutesARouteWillDepartAt:
    def test_an_hourly_route_fans_across_the_whole_day(self):
        assert _planned_minutes(_planned(20011, cycle=1, minute=0)) == list(range(0, 1440, 60))

    def test_a_windowed_route_keeps_only_its_own_hours(self):
        # 1h cycle from 23:00 in a 23:00-07:00 window: 8 survivors.
        minutes = _planned_minutes(_planned(20011, cycle=1, minute=23 * 60, window=(1380, 420)))

        assert minutes == sorted([1380, 1440 % 1440, 60, 120, 180, 240, 300, 360])
        assert len(minutes) == 8

    def test_a_day_window_keeps_the_complement(self):
        minutes = _planned_minutes(_planned(20011, cycle=4, minute=8 * 60, window=(420, 1380)))

        assert minutes == [480, 720, 960, 1200]

    def test_a_route_with_no_in_window_departure_keeps_them_all(self):
        """window_pruning refuses to delete every row -- a route pruned to
        nothing would be a route the run just created and destroyed. So the
        planned set falls back to the full fan-out rather than going empty."""
        # 24h cycle at 12:00 against a night window: nothing lands inside.
        minutes = _planned_minutes(_planned(20011, cycle=24, minute=720, window=(1380, 420)))

        assert minutes == [720]

    @pytest.mark.parametrize("cycle,expected", [(1, 24), (2, 12), (4, 6), (8, 3), (24, 1)])
    def test_the_fan_out_is_24_over_n(self, cycle, expected):
        assert len(_planned_minutes(_planned(20011, cycle=cycle))) == expected


class TestReadingALiveRowsDepartureTime:
    def test_a_departure_becomes_its_minute_of_the_day(self):
        # Measured against the real game: a 23:30 request came back as 1410.
        assert _row_minute(_live(20011, minute=1410)) == 1410

    def test_an_unknown_departure_can_never_match_a_planned_minute(self):
        """-1 is deliberate, not a null: it must compare unequal to every real
        minute so a row whose time could not be read reconciles by recreation
        rather than by trust."""
        assert _row_minute(_live(20011, minute=None)) == -1
        assert -1 not in _planned_minutes(_planned(20011, cycle=1))


class TestWhichRowsBelongToADivergedDestination:
    def test_a_row_of_a_mismatched_destination_is_off_schedule(self):
        assert _off_schedule(_live(20011, 10, 0), {20011: "why"})

    def test_a_row_of_an_agreeing_destination_is_not(self):
        assert not _off_schedule(_live(20012, 0, 10), {20011: "why"})

    def test_a_foreign_destination_matches_by_coordinates(self):
        assert _off_schedule(_live(-5, 46, 133), {(46, 133): "why"})

    def test_nothing_is_off_schedule_when_nothing_diverged(self):
        assert not _off_schedule(_live(20011, 10, 0), {})
