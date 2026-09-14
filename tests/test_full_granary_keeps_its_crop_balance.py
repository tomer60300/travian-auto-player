"""A granary at its cap is a KNOWN state, not an unreadable one (#77).

The warehouse statistics page counts a granary down to full, or down to empty.
A granary already sitting at its cap has neither to count to, so the page states
no timer for it and the village is simply absent from that table.

`get_all_villages_net_crop` built its result by iterating those timers, so such
a village got no `CropBalance` at all. Two things followed, and the second cost
the whole account:

* its `crop_stock` was reported as **0** while the resources table plainly said
  80,000 -- the stock was known and thrown away with the rate;
* its `crop_per_hour` was `None`, so the planner dropped that village's crop
  allocation, and `/execute` refused to go live on a plan that was no longer the
  one the operator approved.

Found on the live account 2026-09-14: village 25 held 80,000 crop against an
80,000 granary and was the only village of 27 without a balance. It blocked a
run confined to village 27, seventy fields away.

The rate genuinely cannot be derived -- `derive_net_crop_per_hour` inverts the
countdown and there is no countdown -- so it stays None. What changes is that
the village is no longer mistaken for a failed read: its stock is recorded, and
a run that never touches it is no longer refused because of it.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.services.building_service import BuildingService, derive_net_crop_per_hour

FULL = 52984  # village 25 on the operator's account
DRAINING = 30540
CAP = 80_000


def _service(monkeypatch, *, stocks, timers, capacity):
    service = BuildingService(SimpleNamespace())

    # The warehouse page is always fetched and the capacity page whenever any
    # timer exists; both are then handed to parsers this test replaces, so the
    # HTML itself is never read and a marker string is enough.
    async def _page(*_a, **_k):
        return "<html>stubbed</html>"

    monkeypatch.setattr(service, "http_client", SimpleNamespace(get_html=_page))
    monkeypatch.setattr(
        "travian_api.services.building_service.parse_village_stats_warehouse",
        lambda _html: timers,
    )
    monkeypatch.setattr(
        "travian_api.services.building_service.parse_village_stats_capacity",
        lambda _html: capacity,
    )
    return service


def _run(service, stocks, granary):
    return asyncio.run(service.get_all_villages_net_crop(stocks=stocks, granary_capacity=granary))


class TestTheCountdownItselfCannotAnswerForAFullGranary:
    """The premise: this is a real limit, not one the fix pretends away."""

    def test_a_zero_countdown_derives_nothing(self):
        assert derive_net_crop_per_hour(stock=CAP, seconds_remaining=0, draining=False) is None

    def test_a_filling_granary_at_its_cap_would_derive_zero_from_nothing(self):
        """`(capacity - stock) / hours` is 0/0 here, which is why there is no timer."""
        assert (
            derive_net_crop_per_hour(
                stock=CAP, seconds_remaining=0, draining=False, granary_capacity=CAP
            )
            is None
        )


class TestAVillageAtItsCapKeepsWhatIsKnownAboutIt:
    STOCKS = {FULL: {"crop": CAP}, DRAINING: {"crop": 10_000}}
    TIMERS = {DRAINING: {"crop_seconds": 3600, "crop_draining": True, "crop_percent": 10}}
    CAPACITY = {
        FULL: {"granary": CAP, "warehouse": CAP},
        DRAINING: {"granary": CAP, "warehouse": CAP},
    }

    def _balances(self, monkeypatch):
        service = _service(
            monkeypatch, stocks=self.STOCKS, timers=self.TIMERS, capacity=self.CAPACITY
        )
        balances, _storage, _spent = _run(service, self.STOCKS, {FULL: CAP, DRAINING: CAP})
        return balances

    def test_it_is_no_longer_missing_entirely(self, monkeypatch):
        assert FULL in self._balances(monkeypatch)

    def test_its_stock_is_the_one_the_page_stated(self, monkeypatch):
        """0 was a plain error: the resources table said 80,000."""
        assert self._balances(monkeypatch)[FULL].stock == CAP

    def test_it_is_not_reported_as_draining(self, monkeypatch):
        """A full granary is the opposite of a starving one."""
        assert self._balances(monkeypatch)[FULL].draining is False

    def test_the_rate_stays_underived_rather_than_becoming_zero(self, monkeypatch):
        """The silent zero this module exists to avoid.

        Net is >= 0 for a full granary, but the exact figure is not knowable from
        a countdown that does not exist. Reporting 0 would invite the planner to
        ship crop INTO a granary that is already overflowing.
        """
        assert self._balances(monkeypatch)[FULL].net_per_hour is None

    def test_a_village_with_a_real_countdown_is_untouched(self, monkeypatch):
        drained = self._balances(monkeypatch)[DRAINING]

        assert drained.draining is True
        assert drained.net_per_hour == pytest.approx(-10_000.0)

    def test_a_village_below_its_cap_with_no_timer_is_still_unknown(self, monkeypatch):
        """The narrow claim. Absent-and-full is explained; absent-and-not-full
        is still a village whose page could not be read, and must not be
        invented into existence."""
        stocks = {FULL: {"crop": 1_000}}
        service = _service(monkeypatch, stocks=stocks, timers={}, capacity=self.CAPACITY)
        balances, _s, _r = _run(service, stocks, {FULL: CAP})

        assert FULL not in balances
