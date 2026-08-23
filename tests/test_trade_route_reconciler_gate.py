"""Creating a route requires being able to read the routes that already exist.

A page we cannot read yields no routes. That is the SAFE answer for disabling --
nothing gets disabled -- and the dangerous answer for creating, because "no
routes" reads as "this village has no routes" and the whole plan gets created
again. Every run. Duplicates accumulate
in-game, and the repeated identical creates are exactly the daily
rebuild-the-same-routes pattern the executor's own comments say it avoids.

The 2026-08-20 capture recorded the request bodies but not the page HTML, so the
row markup is still a guess. Until it is confirmed, creation is refused and
disabling and previewing are not.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    ROUTE_LIST_MARKUP_VERIFIED,
    ExistingRoute,
    MarketplaceUnreadable,
    PlannedRoute,
    TradeRouteReconcilerUnverified,
    TradeRouteService,
)


class _RecordingClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

        class _Delay:
            @staticmethod
            async def wait(*_args, **_kwargs) -> None:
                return None

        self.human_delay = _Delay()
        # open_marketplace reads base_url to pin the write's Referer.
        self.settings = SimpleNamespace(base_url="https://ts2.x1.europe.travian.com")

    async def post_json(self, url, payload, **_kw):
        self.sent.append(("POST", url))
        return {}

    async def put_json(self, url, payload, **_kw):
        self.sent.append(("PUT", url))
        return {}

    async def get_html(self, url, **_kw):
        self.sent.append(("GET", url))
        return "<html><body>unrecognised markup</body></html>"


def _route() -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20031,
        dest_village_id=20044,
        dest_x=23,
        dest_y=88,
        dest_name="capital",
        cargo={Resource.CROP: 20_000},
        cycle_hours=3,
        merchants=4,
        dispatch_minute=90,
    )


def test_the_markup_is_verified_now():
    # Flipped once the real gid=17&t=3 page was captured, its JSON model read,
    # and tests/fixtures/marketplace_trade_routes.html pinned against it.
    # Protection did not go away -- it moved from "we have never seen this
    # markup" to "this particular page did not parse", which is per-page and
    # therefore catches a block page or a future gpack change too.
    assert ROUTE_LIST_MARKUP_VERIFIED is True
    assert TradeRouteService(_RecordingClient()).reconciler_verified is True


def test_the_gate_still_bites_if_it_is_ever_turned_off_again():
    service = TradeRouteService(_RecordingClient(), live_enabled=True, reconciler_verified=False)
    with pytest.raises(TradeRouteReconcilerUnverified):
        asyncio.run(service.create_route(_route()))


def test_a_create_is_allowed_now_that_the_page_can_be_read():
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True)
    asyncio.run(service.create_route(_route()))
    assert [verb for verb, _ in client.sent] == ["POST"]


def test_an_unreadable_marketplace_raises_instead_of_reading_as_empty():
    # THE original hazard, now caught per page rather than blanket-blocked. The
    # recording client returns markup with no route model, which is what a soft
    # block page or a login redirect looks like. Returning [] here would tell
    # the reconciler "this village has no routes" and re-create the whole plan.
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True)
    with pytest.raises(MarketplaceUnreadable, match="no trade-route model"):
        asyncio.run(service.list_existing_routes(20031))


def test_disabling_is_still_allowed():
    # An unreadable page yields no routes, so a disable is a no-op rather than a
    # wrong action -- there is nothing unsafe to gate here.
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True)
    routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=5, dest_y=6)]
    result = asyncio.run(service.disable_routes(20031, routes))
    assert result is not None
    assert [verb for verb, _ in client.sent] == ["PUT"]


def test_a_caller_that_can_read_route_state_may_create():
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)
    asyncio.run(service.create_route(_route()))
    assert [verb for verb, _ in client.sent] == ["POST"]


def test_the_marketplace_read_loads_the_trade_route_tab():
    # Routes live on tab t=3; without it the reconciler read a page that cannot
    # contain them at all.
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True)
    asyncio.run(service.open_marketplace(20031))
    urls = [url for verb, url in client.sent if verb == "GET"]
    assert any("gid=17" in u and "t=3" in u for u in urls), urls
