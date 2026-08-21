"""Creating a route requires being able to read the routes that already exist.

`parse_trade_routes` returns [] for markup it does not recognise. That is the
SAFE answer for disabling -- an empty list disables nothing -- and the dangerous
answer for creating, because the reconciler reads [] as "this village has no
routes" and creates the whole plan again. Every run. Duplicates accumulate
in-game, and the repeated identical creates are exactly the daily
rebuild-the-same-routes pattern the executor's own comments say it avoids.

The 2026-08-20 capture recorded the request bodies but not the page HTML, so the
row markup is still a guess. Until it is confirmed, creation is refused and
disabling and previewing are not.
"""

import asyncio

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    ROUTE_LIST_MARKUP_VERIFIED,
    ExistingRoute,
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
        dest_x=23,
        dest_y=88,
        dest_name="capital",
        cargo={Resource.CROP: 20_000},
        cycle_hours=3,
        merchants=4,
        dispatch_minute=90,
    )


def test_the_production_default_is_unverified():
    # The whole gate rests on this staying False until someone captures the page.
    assert ROUTE_LIST_MARKUP_VERIFIED is False
    assert TradeRouteService(_RecordingClient()).reconciler_verified is False


def test_a_create_is_refused_and_sends_nothing():
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True)
    with pytest.raises(TradeRouteReconcilerUnverified, match="gid=17&t=3"):
        asyncio.run(service.create_route(_route()))
    assert client.sent == [], "a refused create must not touch the game"


def test_disabling_is_still_allowed():
    # An unreadable page yields no routes, so a disable is a no-op rather than a
    # wrong action -- there is nothing unsafe to gate here.
    client = _RecordingClient()
    service = TradeRouteService(client, live_enabled=True)
    routes = [ExistingRoute(route_id=1, dest_x=5, dest_y=6)]
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
