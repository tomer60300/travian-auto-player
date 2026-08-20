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
from travian_api.web.operation_gate import active_ops, captcha_stop
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
        # route_eligible so the tribute is actually shipped as a route (a WW /
        # artifact / own village); ineligible foreign targets are covered by the
        # planner-side tests.
        ForeignTarget(
            name="Ally-Keep",
            x=40,
            y=40,
            crop_per_hour=500.0,
            safety_margin_pct=margin,
            route_eligible=True,
        )
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

    def test_concurrent_execute_is_rejected(self):
        # A second live run for the same account while one is in flight is a 409,
        # so a double-click can't fire two reconciliations that bypass the caps.
        svc = _FakeLiveSvc()

        async def _run() -> int:
            async with svc.execute_lock:  # simulate an in-flight run holding it
                mgr = _SessMgr(svc, connected=True)
                with (
                    _patch(dist_module, "session_manager", mgr),
                    pytest.raises(HTTPException) as exc,
                ):
                    await post_execute(_exec_body(dry_run=False), _USER)
                return exc.value.status_code

        assert asyncio.run(_run()) == 409


def _desired_routes():
    """The routes the plan wants — discovered via a zero-request dry-run — so
    the live tests can seed marketplaces without hard-coding optimizer output."""
    res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
    return [a for a in res.actions if a.status in ("would_create", "deferred")]


class _FakeLiveSvc:
    """A live-enabled trade-route service that records calls instead of hitting
    the game. ``existing`` maps origin id → the routes already on its marketplace."""

    def __init__(
        self,
        existing=None,
        create_status="created",
        disable_status="disabled",
        enable_status="enabled",
        budget_ok=True,
        read_raises=None,
    ):
        self.live_enabled = True
        self._existing = existing or {}
        self._create_status = create_status
        self._disable_status = disable_status
        self._enable_status = enable_status
        self._read_raises = read_raises or set()  # origin ids whose read raises
        self.created = []  # PlannedRoute objects a create was ATTEMPTED for
        self.disabled = []  # (origin, sorted tuple of disabled dest coords)
        self.enabled = []  # (origin, sorted tuple of re-enabled dest coords)
        self.listed = []  # origin ids whose marketplace was READ
        self.execute_lock = asyncio.Lock()
        self.on_create = None  # optional hook fired after a create is recorded
        self.on_pacing = None  # optional hook fired inside create_route BEFORE its
        # stop_check — simulates a signal arriving during the pacing wait
        self.on_read = None  # optional hook fired inside list_existing_routes
        self.budget_ok = budget_ok  # mutable: a test can exhaust it mid-run
        budget_checks = self._budget_checks = []

        def _check_budget():
            budget_checks.append(True)
            if not self.budget_ok:
                from travian_api.exceptions import ActivityBudgetExhausted

                raise ActivityBudgetExhausted("budget exhausted (test)")
            return True

        self.http_client = SimpleNamespace(check_activity_budget=_check_budget)

    def origin_lock(self, vid):
        @contextlib.asynccontextmanager
        async def _cm():
            yield

        return _cm()

    async def list_existing_routes(self, vid):
        self.listed.append(vid)
        if self.on_read is not None:
            self.on_read()  # e.g. signal captcha / exhaust budget DURING the read
        if vid in self._read_raises:
            from travian_api.exceptions import NetworkError

            raise NetworkError("marketplace read failed (test)")
        return list(self._existing.get(vid, []))

    async def disable_routes(self, vid, routes, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(vid, 0, 0, "stopped", reason)
        self.disabled.append((vid, tuple(sorted((r.dest_x, r.dest_y) for r in routes))))
        return RouteActionResult(vid, 0, 0, self._disable_status, f"{len(routes)} route(s)")

    async def enable_routes(self, vid, routes, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(vid, 0, 0, "stopped", reason)
        self.enabled.append((vid, tuple(sorted((r.dest_x, r.dest_y) for r in routes))))
        return RouteActionResult(vid, 0, 0, self._enable_status, f"{len(routes)} route(s)")

    async def create_route(self, route, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if self.on_pacing is not None:
            self.on_pacing()  # something happens during the (simulated) pacing wait
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "stopped", reason
            )
        self.created.append(route)
        if self.on_create is not None:
            self.on_create()
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
        assert res.problems == [], "a clean run reports no execution problems"

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

    def test_disable_existing_disables_stale_but_keeps_matching(self):
        # disable_existing clears only routes the plan no longer wants; a route
        # the plan still wants is left in place — never disabled (no churn) and
        # never a disable-then-create on the same coord (no duplicate risk).
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
        res = self._run(svc, disable_existing=True, max_routes_per_run=50)
        disabled_coords = {c for _, coords in svc.disabled for c in coords}
        assert (99, 98) in disabled_coords, "the stale route is disabled"
        assert plan_dest not in disabled_coords, "a wanted route is kept, not churned"
        assert plan_dest not in {(r.dest_x, r.dest_y) for r in svc.created}, (
            "a route that already exists is not recreated (params are not diffed)"
        )
        assert any(a.status == "skipped" for a in res.actions)

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
        # A Gold-Club block is a problem (not a benign planner warning).
        assert any("Gold Club" in p for p in res.problems)
        assert res.warnings == [] or all("Gold Club" not in w for w in res.warnings)
        # It is "blocked", distinct from an "already active" skip.
        assert any(a.status == "blocked" for a in res.actions)
        assert not any(a.status == "skipped" for a in res.actions)

    def test_failed_disable_is_surfaced_as_a_problem(self):
        # A failed disable leaves stale routes live — it must land in `problems`
        # (a real failure), not `warnings` (benign notes), so the run does not
        # read as a clean success.
        desired = _desired_routes()
        a = desired[0]
        existing = {a.origin: [ExistingRoute(1, 99, 98)]}  # stale, plan doesn't want it
        svc = _FakeLiveSvc(existing=existing, disable_status="failed")
        res = self._run(svc, disable_existing=True, max_routes_per_run=50)
        assert any("disable" in p.lower() for p in res.problems), (
            "a failed disable must surface as a problem, not a silent note"
        )
        assert all("disable" not in w.lower() for w in res.warnings)


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
        <div data-route-id="4" data-x="6" data-y="6" class="row hidden"></div>
        """
        routes = {r["route_id"]: r for r in parse_trade_routes(html)}
        assert routes[1]["visible"] is True
        assert routes[2]["visible"] is False
        assert routes[3]["visible"] is False
        # A stylesheet-hidden honeypot (class, not inline style) is caught too.
        assert routes[4]["visible"] is False

    def test_unknown_markup_yields_nothing(self):
        # Safe default: no recognizable routes → nothing to disable.
        assert parse_trade_routes("<html><body>marketplace</body></html>") == []

    def test_row_without_coordinates_is_skipped_not_guessed(self):
        assert parse_trade_routes('<div data-route-id="7">no coords</div>') == []

    def test_disabled_route_is_flagged_inactive(self):
        html = """
        <div data-route-id="1" data-x="40" data-y="40"></div>
        <div data-route-id="2" data-x="5" data-y="5" data-active="false"></div>
        <div data-route-id="3" data-x="6" data-y="6" class="route disabled"></div>
        """
        routes = {r["route_id"]: r for r in parse_trade_routes(html)}
        assert routes[1]["active"] is True
        assert routes[2]["active"] is False
        assert routes[3]["active"] is False


def _two_origin_account():
    """A feasible plan: two origins → two destinations, with distinct dispatch
    minutes — for hardening tests that need deterministic multi-origin control."""
    rows = (
        SheetRow(
            origin=20003,
            destination=-1,
            cargo={Resource.CROP: 100},
            cycle_hours=6,
            dispatch_minute=100,
            arrival_minute=0,
            merchants=2,
        ),
        SheetRow(
            origin=20011,
            destination=-2,
            cargo={Resource.CROP: 100},
            cycle_hours=6,
            dispatch_minute=700,
            arrival_minute=0,
            merchants=2,
        ),
    )
    plan = SimpleNamespace(is_feasible=True, warnings=(), rows=rows)
    return SimpleNamespace(
        plan=plan,
        names={20003: "03", 20011: "11", -1: "A", -2: "B"},
        coords={20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
        warnings=[],
    )


def _row(origin, destination, x, y, dispatch_minute=0):
    return SheetRow(
        origin=origin,
        destination=destination,
        cargo={Resource.CROP: 100},
        cycle_hours=6,
        dispatch_minute=dispatch_minute,
        arrival_minute=0,
        merchants=2,
    )


def _account(rows, coords, names):
    plan = SimpleNamespace(is_feasible=True, warnings=(), rows=tuple(rows))
    return SimpleNamespace(plan=plan, names=names, coords=coords, warnings=[])


def _same_origin_account():
    """Two desired routes from ONE origin (20003) — for same-origin stop tests."""
    return _account(
        [_row(20003, -1, 40, 40, 100), _row(20003, -2, 50, 50, 700)],
        {20003: (0, 0), -1: (40, 40), -2: (50, 50)},
        {20003: "03", -1: "A", -2: "B"},
    )


def _three_origin_account():
    """Three origins in insertion order [20003, 20011, 20019]."""
    return _account(
        [_row(20003, -1, 40, 40), _row(20011, -2, 50, 50), _row(20019, -3, 60, 60)],
        {20003: (0, 0), 20011: (10, 0), 20019: (20, 0), -1: (40, 40), -2: (50, 50), -3: (60, 60)},
        {20003: "03", 20011: "11", 20019: "19", -1: "A", -2: "B", -3: "C"},
    )


def _run_live(svc, account, **body_kw):
    """Live-execute against an injected plan, with shuffle disabled so origins
    stay in insertion order for deterministic assertions."""

    async def _plan(_body):
        return account

    with (
        _patch(dist_module, "_plan_account", _plan),
        _patch(dist_module.random, "shuffle", lambda seq: None),
    ):
        return _execute(_exec_body(dry_run=False, **body_kw), svc=svc)


class TestExecutionHardening:
    def test_dispatch_minute_survives_into_created_routes(self):  # #58
        svc = _FakeLiveSvc()  # empty marketplaces → both created
        _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert {r.dispatch_minute for r in svc.created} == {100, 700}, (
            "each route's scheduled send time must reach the create payload"
        )

    def test_create_payload_serializes_the_send_time(self):  # #58
        # Was pinning the guessed startMinute/startTime pair. The captured
        # client request splits the send time into two integers instead; the
        # full wire format lives in tests/test_trade_route_payload.py.
        route = PlannedRoute(20003, 40, 40, "A", {Resource.CROP: 100}, 6, 2, dispatch_minute=615)
        payload = TradeRouteService(http_client=SimpleNamespace())._build_create_payload(route)
        assert payload["hour"] == 10
        assert payload["minute"] == 15

    def test_disabled_desired_route_is_re_enabled_not_duplicated(self):  # #60
        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(1, 40, 40, active=False)]})
        _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert (20003, ((40, 40),)) in svc.enabled, "a disabled desired route must be re-enabled"
        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}, "no duplicate create"

    def test_a_disabled_route_does_not_count_as_active(self):  # #60
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(1, 40, 40, active=False)]}, enable_status="failed"
        )
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert any(
            a.status == "blocked" for a in res.actions if (a.dest_x, a.dest_y) == (40, 40)
        ), "a disabled route whose re-enable failed is blocked, not 'already active'"
        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}

    def test_failed_disable_defers_new_routes_for_that_origin(self):  # #61
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(9, 99, 98, active=True)]}, disable_status="failed"
        )
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}, (
            "a failed stale-disable must stop new routes on that origin"
        )
        assert any("disable" in p.lower() for p in res.problems)

    def test_captcha_resolution_halts_the_run(self):  # #62
        import itertools
        import time as _time

        svc = _FakeLiveSvc()
        svc.on_create = lambda: captcha_stop.signal(_USER.id)  # first create → stop
        # A strictly-increasing monotonic clock so the signal timestamp is
        # unambiguously after the run's start (real captcha resolution is seconds
        # later; on a coarse clock the same-tick case would be a test artifact).
        clock = itertools.count(1)
        try:
            with _patch(_time, "monotonic", lambda: next(clock)):
                res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        finally:
            captcha_stop.clear(_USER.id)
        assert len(svc.created) == 1, "no further creates after the captcha-stop signal"
        assert any(a.status == "deferred" for a in res.actions)

    def test_execution_registers_and_unregisters_the_active_op(self):  # #63
        seen = []
        svc = _FakeLiveSvc()
        svc.on_create = lambda: seen.append(list(active_ops.get_active(_USER.id)))
        _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert seen and all("trade-route-execute" in labels for labels in seen), (
            "a live run must be a registered active op so disconnect can't close its client"
        )
        assert "trade-route-execute" not in active_ops.get_active(_USER.id), (
            "and it must unregister when finished"
        )

    def test_exhausted_budget_blocks_all_execution(self):  # #64
        svc = _FakeLiveSvc(budget_ok=False)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert svc.created == [] and svc.listed == [], "no reads or writes once budget is spent"
        assert any("budget" in p.lower() for p in res.problems)

    def test_marketplace_read_failure_keeps_earlier_commits(self):  # #65
        # 20003 is visited first (shuffle disabled) and commits; 20011's read
        # raises — the endpoint returns a structured result, not a 500.
        svc = _FakeLiveSvc(read_raises={20011})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert res.dry_run is False
        assert (40, 40) in {(r.dest_x, r.dest_y) for r in svc.created}, "20003's route committed"
        assert any("marketplace read failed" in p for p in res.problems)

    def test_negative_origin_route_is_never_executed(self):  # #48 execution boundary
        acct = _account(
            [_row(20003, -1, 40, 40), _row(-5, -2, 50, 50)],  # second: impossible origin
            {20003: (0, 0), -5: (0, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", -5: "BAD", -1: "A", -2: "B"},
        )
        svc = _FakeLiveSvc()
        res = _run_live(svc, acct, max_routes_per_run=50)
        created_origins = {r.origin_village_id for r in svc.created}
        assert -5 not in created_origins, "a negative (foreign) origin must never be executed"
        assert 20003 in created_origins
        assert any("not a real account village" in w for w in res.warnings)

    def test_captcha_stop_halts_remaining_same_origin_routes(self):  # #62
        import itertools
        import time as _time

        svc = _FakeLiveSvc()  # two routes from ONE origin; first create signals stop
        svc.on_create = lambda: captcha_stop.signal(_USER.id)
        clock = itertools.count(1)
        try:
            with _patch(_time, "monotonic", lambda: next(clock)):
                res = _run_live(svc, _same_origin_account(), max_routes_per_run=50)
        finally:
            captcha_stop.clear(_USER.id)
        assert len(svc.created) == 1, "the second same-origin route must not be created"
        assert any(a.status == "deferred" for a in res.actions)

    def test_budget_exhaustion_halts_remaining_same_origin_routes(self):  # #64
        svc = _FakeLiveSvc()  # two routes from ONE origin

        def _exhaust():
            svc.budget_ok = False  # budget runs out right after the first create

        svc.on_create = _exhaust
        res = _run_live(svc, _same_origin_account(), max_routes_per_run=50)
        assert len(svc.created) == 1, "the second same-origin route must not be created"
        assert any("budget" in p.lower() for p in res.problems)
        assert any(a.status == "deferred" for a in res.actions)

    def test_read_failure_defers_every_later_origin_not_just_the_failing_one(self):  # #65
        # Three origins in order [20003, 20011, 20019]; the MIDDLE read fails.
        svc = _FakeLiveSvc(read_raises={20011})
        res = _run_live(svc, _three_origin_account(), max_routes_per_run=50)
        assert (40, 40) in {(r.dest_x, r.dest_y) for r in svc.created}, "20003 committed"
        assert 20019 not in svc.listed, "later origins are not read after a failure"
        deferred_dests = {(a.dest_x, a.dest_y) for a in res.actions if a.status == "deferred"}
        assert (50, 50) in deferred_dests, "the failing origin's route is deferred"
        assert (60, 60) in deferred_dests, "the unvisited later origin is deferred, not lost"
        assert res.remaining >= 2

    def test_captcha_during_read_blocks_disable_and_create(self):  # #62 round 3
        import itertools
        import time as _time

        # One stale ACTIVE route to disable + missing desired routes to create;
        # the captcha is signalled DURING the marketplace read, so NEITHER the
        # disable nor the create mutation may fire.
        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(9, 99, 99, active=True)]})
        svc.on_read = lambda: captcha_stop.signal(_USER.id)
        clock = itertools.count(1)
        try:
            with _patch(_time, "monotonic", lambda: next(clock)):
                res = _run_live(svc, _same_origin_account(), max_routes_per_run=50)
        finally:
            captcha_stop.clear(_USER.id)
        assert svc.disabled == [], "no disable after a captcha signalled during the read"
        assert svc.created == [], "no create after a captcha signalled during the read"
        assert any(a.status == "deferred" for a in res.actions)

    def test_budget_exhausted_during_read_blocks_disable_and_create(self):  # #64 round 3
        # Budget runs out DURING the read; the disable and create that follow in
        # the same origin must not fire.
        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(9, 99, 99, active=True)]})

        def _exhaust():
            svc.budget_ok = False

        svc.on_read = _exhaust
        res = _run_live(svc, _same_origin_account(), max_routes_per_run=50)
        assert svc.disabled == [], "no disable after budget exhausted during the read"
        assert svc.created == [], "no create after budget exhausted during the read"
        assert any("budget" in p.lower() for p in res.problems)

    def test_stop_during_create_pacing_wait_prevents_the_post(self):  # #62/#64 post-pacing
        # The endpoint's pre-create check passes, then a captcha is signalled
        # DURING create_route's pacing wait; the service's post-pacing stop_check
        # must catch it and skip the POST (no route created).
        import itertools
        import time as _time

        svc = _FakeLiveSvc()
        svc.on_pacing = lambda: captcha_stop.signal(_USER.id)
        clock = itertools.count(1)
        try:
            with _patch(_time, "monotonic", lambda: next(clock)):
                res = _run_live(svc, _same_origin_account(), max_routes_per_run=50)
        finally:
            captcha_stop.clear(_USER.id)
        assert svc.created == [], "a stop during the pacing wait must prevent the POST"
        assert any(a.status == "deferred" for a in res.actions)
