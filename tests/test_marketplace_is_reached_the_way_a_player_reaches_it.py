"""How the executor gets to a marketplace, measured against a real session.

From the 2026-09-15 capture -- 1,631 requests, one player, five villages. Every
document load in it that touched a marketplace:

    1004  +3.1s  /dorf2.php
    1017  +1.4s  /build.php?id=30&gid=17                  <- from dorf2
    1030  +2.3s  /build.php?id=30&gid=17&t=1              <- tab click
    1070  +4.5s  /build.php?id=30&gid=17&t=2
    1097  +4.4s  /build.php?id=30&gid=17&t=2&newdid=64215 <- village switch
    1098  +0.1s  /build.php?id=21&gid=17&t=2&newdid=64215 <- corrected slot
    1132  +7.0s  /build.php?id=21&gid=17&t=2&newdid=68739
    1133  +0.1s  /build.php?id=31&gid=17&t=2&newdid=68739
    1163  +3.1s  /build.php?id=31&gid=17&t=2&newdid=59564 <- no correction: same slot
    1586  +4.2s  /build.php?id=31&gid=17&t=3&newdid=71828
    1587  +0.1s  /build.php?id=30&gid=17&t=3&newdid=71828
    1611 +12.5s  /build.php?id=30&gid=17&t=3&newdid=81499
    1612  +0.2s  /build.php?id=31&gid=17&t=3&newdid=81499

Three facts in that, and we had all three wrong.

**The URL names the SLOT.** ``id=30``, ``id=21``, ``id=31`` -- five villages,
three different slots, because where a village keeps its marketplace is a fact
about that village. We sent ``gid=17`` alone, which is the address of a building
TYPE: the shape a script writes when it knows what it wants but has never looked
at the village. No in-game link produces it.

**A village switch does not pass through dorf2.** Not once in four. The player
stays on the tab and the selector reloads it under a new ``newdid``. We walked
to the village overview before every single origin -- a page load no player
makes, announcing a switch that had already happened.

**The switch is usually TWO loads, not one.** The reloaded URL carries the
previous village's slot; the game corrects it a tenth of a second later. Line
1163 is the control: same slot in both villages, so nothing to correct, and the
client emits nothing extra.
"""

import asyncio
import re
from types import SimpleNamespace

from travian_api.services.trade_route_service import TradeRouteService

from .test_trade_route_footprint import empty_marketplace, village_view


class _Client:
    """Records GETs and answers them by shape, not by queue position.

    A queue would not survive these tests: the point of several is that the
    number of requests VARIES, so the fixture cannot assume which page the nth
    call wants. It answers a dorf2 with that village's village view and anything
    else with that village's marketplace.
    """

    def __init__(self, slots: dict[int, int]):
        self._slots = slots
        self.calls: list[str] = []
        self.referers: list[str | None] = []
        self.settings = SimpleNamespace(base_url="https://example.invalid")
        self.human_delay = SimpleNamespace(wait=self._wait)
        self.activity_scheduler = SimpleNamespace(log_activity=lambda _s: None)
        # The session's real last page, which is what `_walk_to_marketplace`
        # consults -- not the service's own note of where it thinks it is. A
        # fake that skipped this would let the service claim a Referer the wire
        # would not support, which is the exact divergence the property guards.
        self.browser_headers = SimpleNamespace(last_page_path=None)

    async def _wait(self, _action, reason=""):
        return None

    def _village(self, path: str) -> int:
        asked = re.search(r"newdid=(\d+)", path)
        return int(asked.group(1)) if asked else 0

    async def get_html(self, path, **kw):
        self.calls.append(path)
        self.referers.append(kw.get("referer"))
        self.browser_headers.last_page_path = path
        village = self._village(path)
        if path.startswith("/dorf2.php"):
            return village_view(self._slots[village])
        return empty_marketplace(village)


def _service(slots: dict[int, int]):
    client = _Client(slots)
    return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client


def _read(service, village: int) -> None:
    asyncio.run(service.list_existing_routes(village))


class TestTheUrlNamesTheSlot:
    def test_the_slot_comes_from_the_village_view_we_already_load(self):
        service, client = _service({20003: 26})
        _read(service, 20003)

        assert client.calls == [
            "/dorf2.php?newdid=20003",
            "/build.php?id=26&gid=17&t=3&newdid=20003",
        ]

    def test_it_costs_no_extra_request_to_learn(self):
        # The village view was already being fetched and its HTML thrown away.
        service, client = _service({20003: 26})
        _read(service, 20003)

        assert len(client.calls) == 2

    def test_a_different_village_gets_its_own_slot(self):
        """Nothing here is pinned to one account's layout."""
        service, client = _service({20003: 26, 20004: 31})
        _read(service, 20003)
        client.browser_headers.last_page_path = "/elsewhere.php"  # forces the dorf2 path
        _read(service, 20004)

        assert "/build.php?id=26&gid=17&t=3&newdid=20003" in client.calls
        assert "/build.php?id=31&gid=17&t=3&newdid=20004" in client.calls

    def test_the_parameters_are_in_the_games_order(self):
        # id, then gid, then t, then newdid -- every line of the capture. The
        # server does not care; a request signature does.
        service, client = _service({20003: 26})
        _read(service, 20003)

        assert client.calls[1] == "/build.php?id=26&gid=17&t=3&newdid=20003"

    def test_a_village_with_no_marketplace_addresses_the_type(self):
        """Unknown is not zero, and not a guess.

        A village view carrying no marketplace leaves the slot unknown, and the
        URL goes out the way it went out before any of this existed. The
        alternative -- inventing a slot -- would address some other building.
        """
        service, client = _service({20003: 26})
        client._slots = {}

        async def _no_marketplace(path, **kw):
            client.calls.append(path)
            client.browser_headers.last_page_path = path
            if path.startswith("/dorf2.php"):
                return "<html><body><div id='village_map'></div></body></html>"
            return empty_marketplace(20003)

        client.get_html = _no_marketplace
        _read(service, 20003)

        assert client.calls[1] == "/build.php?gid=17&t=3&newdid=20003"


class TestSwitchingVillagesSkipsTheVillageView:
    """Four switches in the capture, four with no dorf2 in them."""

    def test_the_second_village_is_reached_without_a_dorf2(self, monkeypatch):
        # Requires the slot to be known, so visit 20004 once first.
        service, client = _service({20003: 26, 20004: 31})
        _read(service, 20004)
        _read(service, 20003)
        before = len(client.calls)

        import travian_api.services.trade_route_service as mod

        monkeypatch.setattr(mod.random, "random", lambda: 1.0)  # stale load off
        _read(service, 20004)

        assert client.calls[before:] == ["/build.php?id=31&gid=17&t=3&newdid=20004"]

    def test_it_is_referred_from_the_marketplace_it_switched_from(self, monkeypatch):
        import travian_api.services.trade_route_service as mod

        monkeypatch.setattr(mod.random, "random", lambda: 1.0)
        service, client = _service({20003: 26, 20004: 31})
        _read(service, 20004)
        _read(service, 20003)
        before = len(client.calls)
        _read(service, 20004)

        assert client.referers[before] == (
            "https://example.invalid/build.php?id=26&gid=17&t=3&newdid=20003"
        )

    def test_a_first_visit_still_walks_through_the_village_view(self, monkeypatch):
        """The slot has to come from somewhere, and a player arriving from
        elsewhere goes through dorf2 anyway -- twice in the capture."""
        import travian_api.services.trade_route_service as mod

        monkeypatch.setattr(mod.random, "random", lambda: 1.0)
        service, client = _service({20003: 26, 20004: 31})
        _read(service, 20003)
        before = len(client.calls)
        _read(service, 20004)

        assert client.calls[before] == "/dorf2.php?newdid=20004"

    def test_a_session_that_navigated_away_walks_again(self, monkeypatch):
        """The guard is against the session's REAL last page.

        A farm loop that took a page load during our pacing delay leaves us
        somewhere else entirely. Claiming to switch from a marketplace we are no
        longer on would put a Referer on the wire that the wire contradicts.
        """
        import travian_api.services.trade_route_service as mod

        monkeypatch.setattr(mod.random, "random", lambda: 1.0)
        service, client = _service({20003: 26, 20004: 31})
        _read(service, 20004)
        _read(service, 20003)
        client.browser_headers.last_page_path = "/dorf1.php"  # something else moved us
        before = len(client.calls)
        _read(service, 20004)

        assert client.calls[before] == "/dorf2.php?newdid=20004"


class TestTheStaleSlotLoadIsReplayedButNotAlways:
    """The browser addresses the wrong building first. Sometimes, so do we.

    The client does it every time. We do it a fraction of the time on purpose:
    reproducing a deterministic quirk deterministically swaps one invariant for
    another, and an account with fixed slots would then emit exactly two loads
    on exactly the same village pairs forever. Both shapes are shapes the client
    produces; which one we emit should not be predictable.
    """

    def _switch(self, roll: float, monkeypatch, slots=None):
        import travian_api.services.trade_route_service as mod

        service, client = _service(slots or {20003: 26, 20004: 31})
        monkeypatch.setattr(mod.random, "random", lambda: 1.0)
        _read(service, 20004)
        _read(service, 20003)
        before = len(client.calls)
        monkeypatch.setattr(mod.random, "random", lambda: roll)
        _read(service, 20004)
        return client.calls[before:]

    def test_it_carries_the_previous_villages_slot(self, monkeypatch):
        assert self._switch(0.0, monkeypatch) == [
            "/build.php?id=26&gid=17&t=3&newdid=20004",  # 20003's slot, 20004's village
            "/build.php?id=31&gid=17&t=3&newdid=20004",  # corrected
        ]

    def test_a_high_roll_emits_only_the_correct_one(self, monkeypatch):
        assert self._switch(0.99, monkeypatch) == ["/build.php?id=31&gid=17&t=3&newdid=20004"]

    def test_matching_slots_never_produce_one(self, monkeypatch):
        """Line 1163 of the capture: nothing to correct, nothing emitted."""
        assert self._switch(0.0, monkeypatch, slots={20003: 31, 20004: 31}) == [
            "/build.php?id=31&gid=17&t=3&newdid=20004"
        ]

    def test_both_loads_refer_from_the_page_switched_from(self, monkeypatch):
        # Not from each other. In the capture both carry the previous tab's URL,
        # because the second is a correction of the first, not a click on it.
        import travian_api.services.trade_route_service as mod

        service, client = _service({20003: 26, 20004: 31})
        monkeypatch.setattr(mod.random, "random", lambda: 1.0)
        _read(service, 20004)
        _read(service, 20003)
        before = len(client.calls)
        monkeypatch.setattr(mod.random, "random", lambda: 0.0)
        _read(service, 20004)

        from_page = "https://example.invalid/build.php?id=26&gid=17&t=3&newdid=20003"
        assert client.referers[before:] == [from_page, from_page]

    def test_a_failed_stale_load_does_not_fail_the_read(self, monkeypatch):
        """Its response is discarded unread, so its failure must cost nothing.

        Letting it propagate would fail a marketplace read over a request whose
        entire purpose is to be thrown away -- strictly worse than the pattern
        it exists to hide.
        """
        import travian_api.services.trade_route_service as mod

        service, client = _service({20003: 26, 20004: 31})
        monkeypatch.setattr(mod.random, "random", lambda: 1.0)
        _read(service, 20004)
        _read(service, 20003)
        real = client.get_html

        async def _fail_the_stale_one(path, **kw):
            if path.startswith("/build.php?id=26") and "newdid=20004" in path:
                client.calls.append(path)
                raise RuntimeError("HTTP 500")
            return await real(path, **kw)

        client.get_html = _fail_the_stale_one
        monkeypatch.setattr(mod.random, "random", lambda: 0.0)
        before = len(client.calls)
        _read(service, 20004)  # must not raise

        assert client.calls[before:] == [
            "/build.php?id=26&gid=17&t=3&newdid=20004",
            "/build.php?id=31&gid=17&t=3&newdid=20004",
        ]

    def test_the_chance_is_a_fraction_not_a_certainty(self):
        from travian_api.services.trade_route_service import _STALE_SLOT_CHANCE

        assert 0.0 < _STALE_SLOT_CHANCE < 1.0


class TestAStaleSlotDoesNotFailForever:
    """The direct switch skips the village view, so it never re-learns a slot.

    Which is fine until the slot is wrong. A marketplace demolished and rebuilt
    on another slot leaves the cache pointing at whatever now stands there; the
    read then addresses the wrong building, finds no trade-route model, and
    fails closed -- correctly. But it would fail closed on exactly the same
    wrong URL on every retry, forever, because the only code that refreshes the
    cache is the path the cache lets us skip.
    """

    def test_an_unreadable_page_forgets_the_slot(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, client = _service({20003: 26})
        _read(service, 20003)
        assert service._marketplace_slot[20003] == 26

        async def _no_model(path, **kw):
            client.calls.append(path)
            client.browser_headers.last_page_path = path
            return "<html><body>not the marketplace</body></html>"

        client.get_html = _no_model
        try:
            _read(service, 20003)
        except MarketplaceUnreadable:
            pass
        else:  # pragma: no cover - the read must fail closed
            raise AssertionError("an unreadable marketplace must raise")

        assert 20003 not in service._marketplace_slot

    def test_the_next_attempt_walks_the_village_view_again(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, client = _service({20003: 26})
        _read(service, 20003)
        real = client.get_html

        async def _no_model(path, **kw):
            client.calls.append(path)
            client.browser_headers.last_page_path = path
            return "<html><body>not the marketplace</body></html>"

        client.get_html = _no_model
        try:
            _read(service, 20003)
        except MarketplaceUnreadable:
            pass

        client.get_html = real
        before = len(client.calls)
        _read(service, 20003)

        assert client.calls[before] == "/dorf2.php?newdid=20003", (
            "the retry has to re-learn the slot, or it repeats the same wrong URL"
        )


class TestReopeningTheSameMarketplaceIsAReload:
    """Not every "already on a marketplace" is a village switch.

    Re-opening the SAME village's marketplace took the switch branch, which
    refers the request from the page it is switching FROM -- and that page is
    this page. So the GET went out with its own URL in its Referer, which a
    browser emits only in a redirect loop.

    A reload keeps the Referer the page was opened with. That is what this
    remembers.
    """

    def _reopen(self):
        service, client = _service({20003: 26})
        _read(service, 20003)
        before = len(client.calls)
        _read(service, 20003)
        return client, before

    def test_it_does_not_refer_to_itself(self):
        client, before = self._reopen()
        path, referer = client.calls[before], client.referers[before]

        assert referer != f"https://example.invalid{path}"

    def test_it_refers_to_where_the_page_was_opened_from(self):
        client, before = self._reopen()

        assert client.referers[before] == "https://example.invalid/dorf2.php?newdid=20003"

    def test_it_is_still_one_request_with_no_village_view(self):
        """A reload is a reload -- it does not walk back through dorf2."""
        client, before = self._reopen()

        assert client.calls[before:] == ["/build.php?id=26&gid=17&t=3&newdid=20003"]

    def test_a_real_switch_still_refers_from_the_other_marketplace(self):
        import travian_api.services.trade_route_service as mod

        service, client = _service({20003: 26, 20004: 31})
        _read(service, 20004)
        _read(service, 20003)
        before = len(client.calls)
        mod.random.random = lambda: 1.0  # stale load off
        try:
            _read(service, 20004)
        finally:
            import random as _r

            mod.random.random = _r.random

        assert client.referers[before] == (
            "https://example.invalid/build.php?id=26&gid=17&t=3&newdid=20003"
        )
