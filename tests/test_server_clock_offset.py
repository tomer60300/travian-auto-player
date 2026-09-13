"""Reading the game server's UTC offset off the page it is stated on.

Every minute the planner holds is in the GAME's clock -- the create payload's
`hour`/`minute`, the profile's `dispatch_window`, the night's 23:00-07:00. A
route row's departure is a unix timestamp, so turning one into a minute-of-day
needs that offset, and #76 is what happens without it: on a UTC+01:00 server
every row read an hour early, verification matched none of the rows it had just
created, the trim refused, and the undo disowned the route.

The offset is NOT this machine's. The operator's host is UTC+03:00 while the
server is UTC+01:00, so `datetime.now().astimezone()` would be wrong by two
hours in the other direction. It is not a constant either -- daylight saving
moves it twice a year.

Travian states it outright, in the block it hands its own JavaScript:

    Travian.Game.timestamp = 1789303251;
    Travian.Game.timeZone = "Europe/London";
    Travian.Game.timezoneOffsetToUTC = -3600;

Note the SIGN. The field is the offset *to* UTC -- what you add to server time
to get UTC -- so a server one hour ahead states -3600. The planner wants the
opposite convention (minutes the server runs ahead), which is why this is not a
straight division.
"""

import pytest

from travian_api.parsers.html_parser import parse_server_utc_offset_minutes

# The real block, verbatim from ts2.x1.europe.travian.com on 2026-09-13.
LIVE_PAGE = """
<script type="application/javascript">
    Travian.Game.language = "en-US";
    Travian.Game.timestamp = 1789303251;
    Travian.Game.timeZone = "Europe/London";
    Travian.Game.timezoneOffsetToUTC = -3600;
    Travian.Game.timeFormat = 0;
</script>
<div id="servertime">
    <span>Server time:</span>
    <span><span format="24h" class="timer" counting="up" value="1789303251">13:40:51</span>&nbsp;(UTC&nbsp;+01:00)</span>
</div>
"""


class TestTheOffsetIsReadFromTheGame:
    def test_the_operators_own_server_reads_plus_one_hour(self):
        assert parse_server_utc_offset_minutes(LIVE_PAGE) == 60

    def test_a_server_at_utc_reads_zero(self):
        assert parse_server_utc_offset_minutes("Travian.Game.timezoneOffsetToUTC = 0;") == 0

    def test_a_western_server_reads_negative(self):
        # UTC-05:00 states +18000: add five hours to server time to reach UTC.
        assert parse_server_utc_offset_minutes("Travian.Game.timezoneOffsetToUTC = 18000;") == -300

    def test_a_half_hour_timezone_survives(self):
        # UTC+05:30 states -19800. Travian runs servers on such offsets.
        assert parse_server_utc_offset_minutes("Travian.Game.timezoneOffsetToUTC = -19800;") == 330

    @pytest.mark.parametrize(
        "spacing",
        [
            "Travian.Game.timezoneOffsetToUTC=-3600;",
            "Travian.Game.timezoneOffsetToUTC  =  -3600 ;",
            "Travian.Game.timezoneOffsetToUTC = -3600",
        ],
    )
    def test_it_does_not_depend_on_whitespace(self, spacing):
        assert parse_server_utc_offset_minutes(spacing) == 60


class TestAnUnreadableOffsetIsNoneRatherThanZero:
    """None is a real answer. Zero would be a claim the server is at UTC, and
    acting on that guess is exactly the defect -- it is what the conversion did
    implicitly before it took the argument at all. A caller that cannot read the
    offset must decline to prune, not prune against a guess."""

    def test_a_page_without_the_constant(self):
        assert parse_server_utc_offset_minutes("<html><body>no clock here</body></html>") is None

    def test_an_empty_page(self):
        assert parse_server_utc_offset_minutes("") is None

    def test_a_value_that_is_not_a_number(self):
        assert parse_server_utc_offset_minutes("Travian.Game.timezoneOffsetToUTC = soon;") is None

    def test_an_offset_beyond_any_real_timezone_is_refused(self):
        # Earth's offsets run UTC-12:00 to UTC+14:00. Anything outside that is a
        # parse that latched onto the wrong number, not a timezone.
        assert parse_server_utc_offset_minutes("Travian.Game.timezoneOffsetToUTC = -86400;") is None
