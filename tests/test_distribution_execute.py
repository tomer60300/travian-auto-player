"""The trade-route execute endpoint + service.

The dry-run path is the safe, fully-functional deliverable: it recomputes the
plan server-side and previews exactly what would be created/disabled with zero
game requests. Live execution is gated until the /api/v1/trade-routes payload
is captured. These pin dry-run behavior, coordinate resolution (incl. foreign
sinks), the per-run cap, the live gate, honeypot filtering, and the service's
payload-unverified guard.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.parsers.html_parser import parse_trade_routes
from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    ExistingRoute,
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)
from travian_api.web.routes.distribution import ExecuteRequest, ForeignTarget, post_execute


def _exec_body(dry_run=True, disable_existing=True, max_routes_per_run=3, margin=0.0):
    body = ExecuteRequest.model_validate(
        {
            "snapshot": [
                {
                    "village_id": 20003,
                    "name": "03",
                    "x": 0,
                    "y": 0,
                    "merchants_total": 20,
                    "merchants_free": 20,
                    "lumber_per_hour": 2000,
                    "clay_per_hour": 1000,
                    "iron_per_hour": 1000,
                    "crop_per_hour": 3000,
                },
                {
                    "village_id": 20011,
                    "name": "11",
                    "x": 10,
                    "y": 0,
                    "merchants_total": 20,
                    "merchants_free": 20,
                    "lumber_per_hour": 500,
                    "clay_per_hour": 500,
                    "iron_per_hour": 500,
                    "crop_per_hour": 3000,
                },
            ],
            "allocations": {
                "crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}
            },
            "dry_run": dry_run,
            "disable_existing": disable_existing,
            "max_routes_per_run": max_routes_per_run,
        }
    )
    body.foreign_targets = [
        ForeignTarget(name="Ally-Keep", x=40, y=40, crop_per_hour=500.0, safety_margin_pct=margin)
    ]
    return body


def _session(live_enabled=False):
    return SimpleNamespace(trade_route_service=SimpleNamespace(live_enabled=live_enabled))


class TestDryRun:
    def test_previews_routes_without_touching_the_game(self):
        # A session with only `live_enabled` — if the endpoint called any service
        # method, this stub would raise AttributeError.
        res = asyncio.run(post_execute(_exec_body(dry_run=True), _session()))
        assert res.dry_run is True
        assert res.created == 0
        assert res.actions, "the plan ships a tribute, so there is at least one route"
        assert all(a.status in ("would_create", "deferred") for a in res.actions)

    def test_resolves_foreign_sink_coordinates(self):
        res = asyncio.run(post_execute(_exec_body(dry_run=True), _session()))
        tribute = [a for a in res.actions if a.destination < 0]
        assert tribute, "the foreign tribute row must be present"
        # Coordinates come from the operator-entered foreign target (40|40),
        # resolved server-side for the negative sink id.
        assert (tribute[0].dest_x, tribute[0].dest_y) == (40, 40)
        assert tribute[0].destination_name == "Ally-Keep"

    def test_per_run_cap_defers_the_rest(self):
        res = asyncio.run(post_execute(_exec_body(dry_run=True, max_routes_per_run=1), _session()))
        would = [a for a in res.actions if a.status == "would_create"]
        deferred = [a for a in res.actions if a.status == "deferred"]
        assert len(would) <= 1
        assert res.remaining == len(deferred)

    def test_disable_note_is_surfaced_but_no_request_made(self):
        res = asyncio.run(post_execute(_exec_body(dry_run=True, disable_existing=True), _session()))
        assert any("disabled first" in d for d in res.disables)


class TestLiveGate:
    def test_live_without_the_flag_is_409(self):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(post_execute(_exec_body(dry_run=False), _session(live_enabled=False)))
        assert exc.value.status_code == 409
        assert "payload" in exc.value.detail.lower()


class _FakeLiveSvc:
    """A live-enabled trade-route service that records calls instead of hitting
    the game — every marketplace it reads already has a route to (40|40)."""

    def __init__(self, create_status="created"):
        self.live_enabled = True
        self._create_status = create_status
        self.created = []  # PlannedRoute objects a create was attempted for
        self.disabled = []  # origin ids disabled
        self.listed = []  # origin ids whose marketplace was read

    def origin_lock(self, vid):
        import contextlib

        @contextlib.asynccontextmanager
        async def _cm():
            yield

        return _cm()

    async def list_existing_routes(self, vid):
        self.listed.append(vid)
        return [ExistingRoute(route_id=1, dest_x=40, dest_y=40)]

    async def disable_routes(self, vid, routes):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        self.disabled.append(vid)
        return RouteActionResult(vid, 0, 0, "disabled", str(len(routes)))

    async def create_route(self, route):
        from travian_api.services.trade_route_service import RouteActionResult

        self.created.append(route)
        return RouteActionResult(
            route.origin_village_id, route.dest_x, route.dest_y, self._create_status
        )


class TestLiveExecution:
    def _run(self, svc, **kw):
        body = _exec_body(dry_run=False, **kw)
        return asyncio.run(post_execute(body, SimpleNamespace(trade_route_service=svc)))

    def test_no_origin_is_disabled_without_a_create(self):
        # The route-loss bug: an origin disabled but never recreated this run.
        svc = _FakeLiveSvc()
        self._run(svc, max_routes_per_run=5)
        created_origins = {r.origin_village_id for r in svc.created}
        assert set(svc.disabled) <= created_origins, (
            "every disabled origin must get at least one create this run"
        )

    def test_disable_existing_recreates_the_matching_route(self):
        # The disable-then-skip bug: a route to (40|40) is disabled and then
        # skipped as 'already exists', leaving it gone. With disable_existing it
        # must be recreated.
        svc = _FakeLiveSvc()
        self._run(svc, disable_existing=True)
        dests = {(r.dest_x, r.dest_y) for r in svc.created}
        assert (40, 40) in dests, "the disabled tribute route must be recreated"

    def test_without_disable_existing_the_active_route_is_skipped(self):
        svc = _FakeLiveSvc()
        res = self._run(svc, disable_existing=False)
        assert not svc.disabled, "disable_existing=False must disable nothing"
        assert any(a.status == "skipped" for a in res.actions), (
            "an already-active route must be left in place, not duplicated"
        )

    def test_creates_never_exceed_the_cap(self):
        svc = _FakeLiveSvc(create_status="failed")  # failures must not lift the cap
        self._run(svc, max_routes_per_run=1)
        assert len(svc.created) <= 1, "the cap bounds create ATTEMPTS, not successes"

    def test_dry_run_preview_matches_the_live_disable_set(self):
        dry = asyncio.run(
            post_execute(
                _exec_body(dry_run=True), SimpleNamespace(trade_route_service=_FakeLiveSvc())
            )
        )
        svc = _FakeLiveSvc()
        live = self._run(svc)
        assert dry.remaining == live.remaining
        assert len(dry.disables) == len(svc.disabled)


class TestServiceGuards:
    def _svc(self, live_enabled=False):
        return TradeRouteService(http_client=SimpleNamespace(), live_enabled=live_enabled)

    def test_create_route_refuses_until_payload_verified(self):
        svc = self._svc(live_enabled=False)
        route = PlannedRoute(20003, 40, 40, "Ally-Keep", {Resource.CROP: 500}, 3, 2)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(svc.create_route(route))

    def test_disable_with_nothing_makes_no_request_and_no_guard(self):
        svc = self._svc(live_enabled=False)
        # Empty list returns None before the live guard — nothing to disable.
        assert asyncio.run(svc.disable_routes(20003, [])) is None

    def test_disable_with_routes_refuses_until_verified(self):
        svc = self._svc(live_enabled=False)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(svc.disable_routes(20003, [ExistingRoute(1, 40, 40)]))


class TestTradeRouteParser:
    def test_hidden_routes_are_flagged_not_visible(self):
        html = """
        <div data-route-id="1" data-x="40" data-y="40"></div>
        <div data-route-id="2" data-x="12" data-y="99" style="display:none"></div>
        <div data-route-id="3" data-x="5" data-y="5" hidden></div>
        """
        routes = {r["route_id"]: r for r in parse_trade_routes(html)}
        assert routes[1]["visible"] is True
        assert routes[2]["visible"] is False
        assert routes[3]["visible"] is False

    def test_unknown_markup_yields_nothing(self):
        # Safe default: no recognizable routes → nothing to disable.
        assert parse_trade_routes("<html><body>marketplace</body></html>") == []

    def test_row_without_coordinates_is_skipped_not_guessed(self):
        assert parse_trade_routes('<div data-route-id="7">no coords</div>') == []
