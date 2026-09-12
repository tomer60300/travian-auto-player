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


class TestTheServerClockIsNotUTC:
    """A row's departure is a unix timestamp; the schedule is in server time.

    `departure_at % 86400` answers in UTC. Every other minute the planner holds
    -- the create payload's `hour`/`minute`, the profile's `dispatch_window`,
    the night's 23:00-07:00 -- is in the GAME's clock. Where the server is not
    at UTC+00:00 the two differ by exactly its offset, and nothing bridged them.

    Measured on the operator's account (server UTC+01:00, issue #76): a route
    created for 23:00 every 4h was scheduled by Travian at 23:00, 03:00, 07:00,
    11:00, 15:00 and 19:00, and read back as 22:00, 02:00, 06:00, 10:00, 14:00
    and 18:00 -- every row exactly one hour early. Verification then matched
    none of the rows it had just created, the trim refused rather than delete
    all six, and the undo disowned the route. One number, consumed three times.

    The old docstring claimed no timezone was needed, on a live confirmation
    that returned 1410 for a 23:30 request. That reading is real but only holds
    where the offset is zero, which is why the class above still passes
    unchanged: it pins the UTC+00:00 case, and this one pins the rest.
    """

    # 22:00 UTC, which is 23:00 on a UTC+01:00 server.
    TWENTY_TWO_UTC = 1787695200

    def test_utc_is_still_the_answer_when_the_server_is_at_utc(self):
        assert minute_of_day(self.TWENTY_TWO_UTC, server_utc_offset_minutes=0) == 22 * 60

    def test_the_offset_moves_the_row_onto_the_server_clock(self):
        assert minute_of_day(self.TWENTY_TWO_UTC, server_utc_offset_minutes=60) == 23 * 60

    def test_it_wraps_past_midnight_rather_than_running_past_1440(self):
        # 23:30 UTC on a UTC+01:00 server is 00:30 the next day.
        assert minute_of_day(1787700600, server_utc_offset_minutes=60) == 30

    def test_a_western_server_moves_the_other_way(self):
        assert minute_of_day(self.TWENTY_TWO_UTC, server_utc_offset_minutes=-5 * 60) == 17 * 60

    def test_a_half_hour_offset_is_a_real_timezone(self):
        # Travian runs servers on offsets that are not whole hours.
        assert minute_of_day(self.TWENTY_TWO_UTC, server_utc_offset_minutes=330) == 3 * 60 + 30

    def test_no_departure_is_still_no_minute_whatever_the_offset(self):
        assert minute_of_day(None, server_utc_offset_minutes=60) is None


class TestTheNightRowsSurviveOnAUTCPlusOneServer:
    """The defect end to end, as the operator's account produced it.

    The six rows of a 4h route created for 23:00, against the 23:00-07:00 night
    window. Read as UTC they land 22:00/02:00/06:00/... -- so the two the plan
    means to keep are not the two that look kept, and `rows_outside_window`
    either prunes the wrong four or, as it did live, refuses outright.
    """

    NIGHT = (23 * 60, 7 * 60)
    # Departure timestamps for a route the GAME shows at 23:00, 03:00, 07:00,
    # 11:00, 15:00, 19:00 on a UTC+01:00 server -- i.e. one hour earlier in UTC.
    ROWS = [1787695200 + offset * 3600 for offset in range(0, 24, 4)]

    def _rows(self):
        return [_Row(route_id=700 + i, departure_at=at) for i, at in enumerate(self.ROWS)]

    def test_read_as_utc_the_window_keeps_the_wrong_rows(self):
        rows = self._rows()
        doomed = {r.route_id for r in rows_outside_window(rows, self.NIGHT)}
        kept = sorted(minute_of_day(r.departure_at) for r in rows if r.route_id not in doomed)
        # Read in UTC the survivors are 02:00 and 06:00 -- neither is a minute
        # the plan asked for, and the 23:00 firing it did ask for is deleted.
        assert kept == [2 * 60, 6 * 60]
        assert 23 * 60 not in kept

    def test_read_on_the_server_clock_the_right_two_survive(self):
        rows = self._rows()
        doomed = rows_outside_window(rows, self.NIGHT, server_utc_offset_minutes=60)
        doomed_ids = {r.route_id for r in doomed}
        kept = sorted(
            minute_of_day(r.departure_at, server_utc_offset_minutes=60)
            for r in rows
            if r.route_id not in doomed_ids
        )
        assert kept == [3 * 60, 23 * 60], "the 23:00 and 03:00 firings are the night's"
        assert len(doomed) == 4
