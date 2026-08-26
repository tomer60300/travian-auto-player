"""Choosing which rows of a fanned-out route to remove.

Travian has no setting that confines a route to part of the day, but it does not
need one: "repeat every N hours" is implemented as 24/N separate rows, each with
its own id and its own departure, and each individually deletable. Proven on the
live account -- a 1-hour route produced 24 rows, deleting one left 23.

So a windowed profile is enforced by subtraction. Create the route, then delete
the rows that depart outside the profile's hours. What remains fires only inside
them, which is exactly what the planner already sized the cargo for -- so the
model stops being a fiction, and the row footprint drops to the fraction of the
day the window covers.

The arithmetic needs no timezone. Also measured live: ``departure_at % 86400``
is the same minutes-past-midnight that the create payload's ``hour``/``minute``
and ``dispatch_window`` are already expressed in. A route asked to leave at 23:30
came back as 1410 minutes exactly.

Everything here is a pure function over rows the page already reported. No
requests, no clock -- because deciding to delete something is not a place for a
value read from the machine this happens to run on.
"""

import pytest

from travian_api.services.distribution.window_pruning import (
    minute_of_day,
    rows_outside_window,
)

NIGHT = (23 * 60, 7 * 60)  # 23:00 -> 07:00, wraps midnight
DAY = (7 * 60, 23 * 60)  # 07:00 -> 23:00, does not wrap


class _Row:
    """The fields of an ExistingRoute this decision actually reads."""

    def __init__(self, route_id, departure_at, dest_village_id=99):
        self.route_id = route_id
        self.departure_at = departure_at
        self.dest_village_id = dest_village_id


def _at(hour, minute=30):
    """A departure timestamp whose minute-of-day is `hour:minute`."""
    return 1787616000 + hour * 3600 + minute * 60


class TestMinuteOfDay:
    def test_it_matches_what_the_game_returned_for_a_2330_request(self):
        # The live measurement this whole approach rests on.
        assert minute_of_day(1787700600) == 23 * 60 + 30

    def test_consecutive_hourly_rows_are_an_hour_apart(self):
        assert minute_of_day(1787704200) == 30  # 00:30, the next day
        assert minute_of_day(1787707800) == 90  # 01:30

    def test_a_row_with_no_stated_departure_has_no_minute(self):
        assert minute_of_day(None) is None


class TestChoosingRowsToRemove:
    def test_rows_inside_a_wrapping_window_are_kept(self):
        rows = [_Row(1, _at(23)), _Row(2, _at(2)), _Row(3, _at(6))]
        assert rows_outside_window(rows, NIGHT) == []

    def test_rows_outside_it_are_named(self):
        keep, drop = _Row(1, _at(23)), _Row(2, _at(12))
        assert [r.route_id for r in rows_outside_window([keep, drop], NIGHT)] == [2]

    def test_the_full_hourly_fan_out_keeps_exactly_the_window(self):
        # The real case: 24 rows, an 8-hour window, 8 survivors.
        rows = [_Row(600 + h, _at(h)) for h in range(24)]
        doomed = rows_outside_window(rows, NIGHT)
        assert len(doomed) == 16
        assert len(rows) - len(doomed) == 8

    def test_a_non_wrapping_window_works_the_same_way(self):
        rows = [_Row(600 + h, _at(h)) for h in range(24)]
        assert len(rows) - len(rows_outside_window(rows, DAY)) == 16

    def test_the_window_boundary_is_inclusive_at_the_start_exclusive_at_the_end(self):
        # Matches how the beat already reads a window, so the rows kept are the
        # firings the plan counted -- not one more or one fewer.
        rows = [_Row(1, _at(23, 0)), _Row(2, _at(7, 0))]
        assert [r.route_id for r in rows_outside_window(rows, NIGHT)] == [2]


class TestItRefusesToGuess:
    def test_no_window_means_nothing_is_removed(self):
        # A round-the-clock profile wants every firing. Pruning here would delete
        # the route set the operator asked for.
        rows = [_Row(600 + h, _at(h)) for h in range(24)]
        assert rows_outside_window(rows, None) == []

    def test_a_row_with_an_unknown_departure_is_never_deleted(self):
        # Absent must not read as midnight. Erring toward keeping leaves a route
        # shipping outside its hours, which the plan reports; erring the other way
        # destroys a row for a reason that was never established.
        rows = [_Row(1, None), _Row(2, _at(12))]
        assert [r.route_id for r in rows_outside_window(rows, NIGHT)] == [2]

    def test_it_never_proposes_removing_every_row(self):
        # A window that somehow matched nothing would delete the whole route the
        # run just created. That is a bug in the caller, not an instruction.
        rows = [_Row(1, _at(12)), _Row(2, _at(13))]
        with pytest.raises(ValueError, match="every row"):
            rows_outside_window(rows, NIGHT)

    def test_an_empty_row_list_is_simply_empty(self):
        assert rows_outside_window([], NIGHT) == []
