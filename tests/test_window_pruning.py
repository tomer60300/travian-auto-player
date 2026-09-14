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

from travian_api.services.distribution.window_pruning import in_window, minute_of_day

NIGHT = (23 * 60, 7 * 60)  # 23:00 -> 07:00, wraps midnight
DAY = (7 * 60, 23 * 60)  # 07:00 -> 23:00, does not wrap


class TestMinuteOfDay:
    def test_it_matches_what_the_game_returned_for_a_2330_request(self):
        # The live measurement this whole approach rests on.
        assert minute_of_day(1787700600) == 23 * 60 + 30

    def test_consecutive_hourly_rows_are_an_hour_apart(self):
        assert minute_of_day(1787704200) == 30  # 00:30, the next day
        assert minute_of_day(1787707800) == 90  # 01:30

    def test_a_row_with_no_stated_departure_has_no_minute(self):
        assert minute_of_day(None) is None


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
    """The defect end to end, as the operator's account produced it (#76).

    Six rows of a 4h route the GAME shows at 23:00, 03:00, 07:00, 11:00, 15:00,
    19:00 on a UTC+01:00 server, against the 23:00-07:00 night window. Read as
    UTC they are an hour earlier, so the firings that look like the night's are
    not the ones the plan asked for -- and the executor, which matches a live row
    to a planned minute, matches none of them.

    Asserted against `minute_of_day` and `in_window` directly. The rows-to-delete
    decision itself lives in the executor, which does more than a window test:
    see this module's docstring.
    """

    NIGHT = (23 * 60, 7 * 60)
    ROWS = [1787695200 + offset * 3600 for offset in range(0, 24, 4)]

    def _inside(self, offset):
        return sorted(
            minute
            for at in self.ROWS
            if in_window(
                (minute := minute_of_day(at, server_utc_offset_minutes=offset)), self.NIGHT
            )
        )

    def test_read_as_utc_the_window_keeps_the_wrong_rows(self):
        """02:00 and 06:00 survive -- neither is a minute the plan asked for,
        and the 23:00 firing it did ask for is not among them."""
        kept = self._inside(0)

        assert kept == [2 * 60, 6 * 60]
        assert 23 * 60 not in kept

    def test_read_on_the_server_clock_the_right_two_survive(self):
        kept = self._inside(60)

        assert kept == [3 * 60, 23 * 60], "the 23:00 and 03:00 firings are the night's"

    def test_the_two_readings_disagree_about_four_of_the_six_rows(self):
        """The size of the error, stated: it is not a near miss."""
        utc = {minute_of_day(at) for at in self.ROWS if in_window(minute_of_day(at), self.NIGHT)}
        game = {
            minute_of_day(at, server_utc_offset_minutes=60)
            for at in self.ROWS
            if in_window(minute_of_day(at, server_utc_offset_minutes=60), self.NIGHT)
        }
        assert utc.isdisjoint(game), "not one row is kept by both readings"
