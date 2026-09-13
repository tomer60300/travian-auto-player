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

from types import SimpleNamespace

from travian_api.services.distribution.window_pruning import in_window
from travian_api.services.trade_route_service import ExistingRoute, TradeRouteService
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

    def test_a_row_with_no_clock_context_falls_back_rather_than_inventing_one(self):
        """A route built without a page behind it -- in practice, in a test.

        It reads unshifted, which is what this code did everywhere before the
        offset existed, so no existing caller changes meaning by accident.
        """
        assert _row_minute(self._row(departure_at=TRIBUTE_ROW)) == 23 * 60 + 30
