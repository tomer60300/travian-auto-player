"""The trade-route wire format, pinned to a captured real client request.

Captured from Europe 2 on 2026-08-20 (gpack 597.6) with Gold Club active:

    POST /api/v1/trade-routes  -> 201, empty body
    {"action":"traderoute","sourceVillageId":<id>,
     "targetCoordinates":{"x":<x>,"y":<y>},
     "resources":{"lumber":1,"clay":2,"iron":3,"crop":4},
     "mode":"send","hour":15,"minute":27,"deliveries":1,
     "repeatEvery":1,"enabled":true,"useTradeShips":false}

    PUT  /api/v1/trade-routes  -> 200
    {"action":"traderoute","routes":[{"enabled":false,"id":<id>}, ...]}

The village id, coordinates and route ids from that capture are deliberately
NOT reproduced here -- this repository is public, and they identify a real
account. What matters for the wire format is the field names, the nesting and
the types, so stand-in values are used throughout.

The key-set assertions are the point of this file. An extra field the real
client never sends is a bot fingerprint, so "exactly these keys" is a stronger
and more useful property than "at least these keys".
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    ExistingRoute,
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)

# Exactly the keys the real client sent, in the create body.
CAPTURED_CREATE_KEYS = {
    "action",
    "sourceVillageId",
    "targetCoordinates",
    "resources",
    "mode",
    "hour",
    "minute",
    "deliveries",
    "repeatEvery",
    "enabled",
    "useTradeShips",
}


class _RecordingClient:
    """Records the verb, url and body of anything sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict]] = []

        class _Delay:
            @staticmethod
            async def wait(*_args, **_kwargs) -> None:
                return None

        self.human_delay = _Delay()
        self.settings = SimpleNamespace(base_url="https://ts2.x1.europe.travian.com")
        # Every write feeds the seconds it consumed into the daily activity
        # ceiling. Without this attribute _log_activity raises AttributeError
        # into its own broad catch, so the accounting silently does nothing --
        # and no test notices, because the write itself still succeeds.
        self.logged_activity: list[float] = []
        self.activity_scheduler = SimpleNamespace(
            log_activity=self.logged_activity.append
        )

    async def post_json(self, url: str, payload: dict, **_kwargs):
        self.sent.append(("POST", url, payload))
        return {}

    async def put_json(self, url: str, payload: dict, **_kwargs):
        self.sent.append(("PUT", url, payload))
        return {}


def _service() -> tuple[TradeRouteService, _RecordingClient]:
    client = _RecordingClient()
    # reconciler_verified: this file tests the WIRE SHAPE, not the reconciler.
    # The gate that normally blocks a create is exercised in
    # tests/test_trade_route_reconciler_gate.py.
    return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client


def _route(*, dispatch_minute: int = 15 * 60 + 27, cycle_hours: int = 1) -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20031,
        dest_village_id=20044,
        dest_x=23,
        dest_y=88,
        dest_name="capital",
        cargo={Resource.LUMBER: 1, Resource.CLAY: 2, Resource.IRON: 3, Resource.CROP: 4},
        cycle_hours=cycle_hours,
        merchants=4,
        dispatch_minute=dispatch_minute,
    )


class TestCreateBody:
    def test_it_sends_exactly_the_captured_field_set(self):
        service, _ = _service()
        payload = service._build_create_payload(_route())
        assert set(payload) == CAPTURED_CREATE_KEYS

    def test_it_matches_the_capture_field_for_field(self):
        service, _ = _service()
        assert service._build_create_payload(_route()) == {
            "action": "traderoute",
            "sourceVillageId": 20031,
            "targetCoordinates": {"x": 23, "y": 88},
            "resources": {"lumber": 1, "clay": 2, "iron": 3, "crop": 4},
            "mode": "send",
            "hour": 15,
            "minute": 27,
            "deliveries": 1,
            "repeatEvery": 1,
            "enabled": True,
            "useTradeShips": False,
        }

    def test_the_destination_is_nested_not_flat(self):
        # The earlier guess sent top-level x/y. The client nests them.
        service, _ = _service()
        payload = service._build_create_payload(_route())
        assert payload["targetCoordinates"] == {"x": 23, "y": 88}
        assert "x" not in payload and "y" not in payload

    def test_the_merchant_count_is_never_sent(self):
        # The game derives it from the cargo; sending it would be an extra
        # field, and the planner's figure is for budgeting only.
        service, _ = _service()
        assert "merchants" not in service._build_create_payload(_route(cycle_hours=3))

    @pytest.mark.parametrize(
        "dispatch_minute,hour,minute",
        [(0, 0, 0), (90, 1, 30), (15 * 60 + 27, 15, 27), (23 * 60 + 59, 23, 59)],
    )
    def test_the_send_time_is_split_into_hour_and_minute(self, dispatch_minute, hour, minute):
        # This is what makes the planner's beat realisable: the send time is
        # chosen at creation, not fixed to the moment of the click.
        service, _ = _service()
        payload = service._build_create_payload(_route(dispatch_minute=dispatch_minute))
        assert (payload["hour"], payload["minute"]) == (hour, minute)

    def test_the_cycle_is_carried_as_repeat_every(self):
        service, _ = _service()
        assert service._build_create_payload(_route(cycle_hours=8))["repeatEvery"] == 8

    def test_all_four_resources_are_always_present(self):
        service, _ = _service()
        route = _route()
        route = PlannedRoute(
            origin_village_id=route.origin_village_id,
            dest_village_id=route.dest_village_id,
            dest_x=route.dest_x,
            dest_y=route.dest_y,
            dest_name=route.dest_name,
            cargo={Resource.CROP: 5_000},  # crop only
            cycle_hours=route.cycle_hours,
            merchants=route.merchants,
            dispatch_minute=route.dispatch_minute,
        )
        resources = service._build_create_payload(route)["resources"]
        assert resources == {"lumber": 0, "clay": 0, "iron": 0, "crop": 5_000}

    def test_it_posts_to_the_collection_endpoint(self):
        service, client = _service()
        asyncio.run(service.create_route(_route()))
        assert [(verb, url) for verb, url, _ in client.sent] == [("POST", "/api/v1/trade-routes")]


class TestToggleBody:
    def test_it_matches_the_capture(self):
        service, _ = _service()
        assert service._build_toggle_payload([647196, 647197], active=False) == {
            "action": "traderoute",
            "routes": [
                {"enabled": False, "id": 647196},
                {"enabled": False, "id": 647197},
            ],
        }

    def test_enabling_flips_the_same_shape(self):
        # Only the disable direction was captured; the enable direction is the
        # identical body with enabled=true, which is why they share a builder.
        service, _ = _service()
        payload = service._build_toggle_payload([1, 2], active=True)
        assert all(entry["enabled"] is True for entry in payload["routes"])

    def test_it_is_a_PUT_to_the_collection_not_a_toggle_group_POST(self):
        # The earlier guess was POST /api/v1/trade-routes/toggle-group. Both
        # the verb and the path were wrong.
        service, client = _service()
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=5, dest_y=6)]
        asyncio.run(service.disable_routes(20031, routes))
        assert [(verb, url) for verb, url, _ in client.sent] == [("PUT", "/api/v1/trade-routes")]

    def test_every_route_goes_in_one_request(self):
        service, client = _service()
        routes = [
            ExistingRoute(route_id=i, dest_village_id=20044, dest_x=0, dest_y=0)
            for i in range(24)
        ]
        asyncio.run(service.disable_routes(20031, routes))
        assert len(client.sent) == 1, "the capture toggled 24 routes in a single request"
        assert len(client.sent[0][2]["routes"]) == 24

    def test_nothing_is_sent_when_there_is_nothing_to_toggle(self):
        service, client = _service()
        assert asyncio.run(service.disable_routes(20031, [])) is None
        assert client.sent == []


class TestWritesConsumeActivityBudget:
    """A trade-route write costs time against the daily activity ceiling.

    The ceiling is what keeps the account's total daily traffic inside a human
    range. A whole execute run that reported zero seconds would let the rest of
    the day's automation spend a budget it had already used -- and because
    _log_activity swallows its own failures by design (accounting must never
    break a request that already went out), nothing surfaces when it silently
    stops working. Hence an explicit assertion.
    """

    def test_a_create_reports_the_time_it_took(self):
        service, client = _service()
        asyncio.run(service.create_route(_route()))

        assert len(client.logged_activity) == 1, "the create must be accounted for"
        assert client.logged_activity[0] >= 0.0

    def test_a_toggle_reports_the_time_it_took(self):
        service, client = _service()
        routes = [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=5, dest_y=6)]
        asyncio.run(service.disable_routes(20031, routes))

        assert len(client.logged_activity) == 1

    def test_a_refused_write_costs_nothing(self):
        # Nothing was sent, so nothing was consumed.
        client = _RecordingClient()
        service = TradeRouteService(client, live_enabled=False, reconciler_verified=True)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(service.create_route(_route()))

        assert client.logged_activity == []
