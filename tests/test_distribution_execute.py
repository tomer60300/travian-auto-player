"""The trade-route execute endpoint + service.

The dry-run path is the safe, fully-functional deliverable: it recomputes the
plan server-side and previews the desired routes with zero game requests. Live
execution (gated until the /api/v1/trade-routes payload is captured) reconciles
that plan against each marketplace — creating only missing routes, disabling
only stale ones, never touching honeypots. These pin dry-run behavior,
coordinate resolution (incl. foreign sinks), the per-run cap, convergence across
runs, stale-vs-plan disabling, honeypot handling, and the payload-unverified
guard.
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.parsers.html_parser import parse_trade_routes
from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.planner import SheetRow
from travian_api.services.trade_route_service import (
    ExistingRoute,
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)
from travian_api.web.routes import distribution as dist_module
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


_USER = SimpleNamespace(id=1)


class _SessMgr:
    """Stand-in for the module-level session_manager the endpoint reads."""

    def __init__(self, svc, connected):
        self._svc = svc
        self._connected = connected

    def get(self, _uid):
        if not self._connected:
            return None
        return SimpleNamespace(trade_route_service=self._svc)


def _execute(body, *, svc=None, connected=True):
    """Call post_execute with session_manager pointed at a fake session.

    ``connected=False`` simulates a disconnected user (no session); otherwise
    the endpoint sees a session whose trade_route_service is ``svc``.
    """
    mgr = _SessMgr(svc, connected)
    with _patch(dist_module, "session_manager", mgr):
        return asyncio.run(post_execute(body, _USER))


@contextlib.contextmanager
def _patch(obj, name, value):
    original = getattr(obj, name)
    setattr(obj, name, value)
    try:
        yield
    finally:
        setattr(obj, name, original)


def _dry_svc(live_enabled=False):
    # Dry-run only reads `live_enabled`; any method call would raise here.
    return SimpleNamespace(live_enabled=live_enabled)


class TestDryRun:
    def test_previews_routes_without_touching_the_game(self):
        res = _execute(_exec_body(dry_run=True), svc=_dry_svc())
        assert res.dry_run is True
        assert res.created == 0
        assert res.actions, "the plan ships a tribute, so there is at least one route"
        assert all(a.status in ("would_create", "deferred") for a in res.actions)

    def test_preview_works_while_disconnected(self):
        # Like /plan, the zero-request preview must not require a live session.
        res = _execute(_exec_body(dry_run=True), connected=False)
        assert res.dry_run is True
        assert res.live_enabled is False
        assert res.actions

    def test_resolves_foreign_sink_coordinates(self):
        res = _execute(_exec_body(dry_run=True), svc=_dry_svc())
        tribute = [a for a in res.actions if a.destination < 0]
        assert tribute, "the foreign tribute row must be present"
        # Coordinates come from the operator-entered foreign target (40|40),
        # resolved server-side for the negative sink id.
        assert (tribute[0].dest_x, tribute[0].dest_y) == (40, 40)
        assert tribute[0].destination_name == "Ally-Keep"

    def test_per_run_cap_defers_the_rest(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=1), svc=_dry_svc())
        would = [a for a in res.actions if a.status == "would_create"]
        deferred = [a for a in res.actions if a.status == "deferred"]
        assert len(would) <= 1
        assert res.remaining == len(deferred)

    def test_disable_note_is_surfaced_but_no_request_made(self):
        res = _execute(_exec_body(dry_run=True, disable_existing=True), svc=_dry_svc())
        assert any("disabled first" in d for d in res.disables)


class TestLiveGate:
    def test_live_without_the_flag_is_409(self):
        with pytest.raises(HTTPException) as exc:
            _execute(_exec_body(dry_run=False), svc=_dry_svc(live_enabled=False))
        assert exc.value.status_code == 409
        assert "payload" in exc.value.detail.lower()

    def test_live_without_a_session_is_403(self):
        with pytest.raises(HTTPException) as exc:
            _execute(_exec_body(dry_run=False), connected=False)
        assert exc.value.status_code == 403

    def test_live_refuses_an_infeasible_plan(self):
        # The UI disables the button on !feasible; the server must refuse too, so
        # a direct API call can't commit an over-budget plan.
        async def _fake_plan(_body):
            plan = SimpleNamespace(is_feasible=False, warnings=("merchants over budget",), rows=())
            return SimpleNamespace(plan=plan, names={}, coords={}, warnings=[])

        with (
            _patch(dist_module, "_plan_account", _fake_plan),
            pytest.raises(HTTPException) as exc,
        ):
            _execute(_exec_body(dry_run=False), svc=_dry_svc(live_enabled=True))
        assert exc.value.status_code == 422
        assert "feasible" in exc.value.detail.lower()


def _desired_routes():
    """The routes the plan wants — discovered via a zero-request dry-run — so
    the live tests can seed marketplaces without hard-coding optimizer output."""
    res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
    return [a for a in res.actions if a.status in ("would_create", "deferred")]


class _FakeLiveSvc:
    """A live-enabled trade-route service that records calls instead of hitting
    the game. ``existing`` maps origin id → the routes already on its marketplace."""

    def __init__(self, existing=None, create_status="created"):
        self.live_enabled = True
        self._existing = existing or {}
        self._create_status = create_status
        self.created = []  # PlannedRoute objects a create was ATTEMPTED for
        self.disabled = []  # (origin, sorted tuple of disabled dest coords)
        self.listed = []  # origin ids whose marketplace was READ

    def origin_lock(self, vid):
        @contextlib.asynccontextmanager
        async def _cm():
            yield

        return _cm()

    async def list_existing_routes(self, vid):
        self.listed.append(vid)
        return list(self._existing.get(vid, []))

    async def disable_routes(self, vid, routes):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        self.disabled.append((vid, tuple(sorted((r.dest_x, r.dest_y) for r in routes))))
        return RouteActionResult(vid, 0, 0, "disabled", f"{len(routes)} route(s)")

    async def create_route(self, route):
        from travian_api.services.trade_route_service import RouteActionResult

        self.created.append(route)
        return RouteActionResult(
            route.origin_village_id, route.dest_x, route.dest_y, self._create_status
        )


class TestLiveExecution:
    def _run(self, svc, **kw):
        return _execute(_exec_body(dry_run=False, **kw), svc=svc)

    def test_missing_routes_are_created(self):
        desired = _desired_routes()
        svc = _FakeLiveSvc()  # empty marketplaces → everything is missing
        self._run(svc, disable_existing=False, max_routes_per_run=50)
        assert {(r.dest_x, r.dest_y) for r in svc.created} == {
            (a.dest_x, a.dest_y) for a in desired
        }

    def test_incremental_leaves_active_routes_untouched(self):
        desired = _desired_routes()
        existing = {}
        for a in desired:
            existing.setdefault(a.origin, []).append(ExistingRoute(1, a.dest_x, a.dest_y))
        svc = _FakeLiveSvc(existing=existing)
        res = self._run(svc, disable_existing=False, max_routes_per_run=50)
        assert svc.created == [], "incremental mode must not recreate active routes"
        assert svc.disabled == [], "incremental mode disables nothing"
        assert all(a.status == "skipped" for a in res.actions)

    def test_incremental_second_run_creates_nothing_new(self):
        # Convergence without churn: once routes exist, a later incremental run
        # skips them all instead of rebuilding (a daily-rebuild bot signal).
        svc = _FakeLiveSvc()
        self._run(svc, disable_existing=False, max_routes_per_run=50)
        assert len(svc.created) > 0
        for r in svc.created:  # marketplace now reflects run 1's creations
            svc._existing.setdefault(r.origin_village_id, []).append(
                ExistingRoute(1, r.dest_x, r.dest_y)
            )
        svc.created.clear()
        self._run(svc, disable_existing=False, max_routes_per_run=50)
        assert svc.created == [], "a second incremental run must not rebuild"

    def test_disable_existing_rebuilds_the_origin(self):
        # Rebuild mode applies parameter changes: a destination that already has
        # a route is disabled and recreated (coord-only dedup can't diff params),
        # and stale routes are cleared.
        desired = _desired_routes()
        a = desired[0]
        plan_dest = (a.dest_x, a.dest_y)
        existing = {
            a.origin: [
                ExistingRoute(1, *plan_dest),  # the plan still wants this dest
                ExistingRoute(2, 99, 98),  # stale — plan no longer wants it
            ]
        }
        svc = _FakeLiveSvc(existing=existing)
        self._run(svc, disable_existing=True, max_routes_per_run=50)
        disabled_coords = {c for _, coords in svc.disabled for c in coords}
        assert {plan_dest, (99, 98)} <= disabled_coords, "rebuild disables all visible routes"
        assert plan_dest in {(r.dest_x, r.dest_y) for r in svc.created}, (
            "the wanted destination is recreated with the plan's current parameters"
        )

    def test_hidden_honeypot_is_ignored_entirely(self):
        # A hidden route is invisible to a human, so the reconciler behaves like
        # a human who can't see it: it neither blocks a create nor gets disabled.
        desired = _desired_routes()
        a = desired[0]
        existing = {
            a.origin: [
                ExistingRoute(1, a.dest_x, a.dest_y, visible=False),  # honeypot at a plan dest
                ExistingRoute(2, 97, 96, visible=False),  # honeypot at a non-plan dest
            ]
        }
        svc = _FakeLiveSvc(existing=existing)
        self._run(svc, disable_existing=True, max_routes_per_run=50)
        created_coords = {(r.dest_x, r.dest_y) for r in svc.created}
        assert (a.dest_x, a.dest_y) in created_coords, "a route the plan wants is still created"
        disabled_coords = {c for _, coords in svc.disabled for c in coords}
        assert (97, 96) not in disabled_coords, "a hidden honeypot must never be disabled"

    def test_duplicate_destination_rows_create_once(self):
        # Two plan rows to the same origin+destination (e.g. two foreign targets
        # at identical coords) must not create a duplicate route in one run.
        row = SheetRow(
            origin=20003,
            destination=-1,
            cargo={Resource.CROP: 100},
            cycle_hours=6,
            dispatch_minute=0,
            arrival_minute=0,
            merchants=2,
        )
        plan = SimpleNamespace(is_feasible=True, warnings=(), rows=(row, row))
        account = SimpleNamespace(
            plan=plan,
            names={20003: "03", -1: "Ally"},
            coords={20003: (0, 0), -1: (40, 40)},
            warnings=[],
        )

        async def _fake_plan(_body):
            return account

        svc = _FakeLiveSvc()
        with _patch(dist_module, "_plan_account", _fake_plan):
            res = self._run(svc, max_routes_per_run=50)
        created = [(r.dest_x, r.dest_y) for r in svc.created]
        assert created.count((40, 40)) == 1, "the same destination is created only once"
        assert any(a.status == "skipped" for a in res.actions)

    def test_cap_bounds_create_attempts_even_on_failure(self):
        svc = _FakeLiveSvc(create_status="failed")  # failures must not lift the cap
        res = self._run(svc, max_routes_per_run=1)
        assert len(svc.created) <= 1, "the cap bounds create ATTEMPTS, not successes"
        # A failed create is still outstanding work, so it counts toward remaining.
        assert res.remaining >= 1

    def test_reads_stop_once_the_cap_is_reached(self):
        # Not just creates: marketplace READS (and disables) are bounded per run,
        # so one run touches only the villages it needs — not a full sweep.
        svc = _FakeLiveSvc()  # empty marketplaces → the first origin has work
        self._run(svc, max_routes_per_run=1)
        assert len(svc.listed) == 1, "no further marketplaces are read after the cap"

    def test_reads_are_bounded_when_all_routes_exist(self):
        # Steady state (incremental): everything already provisioned, so every
        # route is skipped and NO create fires. A create-only cap would never
        # trip and the loop would sweep every village; the origins-VISITED cap
        # prevents that.
        desired = _desired_routes()
        existing = {}
        for a in desired:
            existing.setdefault(a.origin, []).append(ExistingRoute(1, a.dest_x, a.dest_y))
        svc = _FakeLiveSvc(existing=existing)
        self._run(svc, disable_existing=False, max_routes_per_run=1)
        assert svc.created == []
        assert len(svc.listed) <= 1, "a fully-provisioned account must not sweep every village"

    def test_gold_club_rejection_aborts_the_run(self):
        # A human without Gold Club would not keep firing rejected creates.
        svc = _FakeLiveSvc(create_status="skipped")  # plus.error_goldclub
        res = self._run(svc, max_routes_per_run=3)
        assert res.created == 0
        assert len(svc.created) == 1, "stop after the first Gold-Club rejection, no burst"
        assert res.remaining >= 1, "the rejected route is still outstanding"
        assert any("Gold Club" in w for w in res.warnings)


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
