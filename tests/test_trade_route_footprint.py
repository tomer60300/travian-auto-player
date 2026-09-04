"""Exactly how much traffic one live trade-route run costs.

Stealth here is a budget, not a vibe. The daily activity ceiling is spent by
every loop in the app, so "roughly a few requests" is not good enough: a change
that quietly doubles the cost of a route run steals budget from the farm loops
and pushes the account's daily total somewhere a human's would not be.

These pin the count, the METHOD and the ORDER for the real service against a
counting transport, so a regression shows up as a failing test rather than as an
account-level anomaly weeks later.

The read-back is a ``POST /api/v1/graphql``, not a page load, because that is
what the game's own client does: a create's success handler calls
``e.onSuccess()``, which on the list page refetches the route list over GraphQL.
The count did not change when it moved -- the shape did.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.exceptions import NetworkError
from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    MARKETPLACE_READBACK_QUERY,
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


GRAPHQL = "/api/v1/graphql"


def _readback(village: int, *routes: dict) -> dict:
    """A GraphQL answer shaped exactly like the marketplace query's.

    Same ``ownPlayer.village.marketplace.tradeRoutes`` path the page model uses,
    plus the ``currentVillageId`` the query asks for so the caller can tell which
    village it was actually answered about.
    """
    return {
        "data": {
            "ownPlayer": {
                "id": 1,
                "currentVillageId": village,
                "village": {"marketplace": {"tradeRoutes": list(routes)}},
            }
        }
    }


def _route_row(route_id: int, dest: int) -> dict:
    return {
        "to": {"id": dest, "name": "x", "mapId": 1},
        "routes": [
            {
                "id": route_id,
                "enabled": True,
                "sendOnce": False,
                "carriedResources": {"lumber": 0, "clay": 0, "iron": 0, "crop": 1},
                "departureAt": 1,
                "arrivalAt": 2,
                "repeat": 1,
                "merchants": 1,
                "ships": 1,
                "useTradeShips": False,
            }
        ],
    }


class _CountingClient:
    """Records every request in order. Serves whatever the test queues."""

    def __init__(self, pages, readbacks=()):
        self.calls: list[tuple[str, str]] = []
        self.referers: list[tuple[str, str | None]] = []
        self.bodies: list[tuple[str, dict]] = []
        self._pages = list(pages)
        self._readbacks = list(readbacks)
        self.waits: list[str] = []
        self.settings = SimpleNamespace(base_url="https://example.invalid")
        self.human_delay = SimpleNamespace(wait=self._wait)
        # Recorded, not discarded: every request this service makes is billed
        # to the daily activity ceiling, and "how much traffic did that cost"
        # is exactly what this file is about.
        self.logged_activity: list[float] = []
        self.activity_scheduler = SimpleNamespace(log_activity=self.logged_activity.append)

    async def _wait(self, _action, reason=""):
        # Pacing is what keeps a burst from looking like a burst. Recorded so a
        # write that skipped its delay is visible here.
        self.waits.append(reason)

    async def get_html(self, path, **kw):
        self.calls.append(("GET", path))
        self.referers.append((path, kw.get("referer")))
        return self._pages.pop(0) if self._pages else EMPTY_MARKETPLACE

    async def post_json(self, path, payload, **kw):
        self.calls.append(("POST", path))
        self.referers.append((path, kw.get("referer")))
        self.bodies.append((path, payload))
        if path == GRAPHQL:
            assert self._readbacks, "queue a read-back payload for every confirm"
            return self._readbacks.pop(0)
        return {}

    async def put_json(self, path, payload, **kw):
        self.calls.append(("PUT", path))
        self.referers.append((path, kw.get("referer")))
        return {}


def _service(pages, readbacks=()):
    client = _CountingClient(pages, readbacks)
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

    def test_a_confirmation_is_a_single_graphql_post_with_no_page_load(self):
        # Refetching the model the open page runs on is not a navigation at all,
        # so it must not walk to the page OR reload it. The game's own create
        # handler fires one GraphQL query; so does this.
        service, client = _service([], [_readback(20003, _route_row(1, 700))])
        asyncio.run(service.confirm_routes(20003))

        assert client.calls == [("POST", GRAPHQL)]

    def test_the_whole_canary_is_four_requests_in_this_exact_order(self):
        service, client = _service(
            [EMPTY_MARKETPLACE, EMPTY_MARKETPLACE],
            [_readback(20003, _route_row(1, 700))],
        )
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))
        confirmed = asyncio.run(service.confirm_routes(20003))

        assert client.calls == [
            ("GET", "/dorf2.php?newdid=20003"),
            ("GET", "/build.php?gid=17&t=3&newdid=20003"),
            ("POST", "/api/v1/trade-routes"),
            ("POST", GRAPHQL),
        ]
        assert len(client.calls) == 4
        assert [r.route_id for r in confirmed] == [1]

    def test_verifying_costs_exactly_one_request_more_than_not_verifying(self):
        # The price of not guessing. Worth stating precisely, because the
        # alternative was reporting routes that may not exist.
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE], [_readback(20003)])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))
        before = len(client.calls)

        asyncio.run(service.confirm_routes(20003))

        assert len(client.calls) - before == 1


class TestTheReadBackIsTheQueryTheGameFires:
    """Byte-for-byte, because a query no client sends is worse than a page load.

    That was the standing objection to moving this read to GraphQL: a
    hand-written query differing by one field or one space is a request the game
    cannot produce, which is more distinctive than a page refresh, not less. It
    is answered by taking the query from the bundle rather than reconstructing
    it -- so this pins the body.
    """

    def _confirm(self):
        service, client = _service([], [_readback(20003)])
        asyncio.run(service.confirm_routes(20003))
        return next(body for path, body in client.bodies if path == GRAPHQL)

    def test_the_query_is_the_bundles_query(self):
        assert self._confirm()["query"] == (
            "{ownPlayer{id currentVillageId village{marketplace"
            "{tradeRoutes{...TradeRouteFields}}}}}"
            "fragment TradeRouteFields on TradeRoutesSet{objectId expanded"
            " from{id name tribeId}to{id mapId name travelTime player{id}}"
            "routes{...RouteFields}"
            "nextDelivery{departureAt merchants ships useTradeShips}}"
            "fragment RouteFields on TradeRoute{id enabled sendOnce"
            " carriedResources{lumber clay iron crop}departureAt arrivalAt repeat"
            " merchants ships useTradeShips}"
        )

    def test_the_operation_carries_no_query_keyword(self):
        # graphql-js prints an anonymous operation with no variables and no
        # directives in short form, and the client prints before it sends. A
        # leading "query" would be a byte the browser never emits.
        assert self._confirm()["query"].startswith("{ownPlayer{")

    def test_it_asks_which_village_it_is_answering_about(self):
        # The query takes no village argument, so this field is the only thing
        # standing between us and attributing one village's routes to another.
        assert "currentVillageId" in self._confirm()["query"]

    def test_the_body_has_exactly_one_key(self):
        # The client passes no variables and JSON.stringify drops the undefined
        # value, so the real body is {"query": ...}. An extra key is a
        # fingerprint like any other.
        assert set(self._confirm()) == {"query"}

    def test_the_constant_is_what_is_sent(self):
        assert self._confirm()["query"] == MARKETPLACE_READBACK_QUERY


class TestEveryMarketplaceRequestStatesItsOwnReferer:
    """Not one of these may fall back to the account-wide "last page".

    That field is a single slot shared by every concurrent operation, and each of
    these requests waits out a pacing gap before its headers are built -- so a
    farm loop or queue poll landing in the window takes the Referer with it.
    """

    MARKETPLACE = "https://example.invalid/build.php?gid=17&t=3&newdid=20003"

    def test_the_marketplace_get_is_referred_from_the_village_view(self):
        # The navigation stays a page load precisely so this Referer is truthful:
        # dorf2 is the page a human clicks the marketplace from. Pinning it is
        # what makes the claim survive a concurrent GET.
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))

        assert client.referers == [
            ("/dorf2.php?newdid=20003", None),
            (
                "/build.php?gid=17&t=3&newdid=20003",
                "https://example.invalid/dorf2.php?newdid=20003",
            ),
        ]

    def test_a_create_is_referred_from_the_page_that_has_the_form(self):
        # A POST to the trade-route endpoint referred from anywhere else is a
        # desync no browser produces: the form only exists on that tab.
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))

        assert dict(client.referers)["/api/v1/trade-routes"] == self.MARKETPLACE

    def test_the_read_back_is_referred_from_the_marketplace_too(self):
        # An API request never advances page context, so the GraphQL read-back
        # cannot inherit a truthful Referer -- it has to be given one, and the
        # only page whose script fires this query is the trade-route tab.
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE], [_readback(20003)])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.confirm_routes(20003))

        assert dict(client.referers)[GRAPHQL] == self.MARKETPLACE

    def test_the_navigation_still_establishes_the_pin_for_later_writes(self):
        service, _ = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))

        assert service._marketplace_referer[20003] == self.MARKETPLACE


class TestAnUnreadableAnswerIsNeverSilentlyEmpty:
    def test_a_confirmation_that_cannot_be_read_raises(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        # A soft block, a login redirect or an error payload all arrive as a body
        # with no marketplace in it. That is "unknown", never "no routes".
        service, _ = _service([], [{"errors": [{"message": "nope"}]}])
        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.confirm_routes(20003))

    def test_a_read_back_about_another_village_raises(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        # The query names no village, so a concurrent ?newdid= can move the
        # session between the write and the read. The answer says which village
        # it described; believing the wrong one would report routes that were
        # never created here.
        service, _ = _service([], [_readback(20009, _route_row(1, 700))])
        with pytest.raises(MarketplaceUnreadable, match="answered for village 20009"):
            asyncio.run(service.confirm_routes(20003))

    def test_a_read_that_cannot_be_parsed_raises_too(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, _ = _service(["<html/>", "<html><body>login please</body></html>"])
        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.list_existing_routes(20003))


class TestAFailedReadIsBilledToo:
    """A read that raised spent its request exactly as one that answered.

    This is the module's own argument for billing reads at all, pointed at the
    failures: they consumed a real throttler gap, and the daily ceiling is
    SHARED with the farm-list and oasis loops, so under-counting here silently
    licenses THOSE to overspend. A NetworkError refunds nothing.

    Exactly once is the property, in both directions: a read that failed
    half-way through its two GETs must not be billed twice, and one that
    answered must not be billed at all over again.
    """

    def _refusing_get(self, client, fail_on: int):
        """A get_html that answers normally until the *fail_on*-th call."""
        seen = {"n": 0}
        real = client.get_html

        async def get_html(path, **kwargs):
            seen["n"] += 1
            if seen["n"] == fail_on:
                client.calls.append(("GET", path))
                raise NetworkError("HTTP 500: the game said no")
            return await real(path, **kwargs)

        return get_html

    def test_a_village_view_that_fails_is_still_billed(self):
        service, client = _service([])
        client.get_html = self._refusing_get(client, fail_on=1)

        with pytest.raises(NetworkError):
            asyncio.run(service.open_marketplace(20003))

        assert client.calls == [("GET", "/dorf2.php?newdid=20003")]
        assert len(client.logged_activity) == 1, "a failed read is not a free read"
        assert client.logged_activity[0] >= 0.0

    def test_a_marketplace_get_that_fails_after_the_village_view_is_billed_once(self):
        # Two requests went out and one billing covers the pair, exactly as on
        # the success path -- the span is what is billed, not the count.
        service, client = _service([EMPTY_MARKETPLACE])
        client.get_html = self._refusing_get(client, fail_on=2)

        with pytest.raises(NetworkError):
            asyncio.run(service.open_marketplace(20003))

        assert len(client.calls) == 2
        assert len(client.logged_activity) == 1

    def test_a_read_back_that_fails_is_still_billed(self):
        service, client = _service([])

        async def refuse(path, _payload, **_kwargs):
            client.calls.append(("POST", path))
            raise NetworkError("HTTP 500: the game said no")

        client.post_json = refuse

        with pytest.raises(NetworkError):
            asyncio.run(service.refresh_marketplace(20003))

        assert client.calls == [("POST", GRAPHQL)]
        assert len(client.logged_activity) == 1

    def test_an_unreadable_answer_is_billed_exactly_once(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        # The request succeeded and the BODY was the problem. Already billed
        # before this raise, so the guard is against billing it twice.
        service, client = _service([], [{"errors": [{"message": "nope"}]}])

        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.refresh_marketplace(20003))

        assert len(client.logged_activity) == 1

    def test_a_read_that_answers_is_billed_once_per_method(self):
        # The regression anchor: open_marketplace is two GETs and one billing,
        # refresh_marketplace is one POST and one billing.
        service, client = _service([EMPTY_MARKETPLACE, EMPTY_MARKETPLACE], [_readback(20003)])
        asyncio.run(service.list_existing_routes(20003))
        assert len(client.logged_activity) == 1

        asyncio.run(service.refresh_marketplace(20003))
        assert len(client.logged_activity) == 2
