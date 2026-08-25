"""Exactly how much traffic one live trade-route run costs.

Stealth here is a budget, not a vibe. The daily activity ceiling is spent by
every loop in the app, so "roughly a few requests" is not good enough: a change
that quietly doubles the cost of a route run steals budget from the farm loops
and pushes the account's daily total somewhere a human's would not be.

These pin the count and the ORDER for the real service against a counting
transport, so a regression shows up as a failing test rather than as an
account-level anomaly weeks later.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    PlannedRoute,
    TradeRouteService,
)

# A page carrying an empty but VALID trade-route model, so the parser recognises
# it (no routes) rather than reporting the page as unreadable.
EMPTY_MARKETPLACE = (
    "<html><body><script>window.Travian.React.TradeRoutes.render("
    '{viewData: {"ownPlayer":{"village":{"marketplace":{"tradeRoutes":[]}}}}}'
    ");</script></body></html>"
)


def _marketplace_with(route_id: int, dest: int) -> str:
    return (
        "<html><body><script>window.Travian.React.TradeRoutes.render("
        '{viewData: {"ownPlayer":{"village":{"marketplace":{"tradeRoutes":['
        '{"to":{"id":' + str(dest) + ',"name":"x","mapId":1},"routes":['
        '{"id":' + str(route_id) + ',"enabled":true,"sendOnce":false,'
        '"carriedResources":{"lumber":0,"clay":0,"iron":0,"crop":1},'
        '"departureAt":1,"arrivalAt":2,"repeat":1,"merchants":1,"ships":1,'
        '"useTradeShips":false}]}'
        "]}}}}}"
        ");</script></body></html>"
    )


class _CountingClient:
    """Records every request in order. Serves whatever HTML the test queues."""

    def __init__(self, pages):
        self.calls: list[tuple[str, str]] = []
        self._pages = list(pages)
        self.waits: list[str] = []
        self.settings = SimpleNamespace(base_url="https://example.invalid")
        self.human_delay = SimpleNamespace(wait=self._wait)
        self.activity_scheduler = SimpleNamespace(log_activity=lambda _s: None)

    async def _wait(self, _action, reason=""):
        # Pacing is what keeps a burst from looking like a burst. Recorded so a
        # write that skipped its delay is visible here.
        self.waits.append(reason)

    async def get_html(self, path, **kw):
        self.calls.append(("GET", path))
        return self._pages.pop(0) if self._pages else EMPTY_MARKETPLACE

    async def post_json(self, path, payload, **kw):
        self.calls.append(("POST", path))
        return {}

    async def put_json(self, path, payload, **kw):
        self.calls.append(("PUT", path))
        return {}


def _service(pages):
    client = _CountingClient(pages)
    return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client


def _route(dest: int = 700) -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20003,
        dest_village_id=dest,
        dest_x=1,
        dest_y=2,
        dest_name="target",
        cargo={Resource.CROP: 1000},
        cycle_hours=24,
        merchants=1,
        dispatch_minute=58,
    )


class TestTheCanaryRunCostsFourRequests:
    """Read the village, create one route, confirm it. Nothing else."""

    def test_a_read_is_two_gets_in_the_human_order(self):
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))

        assert client.calls == [
            ("GET", "/dorf2.php?newdid=20003"),
            ("GET", "/build.php?gid=17&t=3&newdid=20003"),
        ], "the village view must come first, or the marketplace Referer is a lie"

    def test_a_create_is_one_post_and_it_waits_first(self):
        service, client = _service([])
        asyncio.run(service.create_route(_route()))

        assert client.calls == [("POST", "/api/v1/trade-routes")]
        assert client.waits, "a write with no pacing delay is a burst of one"

    def test_a_confirmation_is_a_single_get_with_no_dorf2_hop(self):
        # Refreshing the page you are already on must not re-walk the navigation
        # to it. A browser does not visit the village view to reload the tab it
        # is sitting on.
        service, client = _service([_marketplace_with(1, 700)])
        asyncio.run(service.confirm_routes(20003))

        assert client.calls == [("GET", "/build.php?gid=17&t=3&newdid=20003")]

    def test_the_whole_canary_is_four_requests_in_this_exact_order(self):
        service, client = _service(
            [EMPTY_MARKETPLACE, EMPTY_MARKETPLACE, _marketplace_with(1, 700)]
        )
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))
        confirmed = asyncio.run(service.confirm_routes(20003))

        assert client.calls == [
            ("GET", "/dorf2.php?newdid=20003"),
            ("GET", "/build.php?gid=17&t=3&newdid=20003"),
            ("POST", "/api/v1/trade-routes"),
            ("GET", "/build.php?gid=17&t=3&newdid=20003"),
        ]
        assert len(client.calls) == 4
        assert [r.route_id for r in confirmed] == [1]

    def test_verifying_costs_exactly_one_request_more_than_not_verifying(self):
        # The price of not guessing. Worth stating precisely, because the
        # alternative was reporting routes that may not exist.
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))
        before = len(client.calls)

        service._marketplace_referer.clear()
        asyncio.run(service.confirm_routes(20003))

        assert len(client.calls) - before == 1


class TestTheWriteIsRefereredFromTheMarketplace:
    def test_a_create_is_referred_from_the_page_that_has_the_form(self):
        # A POST to the trade-route endpoint referred from anywhere else is a
        # desync no browser produces: the form only exists on that tab.
        captured = {}

        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE])

        async def _post(path, payload, **kw):
            captured["referer"] = kw.get("referer")
            client.calls.append(("POST", path))
            return {}

        client.post_json = _post
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))

        assert captured["referer"] == ("https://example.invalid/build.php?gid=17&t=3&newdid=20003")

    def test_a_confirmation_read_keeps_the_referer_pinned_for_later_writes(self):
        service, client = _service([_marketplace_with(1, 700)])
        asyncio.run(service.confirm_routes(20003))

        assert service._marketplace_referer[20003].endswith("/build.php?gid=17&t=3&newdid=20003")


class TestAnUnreadablePageIsNeverSilentlyEmpty:
    def test_a_confirmation_that_cannot_be_read_raises(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, _ = _service(["<html><body>soft block</body></html>"])
        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.confirm_routes(20003))

    def test_a_read_that_cannot_be_parsed_raises_too(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, _ = _service(["<html/>", "<html><body>login please</body></html>"])
        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.list_existing_routes(20003))
