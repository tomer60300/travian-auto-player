"""The row-clock diagnostic reads both clocks off one page and compares them.

#76 turns on whether `departure_at % 86400` is already the minute the create
payload asked for, or a UTC minute an hour behind it. The repository carried one
claim in comments and a live measurement saying the opposite, and getting the
sign wrong either deletes live rows or recreates correct routes on every run.

The question is settled by reading the server's OWN "now" and a row's departure
off the same page: if the page's epoch and the clock it renders disagree by an
hour, every epoch on that page is on the other clock, and `departureAt` is one
of them. This pins that arithmetic so the answer cannot drift.

Measured against the live account 2026-09-13 with exactly these fields:
`Travian.Game.timestamp = 1789312427` (15:13 UTC) rendered as 16:13 server time,
`timezoneOffsetToUTC = -3600`, and village 24's tribute rows reading 23:30 UTC
against 00:30 on the game's clock.
"""

from travian_api.parsers.html_parser import parse_server_utc_offset_minutes
from travian_api.services.distribution.window_pruning import minute_of_day

# The two lines the diagnostic keys on, verbatim in the shape the game emits.
PAGE = """
<script type="application/javascript">
    Travian.Game.timestamp = 1789312427;
    Travian.Game.timeZone = "Europe/London";
    Travian.Game.timezoneOffsetToUTC = -3600;
</script>
"""

PAGE_NOW = 1789312427
# Village 24's first tribute row to 01 Ariados, as the live page stated it.
TRIBUTE_ROW = 1788478200


class TestTheServerClockIsAnHourAheadOfThePageEpoch:
    def test_the_offset_is_read_as_plus_sixty(self):
        """The page states the offset TO UTC; the planner wants it FROM UTC."""
        assert parse_server_utc_offset_minutes(PAGE) == 60

    def test_the_page_epoch_is_utc_not_server_time(self):
        """1789312427 is 15:13 UTC. The operator's own clock read 16:13.

        This is the whole argument in one assertion: the number the page
        publishes as "now" is an hour behind the clock it shows the operator, so
        every epoch on the page is UTC.
        """
        assert minute_of_day(PAGE_NOW) == 15 * 60 + 13
        assert minute_of_day(PAGE_NOW, server_utc_offset_minutes=60) == 16 * 60 + 13

    def test_a_row_departure_shifts_by_exactly_one_hour(self):
        """Read raw it is 23:30; read on the game's clock it is 00:30.

        Which matters more than an hour of arithmetic suggests: the two land on
        opposite sides of midnight, so they fall in DIFFERENT days and, for any
        window anchored near midnight, on opposite sides of the window too.
        """
        assert minute_of_day(TRIBUTE_ROW) == 23 * 60 + 30
        assert minute_of_day(TRIBUTE_ROW, server_utc_offset_minutes=60) == 30

    def test_the_operators_own_rows_are_midnight_anchored_only_on_the_game_clock(self):
        """Corroboration from how a human actually configures a route.

        Village 24 sends tribute to 01 Ariados on a three-hourly series. Read
        raw, that series is anchored at 23:30 -- an odd hour to pick. Read on
        the game's clock it is anchored at 00:30 and every row lands on a clean
        three-hour step from midnight, which is what someone typing a send time
        would choose. The arithmetic and the human agree.
        """
        series = [TRIBUTE_ROW + hours * 3600 for hours in range(0, 24, 3)]
        on_game_clock = [minute_of_day(e, server_utc_offset_minutes=60) for e in series]

        assert on_game_clock[0] == 30, "anchored at 00:30, not 23:30"
        assert all(m % 30 == 0 for m in on_game_clock)
        assert sorted(on_game_clock) == [30 + 180 * i for i in range(8)]

    def test_the_shift_never_runs_off_the_end_of_the_day(self):
        """Midnight wrap, in both directions, because 23:30 + 1h is the case."""
        for epoch in (TRIBUTE_ROW, PAGE_NOW, 0, 86_399):
            for offset in (-720, -60, 0, 60, 840):
                minute = minute_of_day(epoch, server_utc_offset_minutes=offset)
                assert 0 <= minute < 1440, (epoch, offset, minute)
