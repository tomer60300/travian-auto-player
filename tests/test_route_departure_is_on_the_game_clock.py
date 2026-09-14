"""A row's schedule is read on the GAME's clock, not on UTC (#76).

Travian's pages publish epochs in UTC and show the operator a clock an hour
ahead of them (this server is UTC+1). The "send at HH:MM" in a create payload is
on the clock the operator sees. So `departure_at % 86400` names a different
minute from the one the route was asked for -- and for a row near midnight, a
different DAY.

Both failure directions are expensive and neither is loud:

* the reconciler compares a live row's minute against the plan's, so a uniform
  hour of error makes EVERY row look off-schedule and the executor recreates
  routes that were already correct, on every run;
* the window trim deletes the rows that fall outside a profile's hours, and an
  hour of error at a boundary deletes the wrong ones.

`TradeRouteService` learns the offset from any page that states it and stamps
each row's `departure_minute` at the moment it reads it. `departure_at` stays a
true UTC epoch, so anything echoing it stays honest; `departure_minute` is what
every schedule decision reads.
"""

import asyncio
import re
from types import SimpleNamespace

import pytest

from travian_api.services.distribution.window_pruning import in_window
from travian_api.services.trade_route_service import (
    ExistingRoute,
    MarketplaceUnreadable,
    TradeRouteService,
)
from travian_api.web.routes.distribution import _row_minute

# Village 24's first tribute row to 01 Ariados, exactly as the live page stated
# it on 2026-09-13, alongside that page's own clock.
TRIBUTE_ROW = 1_788_478_200
SERVER_OFFSET = 60


def _service() -> TradeRouteService:
    return TradeRouteService(SimpleNamespace())


class TestTheServiceStampsTheGameClockMinute:
    def test_an_unknown_offset_is_not_reported_as_midnight(self):
        """Before any page has been read, the answer is "I do not know".

        Zero would be a real minute and a wrong one -- the same reason
        `minute_of_day` returns None for a missing departure rather than 0.
        """
        service = _service()
        assert service.server_utc_offset_minutes is None
        assert service._departure_minute(TRIBUTE_ROW) is None
        assert service._departure_minute(None) is None

    def test_once_the_offset_is_known_the_minute_moves_with_it(self):
        service = _service()
        service.server_utc_offset_minutes = SERVER_OFFSET

        # 23:30 UTC is 00:30 on the game's clock -- the far side of midnight.
        assert service._departure_minute(TRIBUTE_ROW) == 30

    def test_a_utc_reading_and_a_game_reading_disagree_about_the_day(self):
        """Why an hour matters more than an hour.

        A Night window that opens at 23:00 contains the raw reading and the
        shifted one alike, but a window that CLOSES at 23:45 contains only the
        raw one -- so the two readings disagree about whether this row should be
        kept or deleted.
        """
        service = _service()
        service.server_utc_offset_minutes = SERVER_OFFSET
        on_game_clock = service._departure_minute(TRIBUTE_ROW)
        on_utc = (TRIBUTE_ROW % 86_400) // 60

        closing = (23 * 60, 23 * 60 + 45)
        assert in_window(on_utc, closing) is True
        assert in_window(on_game_clock, closing) is False


class TestTheReconcilerReadsTheStampedMinute:
    def _row(self, **kw) -> ExistingRoute:
        return ExistingRoute(
            route_id=1,
            dest_village_id=2,
            dest_x=None,
            dest_y=None,
            visible=True,
            active=True,
            cargo=None,
            **kw,
        )

    def test_the_stamped_minute_wins_over_the_raw_epoch(self):
        row = self._row(departure_at=TRIBUTE_ROW, departure_minute=30)

        assert _row_minute(row) == 30, "the game-clock minute, not 23:30 UTC"

    def test_a_row_with_no_departure_at_all_still_reconciles_by_recreation(self):
        """-1 can never equal a planned minute, which is the point of it."""
        assert _row_minute(self._row(departure_at=None)) == -1

    def test_a_row_with_no_clock_context_is_UNKNOWN_not_utc(self):
        """The fallback that used to live here was wrong, and dangerously so.

        It read the epoch unshifted -- i.e. assumed UTC -- on the belief that a
        row without clock context could only be one built in a test. A real
        `list_existing_routes` leaves the stamp None whenever the page's clock
        assignment is missing or out of range, so the fallback quietly restored
        the very comparison #76 is about, on live data.

        -1 never equals a planned minute. Nothing is written on it either: the
        service refuses the read outright (see
        `TestAPageThatWillNotStateItsClockIsRefused`), so this is the second
        line of defence rather than the first.
        """
        assert _row_minute(self._row(departure_at=TRIBUTE_ROW)) == -1


class TestAPageThatWillNotStateItsClockIsRefused:
    """The first line of defence: refuse the READ, before any write decision.

    `_row_minute` returning -1 is safe but not sufficient -- -1 means "recreate
    this route", which is itself a write, taken because a clock could not be
    read. So the read refuses instead, and only once the page has proved it is
    this village's marketplace: an unreadable page has a better story to tell.

    Driven by the real captured fixture rather than a hand-written model, with
    the clock line removed for the negative case, so the two runs differ in
    exactly one thing.
    """

    FIXTURE = "tests/fixtures/marketplace_trade_routes.html"
    VILLAGE = 20002  # the fixture states this as currentVillageId

    def _html(self):
        with open(self.FIXTURE, encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def _service(self, html):
        service = TradeRouteService(SimpleNamespace())

        async def _page(*_a, **_k):
            return html

        service.open_marketplace = _page
        return service

    def test_the_fixture_states_its_clock(self):
        """The premise. If the fixture loses it, the next case proves nothing."""
        assert "timezoneOffsetToUTC" in self._html()

    def test_the_same_page_without_that_line_is_refused(self):
        stripped = re.sub(r"Travian\.Game\.timezoneOffsetToUTC\s*=\s*-?\d+;", "", self._html())
        service = self._service(stripped)

        with pytest.raises(MarketplaceUnreadable, match="clock"):
            asyncio.run(service.list_existing_routes(self.VILLAGE))

    def test_with_the_line_the_rows_are_read_and_stamped(self):
        service = self._service(self._html())
        rows = asyncio.run(service.list_existing_routes(self.VILLAGE))

        assert service.server_utc_offset_minutes == 60
        assert rows, "the fixture has routes"
        stamped = [r for r in rows if r.departure_at is not None]
        assert stamped, "and they state departures"
        for row in stamped:
            assert row.departure_minute == (row.departure_at // 60 + 60) % 1440

    def test_a_remembered_offset_carries_a_page_that_omits_it(self):
        """The offset is a property of the SERVER, not of one page."""
        stripped = re.sub(r"Travian\.Game\.timezoneOffsetToUTC\s*=\s*-?\d+;", "", self._html())
        service = self._service(stripped)
        service.server_utc_offset_minutes = 60

        rows = asyncio.run(service.list_existing_routes(self.VILLAGE))
        assert any(r.departure_minute is not None for r in rows)
