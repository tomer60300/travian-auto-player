"""The live trade-route switch: it must exist, default off, and reach the service.

The ``/api/v1/trade-routes`` request payload has never been captured from a real
client, so :class:`TradeRouteService` refuses live writes until an operator says
otherwise. Until this switch existed there was no way to say otherwise:
``web/sessions.py`` built the service with the constructor default, so even a
correctly captured payload would have stayed inert.

The wiring is the part worth pinning. A flag that exists but does not reach the
service is worse than no flag, because it reads as enabled while refusing every
write -- and the refusal only shows up at the moment the operator finally tries
to create real routes.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.config import Settings
from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)
from travian_api.web.sessions import TravianSession

SERVER = "https://ts1.x1.europe.travian.com"
FLAG = "TRAVIAN_TRADE_ROUTE_LIVE"


def _route() -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20031,
        dest_x=10,
        dest_y=-20,
        dest_name="capital",
        cargo={Resource.LUMBER: 0, Resource.CLAY: 0, Resource.IRON: 0, Resource.CROP: 20_000},
        cycle_hours=3,
        merchants=4,
        dispatch_minute=90,
    )


class _RecordingClient:
    """Minimal HttpClient stand-in that records anything sent."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

        class _Delay:
            @staticmethod
            async def wait(*_args, **_kwargs) -> None:
                return None

        self.human_delay = _Delay()
        # open_marketplace reads base_url to pin the write's Referer.
        self.settings = SimpleNamespace(base_url="https://ts2.x1.europe.travian.com")

    async def post_json(self, url: str, payload: dict, **_kwargs):
        self.posts.append((url, payload))
        return {}


class TestTheSwitchDefaultsOff:
    def test_settings_default_is_off(self, monkeypatch):
        # Cleared explicitly: the repo ships a .env, and a developer who has
        # turned the flag on there must not make this test pass by accident.
        monkeypatch.delenv(FLAG, raising=False)
        assert Settings(base_url=SERVER, _env_file=None).trade_route_live is False

    def test_the_env_var_turns_it_on(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        assert Settings(base_url=SERVER).trade_route_live is True

    def test_the_service_constructor_still_defaults_off(self):
        assert TradeRouteService(_RecordingClient()).live_enabled is False


class TestTheSessionWiresItThrough:
    """The gap this switch was added to close."""

    def test_a_session_hands_the_setting_to_the_service(self, monkeypatch):
        monkeypatch.setenv(FLAG, "1")
        session = TravianSession(1, SERVER, "someone", "secret")
        assert session.trade_route_service.live_enabled is True

    def test_a_session_leaves_it_off_by_default(self, monkeypatch):
        monkeypatch.delenv(FLAG, raising=False)
        session = TravianSession(2, SERVER, "someone", "secret")
        assert session.trade_route_service.live_enabled is False


class TestRefusalSendsNothing:
    """Refusing is only useful if it happens BEFORE anything reaches the game."""

    def test_create_route_refuses_and_posts_nothing(self):
        client = _RecordingClient()
        service = TradeRouteService(client, live_enabled=False)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(service.create_route(_route()))
        assert client.posts == [], "a refused create must not touch the game"

    def test_the_refusal_names_the_switch_that_would_allow_it(self):
        service = TradeRouteService(_RecordingClient(), live_enabled=False)
        with pytest.raises(TradeRoutePayloadUnverified, match="TRAVIAN_TRADE_ROUTE_LIVE"):
            asyncio.run(service.create_route(_route()))
