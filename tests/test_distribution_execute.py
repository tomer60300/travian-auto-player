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
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.parsers.html_parser import parse_trade_routes
from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.optimizer import OverBudget, Plan
from travian_api.services.distribution.planner import DistributionPlan, SheetRow
from travian_api.services.trade_route_service import (
    ExistingRoute,
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)
from travian_api.web.operation_gate import active_ops, captcha_stop
from travian_api.web.routes import distribution as dist_module
from travian_api.web.routes.distribution import (
    _CONSECUTIVE_FAILURE_LIMIT,
    ExecuteRequest,
    ForeignTarget,
    post_execute,
)


def _exec_body(dry_run=True, disable_existing=True, max_routes_per_run=3, margin=0.0, **extra):
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
            # Anything else the caller wants to set (only_origins, ...).
            **extra,
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
        #
        # A real DistributionPlan, not a stand-in with an is_feasible attribute:
        # the refusal now explains itself from the plan's own over_budget rows, so
        # a double that only carries the boolean would pass while telling the
        # operator nothing.
        async def _fake_plan(_body):
            plan = DistributionPlan(
                routing=Plan(over_budget=(OverBudget(village_id=20003, committed=9, available=4),))
            )
            return SimpleNamespace(
                plan=plan, names={20003: "Capital"}, coords={}, warnings=[], dropped_allocations=[]
            )

        with (
            _patch(dist_module, "_plan_account", _fake_plan),
            pytest.raises(HTTPException) as exc,
        ):
            _execute(_exec_body(dry_run=False), svc=_dry_svc(live_enabled=True))
        assert exc.value.status_code == 422
        detail = exc.value.detail
        assert "not executable" in detail.lower()
        # And it says WHICH village and by how much, rather than pasting every
        # warning the plan produced.
        assert "Capital" in detail
        assert "9" in detail and "4" in detail

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


# A real village id no plan row targets, and no plan coordinate resolves to.
_UNWANTED_DEST = 909090

_MINUTES_PER_DAY = 1440
# Midnight of a fixed day, so a fabricated departure carries no clock: `% 86400`
# on these values yields exactly the minute of the day that was asked for.
_EPOCH_DAY = 1787616000
# The id a marketplace page would report for a FOREIGN destination: real, and
# absent from the plan, which only ever knows that target by its coordinates.
_FOREIGN_REAL_ID = 555001


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
        phantom_creates=False,
        confirm_raises=None,
        rows_per_create=None,
    ):
        # phantom_creates models the failure a 200 with an empty body cannot
        # rule out: the game accepts the create and produces no route.
        self._phantom = phantom_creates
        # Rows one create makes appear. Default: the game's own 24/N fan-out.
        # An int here models a game that accepted the request and produced a
        # different number of rows than "repeat every N hours" implies -- the
        # case where reporting the forecast as the outcome is a false claim.
        self._rows_per_create = rows_per_create
        self._confirm_raises = set(confirm_raises or ())
        self._next_route_id = 900000
        self.updated: list[tuple] = []
        self.live_enabled = True
        # This fake DOES model the marketplace it reads, so it can legitimately
        # claim the route list is readable. The real service defaults to False
        # until the gid=17&t=3 markup is captured.
        self.reconciler_verified = True
        self._existing = existing or {}
        self._create_status = create_status
        self._disable_status = disable_status
        self._enable_status = enable_status
        self._read_raises = read_raises or set()  # origin ids whose read raises
        self.created = []  # PlannedRoute objects a create was ATTEMPTED for
        self.disabled = []  # (origin, sorted tuple of disabled dest coords)
        self.deleted = []  # (origin, sorted tuple of route ids removed for good)
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

    async def list_existing_routes(self, vid, *, map_span=None):
        self.listed.append(vid)
        if self.on_read is not None:
            self.on_read()  # e.g. signal captcha / exhaust budget DURING the read
        if vid in self._read_raises:
            from travian_api.exceptions import NetworkError

            raise NetworkError("marketplace read failed (test)")
        return list(self._existing.get(vid, []))

    async def update_cargo(self, vid, routes, cargo, *, dest_x=None, dest_y=None, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(vid, 0, 0, "stopped", reason)
        self.updated.append((vid, tuple(sorted(r.route_id for r in routes)), dict(cargo)))
        # The real game rewrites the rows, which is what a later read would show.
        targets = {r.route_id for r in routes}
        for row in self._existing.get(vid, []):
            if row.route_id in targets:
                row.cargo = dict(cargo)
        return RouteActionResult(vid, 0, 0, "updated", f"{len(routes)} route(s)")

    async def delete_routes(self, vid, routes, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(vid, 0, 0, "stopped", reason)
        ids = {r.route_id for r in routes}
        self.deleted.append((vid, tuple(sorted(ids))))
        # Actually gone. The read-back is what the production code trusts, so a
        # double that recorded the call and left the rows in place would let a
        # broken delete pass.
        self._existing[vid] = [e for e in self._existing.get(vid, []) if e.route_id not in ids]
        return RouteActionResult(vid, 0, 0, "deleted")

    async def confirm_routes(self, vid, *, map_span=None):
        if vid in self._confirm_raises:
            from travian_api.exceptions import NetworkError

            raise NetworkError("read-back failed (test)")
        return list(self._existing.get(vid, []))

    async def disable_routes(self, vid, routes, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(vid, 0, 0, "stopped", reason)
        self.disabled.append((vid, tuple(sorted((r.dest_x, r.dest_y) for r in routes))))
        if self._disable_status == "disabled":
            # The real game switches the rows off, which is the only evidence
            # the PUT's response does not provide.
            targets = {r.route_id for r in routes}
            for row in self._existing.get(vid, []):
                if row.route_id in targets:
                    row.active = False
        return RouteActionResult(vid, 0, 0, self._disable_status, f"{len(routes)} route(s)")

    async def enable_routes(self, vid, routes, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if not routes:
            return None
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(vid, 0, 0, "stopped", reason)
        self.enabled.append((vid, tuple(sorted((r.dest_x, r.dest_y) for r in routes))))
        if self._enable_status == "enabled":
            targets = {r.route_id for r in routes}
            for row in self._existing.get(vid, []):
                if row.route_id in targets:
                    row.active = True
        return RouteActionResult(vid, 0, 0, self._enable_status, f"{len(routes)} route(s)")

    def rows_a_create_makes(self, route):
        """How many rows this double lets one create produce.

        The game's answer is 24/N -- "repeat every 6 hours" is four separate
        daily rows, not one. A double that produced a single row could not tell
        a measured 4 apart from a forecast 4, which is the whole question here.
        """
        if self._rows_per_create is not None:
            return self._rows_per_create
        if route.cycle_hours <= 0:
            return 1
        return max(1, -(-24 // route.cycle_hours))

    async def create_route(self, route, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if self.on_pacing is not None:
            self.on_pacing()  # something happens during the (simulated) pacing wait
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "stopped", reason
            )
        self.created.append(route)
        if not self._phantom:
            # The real game makes the route appear on the marketplace, which is
            # the only evidence the empty response does not give -- and it makes
            # one row per daily departure, so a 6-hour cycle appears as four.
            for offset in range(self.rows_a_create_makes(route)):
                self._next_route_id += 1
                # Each row departs `offset * cycle` after the requested time, and
                # `departure_at % 86400` is that minute of the day -- measured
                # against the real game, which returned 1410 for a 23:30 request.
                # A row with no departure would read as "time unknown", which is
                # never pruned, so omitting this would silently disable the whole
                # window question.
                minute = (
                    route.dispatch_minute + offset * max(1, route.cycle_hours) * 60
                ) % _MINUTES_PER_DAY
                self._existing.setdefault(route.origin_village_id, []).append(
                    ExistingRoute(
                        route_id=self._next_route_id,
                        dest_village_id=route.dest_village_id,
                        dest_x=route.dest_x,
                        dest_y=route.dest_y,
                        active=True,
                        departure_at=_EPOCH_DAY + minute * 60,
                        # The real page shows each row's cargo, and the pooled
                        # trim breaks same-minute ties with it.
                        cargo=dict(route.cargo),
                    )
                )
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
        # Provisioned by an actual run: the double's create_route fans each
        # route into its daily rows the way the game does, which is the only
        # marketplace state a faithful "everything exists" fixture can be.
        svc = _FakeLiveSvc()
        self._run(svc, disable_existing=False, max_routes_per_run=50)
        svc.created.clear()
        svc.listed.clear()
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
        # The double's create_route already put run 1's rows on the fake
        # marketplace, fanned and time-stamped like the game's.
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
        # Provision the wanted routes by running, then plant one stale row.
        svc = _FakeLiveSvc()
        self._run(svc, disable_existing=False, max_routes_per_run=50)
        svc._existing.setdefault(a.origin, []).append(ExistingRoute(999, _UNWANTED_DEST, 99, 98))
        svc.created.clear()
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
                # honeypot at a plan dest
                ExistingRoute(1, a.destination, a.dest_x, a.dest_y, visible=False),
                # honeypot at a non-plan dest
                ExistingRoute(2, _UNWANTED_DEST, 97, 96, visible=False),
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
            dropped_allocations=[],
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
        svc = _FakeLiveSvc()
        self._run(svc, disable_existing=False, max_routes_per_run=50)
        svc.created.clear()
        svc.listed.clear()
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
        existing = {a.origin: [ExistingRoute(1, _UNWANTED_DEST, 99, 98)]}  # stale
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
        route = PlannedRoute(20003, -1, 40, 40, "Ally-Keep", {Resource.CROP: 500}, 3, 2)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(svc.create_route(route))

    def test_disable_with_nothing_makes_no_request_and_no_guard(self):
        svc = self._svc(live_enabled=False)
        # Empty list returns None before the live guard — nothing to disable.
        assert asyncio.run(svc.disable_routes(20003, [])) is None

    def test_disable_with_routes_refuses_until_verified(self):
        svc = self._svc(live_enabled=False)
        with pytest.raises(TradeRoutePayloadUnverified):
            asyncio.run(svc.disable_routes(20003, [ExistingRoute(1, _UNWANTED_DEST, 40, 40)]))


class TestTradeRouteParser:
    """These pinned a DOM scraper that, measured against a real marketplace
    page, found ZERO routes: the attribute is `data-trade-route-id`, not
    `data-route-id`, and coordinates appear nowhere on the page. The properties
    worth keeping are re-pinned here against the model the page actually
    carries; the full real-page coverage lives in
    tests/test_marketplace_route_parsing.py.
    """

    @staticmethod
    def _page(routes: list[dict], dest_id: int = 20010) -> str:
        """A marketplace page carrying the model React is handed."""
        view = {
            "ownPlayer": {
                "id": 1,
                "currentVillageId": 20002,
                "village": {
                    "marketplace": {
                        "tradeRoutes": [
                            {
                                "objectId": f"20002_{dest_id}",
                                "from": {"id": 20002, "name": "02"},
                                "to": {
                                    "id": dest_id,
                                    "mapId": 50001,
                                    "name": "V01",
                                    "player": {"id": 1},
                                },
                                "routes": routes,
                            }
                        ]
                    }
                },
            }
        }
        return (
            "<script>window.Travian.React.TradeRoutes.render("
            f"{{viewData: {json.dumps(view)}}})</script>"
        )

    @staticmethod
    def _route(route_id: int | None = None, enabled: bool = True) -> dict:
        route = {
            "enabled": enabled,
            "sendOnce": False,
            "carriedResources": {"lumber": 0, "clay": 0, "iron": 0, "crop": 100},
            "departureAt": 1700000000,
            "arrivalAt": 1700003600,
            "repeat": 1,
            "merchants": 1,
            "ships": 0,
            "useTradeShips": False,
        }
        if route_id is not None:
            route["id"] = route_id
        return route

    def test_unknown_markup_yields_nothing(self):
        # Safe default for disabling: no recognisable routes -> nothing to
        # disable. NOT safe for creating, which is why creation is gated.
        assert parse_trade_routes("<html><body>marketplace</body></html>") == []

    def test_the_old_dom_markup_is_not_mistaken_for_routes(self):
        # The shape the previous parser looked for. It never existed on a real
        # page, and must not be resurrected as a fallback that invents routes.
        assert parse_trade_routes('<div data-route-id="7" data-x="1" data-y="2"></div>') == []

    def test_disabled_route_is_flagged_inactive(self):
        # Read from the model's own `enabled`, which is what decides whether a
        # route the plan still wants is re-enabled rather than created again.
        page = self._page([self._route(1), self._route(2, enabled=False)])
        routes = {r["route_id"]: r for r in parse_trade_routes(page)}
        assert routes[1]["active"] is True
        assert routes[2]["active"] is False

    def test_every_route_is_visible(self):
        # The real model has no hidden-entry mechanism, so the honeypot concern
        # the DOM scraper carried does not arise here. It was a property of
        # assumed markup, not of the page.
        page = self._page([self._route(1)])
        # Pins the CONTRACT, not a computation: every route the parser reports
        # is one a human can see. It cannot fail while the model has no hidden
        # rows, which is the point being recorded.
        assert all(r["visible"] is True for r in parse_trade_routes(page))

    def test_a_route_without_an_id_is_skipped_not_guessed(self):
        page = self._page([self._route(route_id=None)])
        assert parse_trade_routes(page) == []


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
        dropped_allocations=[],
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


def _fanned(
    dest_village_id,
    x=None,
    y=None,
    *,
    cycle_hours=6,
    dispatch_minute=100,
    start_id=1,
    active=True,
    cargo=None,
):
    """The rows the game holds for one created route: one per daily departure,
    each a cycle after the last, `departure_at % 86400` being that minute
    (measured live: 1410 for a 23:30 request). What a faithful "this already
    exists" fixture seeds, now that reconciliation matches the row set rather
    than merely the destination -- a single bare row is not an existing route,
    it is a schedule the plan does not recognise."""
    rows = []
    for i in range(max(1, 24 // cycle_hours)):
        minute = (dispatch_minute + i * cycle_hours * 60) % _MINUTES_PER_DAY
        rows.append(
            ExistingRoute(
                start_id + i,
                dest_village_id,
                x,
                y,
                active=active,
                cargo=dict(cargo) if cargo else None,
                departure_at=_EPOCH_DAY + minute * 60,
            )
        )
    return rows


def _account(rows, coords, names):
    plan = SimpleNamespace(is_feasible=True, warnings=(), rows=tuple(rows))
    return SimpleNamespace(
        plan=plan, names=names, coords=coords, warnings=[], dropped_allocations=[]
    )


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


def _own_village_account():
    """A plan whose destination is one of the account's OWN villages, so the
    reconciler can match it by village id rather than by coordinates."""
    rows = (
        SheetRow(
            origin=20003,
            destination=20011,
            cargo={Resource.CROP: 100},
            cycle_hours=6,
            dispatch_minute=100,
            arrival_minute=0,
            merchants=2,
        ),
    )
    plan = SimpleNamespace(is_feasible=True, warnings=(), rows=rows)
    return SimpleNamespace(
        plan=plan,
        names={20003: "03", 20011: "11"},
        coords={20003: (0, 0), 20011: (10, 0)},
        warnings=[],
        dropped_allocations=[],
    )


class TestOwnVillageRoutesSurviveABadWorldSpan:
    """The marketplace page carries no coordinates -- only a village id and a map
    id -- so any coordinate we hold for a live route is back-derived through the
    world's span. On a world that is not 401 wide (or for a map id that span
    cannot place) that derivation is wrong or absent, and a reconciler keyed on
    coordinates then matches nothing: it disables every live route as stale and
    creates the entire plan on top, every single run. That is the daily
    rebuild-the-same-routes signature the whole reconciler exists to avoid."""

    def test_a_wrong_span_does_not_churn_a_route_the_plan_still_wants(self):
        account = _own_village_account()
        # The route that is really there, as the page reports it: the right
        # village, with coordinates that came out wrong for this world.
        svc = _FakeLiveSvc(existing={20003: _fanned(20011, -137, 42)})
        res = _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert svc.disabled == [], "a route the plan still wants must never be disabled"
        assert svc.created == [], "and must never be recreated on top of itself"
        assert [a.status for a in res.actions] == ["skipped"]

    def test_an_unplaceable_map_id_does_not_churn_it_either(self):
        # dest_x/dest_y are None when the map id could not be placed at all.
        account = _own_village_account()
        svc = _FakeLiveSvc(existing={20003: _fanned(20011)})
        res = _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert svc.disabled == [], "an unplaceable route the plan wants is not stale"
        assert svc.created == []
        assert [a.status for a in res.actions] == ["skipped"]

    def test_a_route_to_a_village_the_plan_dropped_is_still_disabled(self):
        # The other half: village-id matching must not make everything look
        # wanted, or disable_existing would stop working entirely.
        account = _own_village_account()
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(1, _UNWANTED_DEST, dest_x=None, dest_y=None)]}
        )
        _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert [vid for vid, _ in svc.disabled] == [20003], "a dropped destination is stale"


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
        route = PlannedRoute(
            20003, -1, 40, 40, "A", {Resource.CROP: 100}, 6, 2, dispatch_minute=615
        )
        payload = TradeRouteService(http_client=SimpleNamespace())._build_create_payload(route)
        assert payload["hour"] == 10
        assert payload["minute"] == 15

    def test_disabled_desired_route_is_re_enabled_not_duplicated(self):  # #60
        svc = _FakeLiveSvc(existing={20003: _fanned(_FOREIGN_REAL_ID, 40, 40, active=False)})
        _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert any(vid == 20003 and (40, 40) in coords for vid, coords in svc.enabled), (
            "a disabled desired route must be re-enabled"
        )
        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}, "no duplicate create"

    def test_a_disabled_route_does_not_count_as_active(self):  # #60
        svc = _FakeLiveSvc(
            existing={20003: _fanned(_FOREIGN_REAL_ID, 40, 40, active=False)},
            enable_status="failed",
        )
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert any(
            a.status == "blocked" for a in res.actions if (a.dest_x, a.dest_y) == (40, 40)
        ), "a disabled route whose re-enable failed is blocked, not 'already active'"
        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}

    def test_failed_disable_defers_new_routes_for_that_origin(self):  # #61
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(9, _UNWANTED_DEST, 99, 98, active=True)]},
            disable_status="failed",
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
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(9, _UNWANTED_DEST, 99, 99, active=True)]}
        )
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
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(9, _UNWANTED_DEST, 99, 99, active=True)]}
        )

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


class TestALiveRunLeavesAnAuditableTrace:
    """End-to-end: the endpoint must actually emit the trace, not just be able to.

    A tracer with no call sites is worse than none -- it reads as observability
    that exists. These run the real endpoint against the fake game and read the
    trace file back.
    """

    def _trace_events(self, res):
        import json
        from pathlib import Path

        assert res.trace_path, "a live run must report where its trace went"
        return [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

    def test_the_run_is_bracketed_by_a_start_and_an_end(self):
        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        events = self._trace_events(res)

        assert events[0]["kind"] == "run_start"
        assert events[0]["live_enabled"] is True
        assert events[-1]["kind"] == "run_end"
        assert events[-1]["created"] == 2

    def test_every_marketplace_read_is_recorded_with_what_it_found(self):
        existing = {20003: [ExistingRoute(1, _UNWANTED_DEST, 99, 98, active=True)]}
        svc = _FakeLiveSvc(existing=existing)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        reads = [e for e in self._trace_events(res) if e["kind"] == "origin_read"]
        assert {r["origin"] for r in reads} == {20003, 20011}
        read = next(r for r in reads if r["origin"] == 20003)
        assert read["existing"] == 1
        assert read["destinations"] == [_UNWANTED_DEST]

    def test_a_skip_records_why_and_which_key_matched(self):
        # The whole point of the village-id reconciliation fix: a run must be
        # able to say whether it matched on the id or fell back to coordinates.
        account = _own_village_account()
        svc = _FakeLiveSvc(existing={20003: _fanned(20011, 10, 0)})
        res = _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        decisions = [e for e in self._trace_events(res) if e["kind"] == "decision"]
        assert len(decisions) == 1
        assert decisions[0]["decision"] == "skipped"
        assert decisions[0]["matched_by"] == "village_id"
        assert decisions[0]["reason"]

    def test_a_disable_records_the_route_ids_and_what_the_plan_wanted(self):
        # So a wrong disable can be reconstructed afterwards: these two fields
        # together are the entire argument for calling a route stale.
        account = _own_village_account()
        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(7, _UNWANTED_DEST, None, None)]})
        res = _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        stale = [e for e in self._trace_events(res) if e["kind"] == "stale_classified"]
        assert stale[0]["stale_route_ids"] == [7]
        assert stale[0]["wanted_village_ids"] == [20011]

    def test_a_whole_origin_deferred_by_the_cap_says_so(self):
        # With a cap of 1 the second origin is deferred at the top of the loop,
        # WITHOUT its marketplace being read -- so the trace has to record the
        # deferral itself, or that origin's routes would vanish from the record.
        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=1)
        events = self._trace_events(res)

        deferred = [e for e in events if e["kind"] == "origin_deferred"]
        assert deferred, "a run that deferred a whole origin must say so"
        assert "cap" in deferred[0]["reason"]
        assert deferred[0]["routes"] == 1
        read_origins = {e["origin"] for e in events if e["kind"] == "origin_read"}
        assert deferred[0]["origin"] not in read_origins, (
            "the deferred origin was correctly never read"
        )

    def test_an_unreadable_marketplace_is_recorded_as_unknown_not_empty(self):
        # The most dangerous state in the whole path: not knowing what a village
        # already has. It must be unmistakable in the trace.
        svc = _FakeLiveSvc(read_raises={20003})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        events = self._trace_events(res)

        failures = [e for e in events if e["kind"] == "origin_read_failed"]
        assert failures, "a failed read must be traced, not just counted"
        assert failures[0]["origin"] == 20003
        assert failures[0]["error_type"]
        assert not [e for e in events if e["kind"] == "wrote" and e["origin"] == 20003], (
            "nothing may be written to a village whose state could not be read"
        )


class TestTheGameRowFanOutIsReported:
    """One create request is not one route in the game.

    Travian implements "repeat every N hours" by generating 24/N separate daily
    rows. Measured on a real marketplace page: every destination's row count was
    exactly 24 divided by its departure spacing (24@1h, 12@2h, 8@3h, 2@12h).

    So an operator who caps a run at 3 routes and plans 1-hour cycles has
    authorised 72 rows. Reporting only "3 created" would be true and misleading
    at the same time, which is the worst kind of number to print.
    """

    def test_the_row_count_is_twenty_four_over_the_cycle(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        for action in res.actions:
            if action.cycle_hours > 0:
                assert action.game_rows == 24 // action.cycle_hours, (
                    f"a {action.cycle_hours}h cycle becomes {24 // action.cycle_hours} rows"
                )

    def test_a_dry_run_totals_the_rows_it_would_create(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        expected = sum(a.game_rows for a in res.actions if a.status == "would_create")
        assert res.created_game_rows == expected
        assert res.created_game_rows >= len(
            [a for a in res.actions if a.status == "would_create"]
        ), "rows can never be fewer than requests"

    def test_a_live_run_reports_the_rows_it_actually_created(self):
        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        created = [a for a in res.actions if a.status == "created"]
        assert created, "the fixture is meant to create routes"
        # _two_origin_account plans 6-hour cycles: 24/6 = 4 rows each.
        assert all(a.game_rows == 4 for a in created)
        # And the four are what the read-back COUNTED, not what the cycle
        # implies. Without observed_game_rows this assertion held for a game
        # that produced no rows at all.
        assert all(a.observed_game_rows == 4 for a in created)
        assert res.created_game_rows == 4 * len(created)

    def test_the_authorised_row_footprint_is_stated_at_run_start(self):
        import json
        from pathlib import Path

        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        start = events[0]
        assert start["kind"] == "run_start"
        assert start["max_game_rows_this_run"] == 8, (
            "two 6-hour routes authorise 2 x 4 = 8 rows, and the trace must say so "
            "BEFORE anything is written"
        )
        assert events[-1]["created_game_rows"] == 8


class _UnevenFanOut(_FakeLiveSvc):
    """A game that fans one create out by a different amount per destination.

    A symmetric fixture cannot catch an attribution bug: if both destinations
    make four rows, handing each action the origin's flat total of eight is
    wrong in a way that a "== 4" assertion still fails on, but handing each the
    other's rows as well would look identical to correct for any fixture where
    the two happen to agree. Different counts pin which rows went where.
    """

    def __init__(self, rows_by_dest, **kw):
        super().__init__(**kw)
        self._rows_by_dest = rows_by_dest

    def rows_a_create_makes(self, route):
        return self._rows_by_dest[(route.dest_x, route.dest_y)]


class TestTheRowCountIsMeasuredNotPredicted:
    """`created_game_rows` on a LIVE run must be evidence, not arithmetic.

    24/cycle is a model of the game, and the model is what the run set out to
    check. Printing "Put 24 route row(s) in the game" because the cycle was one
    hour states a fact about the account that nobody looked at -- and the run
    already looked: the reconciler re-reads every origin it wrote to, precisely
    because an empty 200 proves nothing. The rows it found are right there.
    """

    def test_the_measured_count_replaces_the_arithmetic_one(self):
        # The game accepts both creates and makes ONE row each, where the
        # 6-hour cycle predicts four.
        svc = _FakeLiveSvc(rows_per_create=1)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        created = [a for a in res.actions if a.status == "created"]
        assert len(created) == 2, "both routes exist -- they are just smaller than planned"
        assert all(a.game_rows == 4 for a in created), "the forecast is still reported as such"
        assert all(a.observed_game_rows == 1 for a in created)
        assert res.created_game_rows == 2, (
            "two rows is what the marketplace showed; 8 is what the cycle implied"
        )

    def test_the_trace_summary_records_the_measurement_too(self):
        import json
        from pathlib import Path

        svc = _FakeLiveSvc(rows_per_create=1)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        assert events[0]["max_game_rows_this_run"] == 8, "the forecast, before anything was written"
        assert events[-1]["created_game_rows"] == 2, "the measurement, after"
        verified = [e for e in events if e["kind"] == "verified"]
        assert verified, "the read-back is what produced the measurement"
        assert verified[0]["rows_forecast"] == 4
        assert verified[0]["new_rows_found"] == 1

    def test_a_shortfall_against_the_forecast_is_reported_as_a_problem(self):
        # A route that made 1 row where 4 were expected ships a quarter of what
        # the plan believes it ships. Nothing else in the response says so: the
        # status is "created" and the count is honest but silent about the rate.
        svc = _FakeLiveSvc(rows_per_create=1)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert res.problems, "a quarter-rate route is a finding, not a rounding difference"
        joined = " ".join(res.problems)
        assert "the game made 1 route row(s), not the 4" in joined
        assert "does not ship at the rate the plan assumes" in joined

    def test_a_matching_count_is_not_a_problem(self):
        # The check must be silent when the model holds, or it is noise.
        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["created", "created"]
        assert res.problems == []

    def test_rows_are_attributed_to_the_destination_that_made_them(self):
        # Both creates leave the SAME origin, so the origin's fresh rows are a
        # pooled six. Each action must get its own share, matched by the same
        # key the reconciler recognises routes by.
        svc = _UnevenFanOut({(40, 40): 4, (50, 50): 2})
        res = _run_live(svc, _same_origin_account(), max_routes_per_run=50)

        by_dest = {(a.dest_x, a.dest_y): a for a in res.actions if a.status == "created"}
        assert len(by_dest) == 2, "one create each, from one origin"
        assert by_dest[(40, 40)].observed_game_rows == 4
        assert by_dest[(50, 50)].observed_game_rows == 2, (
            "not 6 -- the other destination's rows are not this route's rows"
        )
        assert res.created_game_rows == 6

        # And only the destination that fell short is flagged.
        joined = " ".join(res.problems)
        assert "the game made 2 route row(s), not the 4" in joined
        assert "the game made 4 route row(s)" not in joined

    def test_a_dry_run_measures_nothing_and_never_pretends_to(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)

        assert all(a.observed_game_rows is None for a in res.actions), (
            "a preview issues zero game requests, so there is nothing it could have observed"
        )
        assert res.created_game_rows == sum(
            a.game_rows for a in res.actions if a.status == "would_create"
        ), "the forecast stays the forecast on the path that has nothing else to offer"

    def test_a_phantom_create_measures_zero_rather_than_nothing(self):
        # Zero rows found is a MEASUREMENT, and a different answer from "the
        # read-back failed so there is no measurement".
        svc = _FakeLiveSvc(phantom_creates=True)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["not_created", "not_created"]
        assert all(a.observed_game_rows == 0 for a in res.actions)
        assert res.created_game_rows == 0

    def test_an_unconfirmed_create_has_no_row_count_at_all(self):
        # The rows are almost certainly there; this run simply did not see them.
        # Substituting the forecast would be the original defect with extra
        # steps, so the honest answer is "unmeasured".
        svc = _FakeLiveSvc(confirm_raises={20003})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        by_origin = {a.origin: a for a in res.actions}
        assert by_origin[20003].status == "created_unverified"
        assert by_origin[20003].observed_game_rows is None
        assert by_origin[20011].observed_game_rows == 4
        assert res.created_game_rows == 4, "only the village that was re-read contributes rows"


class TestTheHeadlineCannotContradictTheProblems:
    """`created` counts VERIFIED creates only, which is correct and incomplete.

    A run whose every read-back failed reported `created=0` while `problems`
    said three routes had just been written and could not be confirmed. Both
    statements were true; together they were a summary refuting its own detail,
    and resolving that was left to the operator reading prose under a headline
    number that said nothing happened.
    """

    def test_a_run_where_every_read_back_failed_does_not_report_zero_alone(self):
        svc = _FakeLiveSvc(confirm_raises={20003, 20011})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert res.created == 0, "nothing was CONFIRMED, and that stays true"
        assert res.created_unverified == 2, (
            "but two routes were written, and the headline has to be able to say so"
        )
        assert res.not_created == 0
        assert res.problems, "and the detail still explains why they are unconfirmed"

    def test_creates_the_game_swallowed_are_counted_on_the_response(self):
        svc = _FakeLiveSvc(phantom_creates=True)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert res.created == 0
        assert res.created_unverified == 0, "these were checked -- and found absent"
        assert res.not_created == 2

    def test_a_clean_run_reports_neither(self):
        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert res.created == 2
        assert res.created_unverified == 0
        assert res.not_created == 0

    def test_the_three_counts_agree_with_the_action_list(self):
        # The response summary and the per-route detail are the same run; a
        # summary derived from anything but the actions could drift from them.
        svc = _FakeLiveSvc(confirm_raises={20003})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert res.created == sum(1 for a in res.actions if a.status == "created")
        assert res.created_unverified == sum(
            1 for a in res.actions if a.status == "created_unverified"
        )
        assert res.not_created == sum(1 for a in res.actions if a.status == "not_created")

    def test_a_dry_run_claims_neither(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        assert res.created_unverified == 0
        assert res.not_created == 0


class _ExplodingSvc(_FakeLiveSvc):
    """A game that fails in a way nobody anticipated, part-way through."""

    def __init__(self, blow_on: int = 2, **kw):
        super().__init__(**kw)
        self._blow_on = blow_on
        self._calls = 0

    async def create_route(self, route, *, stop_check=None):
        self._calls += 1
        if self._calls == self._blow_on:
            raise RuntimeError("the game returned something nobody expected")
        return await super().create_route(route, stop_check=stop_check)


class TestAnUnexpectedFailureStillAccountsForWhatItWrote:
    """The failure mode that matters most: writes commit, then something breaks.

    Issue #65 handled the read failures we anticipated. Anything else propagated
    as a bare 500, so a run that had already created routes in a real account
    told the operator nothing whatsoever about them -- the worst possible
    combination of a real side effect and no record of it.
    """

    def test_it_still_fails_rather_than_pretending_the_run_succeeded(self):
        svc = _ExplodingSvc(blow_on=2)
        with pytest.raises(HTTPException) as caught:
            _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert caught.value.status_code == 500

    def test_the_error_says_how_many_routes_it_had_already_created(self):
        svc = _ExplodingSvc(blow_on=2)
        with pytest.raises(HTTPException) as caught:
            _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        detail = str(caught.value.detail)
        assert len(svc.created) == 1, "the fixture is meant to commit one write first"
        assert "creating 1 route(s)" in detail
        assert "Nothing further was attempted" in detail

    def test_the_error_points_at_the_trace_and_the_way_to_undo_it(self):
        svc = _ExplodingSvc(blow_on=2)
        with pytest.raises(HTTPException) as caught:
            _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        detail = str(caught.value.detail)
        assert "trace" in detail
        assert "revert-plan" in detail, "an operator mid-incident needs the next step"

    def test_the_trace_records_the_committed_write_and_the_failure(self):
        import json
        from pathlib import Path

        from travian_api.services.distribution import execution_trace

        svc = _ExplodingSvc(blow_on=2)
        with pytest.raises(HTTPException):
            _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        # The newest trace in the (test-isolated) directory is this run's.
        newest = max(
            Path(execution_trace.TRACE_DIR).glob("exec-*.jsonl"),
            key=lambda p: p.stat().st_mtime,
        )
        events = [json.loads(line) for line in newest.read_text(encoding="utf-8").splitlines()]
        kinds = [e["kind"] for e in events]

        assert "run_failed" in kinds, "the failure itself must be in the record"
        failure = next(e for e in events if e["kind"] == "run_failed")
        assert failure["error_type"] == "RuntimeError"
        assert failure["created_before_failure"] == 1
        assert events[-1]["kind"] == "run_end", "and the trace must still terminate"


class TestTheFanOutDoesNotCauseAReRun:
    """One create becomes 24/N rows in the game. The next run must skip them all.

    This is the interaction most likely to cause silent duplication: the run
    creates ONE route, the game turns it into 24, and a reconciler that expected
    to find one thing could conclude its route is missing and create it again --
    every run, forever.
    """

    def _fan_out(self, svc, cycle_hours: int):
        """Rewrite the fake marketplace the way the game would after a create:
        one row per daily departure, each stamped with its departure time."""
        rows = {}
        for created in svc.created:
            origin = created.origin_village_id
            for i in range(24 // cycle_hours):
                minute = (created.dispatch_minute + i * cycle_hours * 60) % _MINUTES_PER_DAY
                rows.setdefault(origin, []).append(
                    ExistingRoute(
                        route_id=700000 + len(rows.get(origin, [])) + i,
                        dest_village_id=created.dest_village_id,
                        dest_x=created.dest_x,
                        dest_y=created.dest_y,
                        active=True,
                        departure_at=_EPOCH_DAY + minute * 60,
                    )
                )
        return rows

    def test_a_second_run_creates_nothing_after_the_game_fans_out(self):
        svc = _FakeLiveSvc()
        _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert len(svc.created) == 2

        # _two_origin_account plans 6-hour cycles -> 4 rows each.
        svc._existing = self._fan_out(svc, cycle_hours=6)
        assert sum(len(v) for v in svc._existing.values()) == 8
        svc.created.clear()

        res = _run_live(svc, _two_origin_account(), disable_existing=True, max_routes_per_run=50)

        assert svc.created == [], "the fanned-out rows already satisfy the plan"
        assert svc.disabled == [], "and none of them is stale"
        assert all(a.status == "skipped" for a in res.actions)

    def test_disabling_a_dropped_destination_disables_every_row_of_it(self):
        # The other direction: dropping a destination must switch off all 24/N
        # of its rows, not just one, or the route keeps running at reduced rate.
        account = _own_village_account()
        rows = [
            ExistingRoute(route_id=700000 + i, dest_village_id=_UNWANTED_DEST, active=True)
            for i in range(24)
        ]
        svc = _FakeLiveSvc(existing={20003: rows})

        _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert len(svc.disabled) == 1, "one PUT carries them all, as the capture showed"
        _, coords = svc.disabled[0]
        assert len(coords) == 24, "every row of the dropped destination is disabled"

    def test_create_only_never_duplicates_a_mismatched_destination(self):
        # Found by a live probe trace (exec-d974a7098361): eight 3h rows to the
        # destination, a plan wanting 6h, and disable_existing=False. The
        # mismatch correctly kept the destination out of `satisfied` -- but
        # nothing may disable in create-only mode, so the create path built a
        # duplicate ON TOP of the eight live rows: created 1, disabled 0, and
        # the destination shipped both schedules at once. Create-only means the
        # only change is a route the plan is MISSING; a mismatched one is not
        # missing, it is wrong, and fixing wrong requires the disable the
        # operator withheld -- so the run must block and say that.
        account = _own_village_account()  # wants 6h from minute 100
        rows = _fanned(20011, cycle_hours=3, start_id=710000)  # 8 rows, 3h apart
        svc = _FakeLiveSvc(existing={20003: rows})

        res = _run_live(svc, account, disable_existing=False, max_routes_per_run=50)

        assert svc.created == [], "create-only built a duplicate route on top of eight live rows"
        assert svc.disabled == [], "create-only must never disable"
        assert [a.status for a in res.actions] == ["blocked"]
        assert "different schedule" in res.actions[0].detail
        assert "disable" in res.actions[0].detail, "the operator must be told which switch fixes it"

    def test_with_disable_the_same_mismatch_is_replaced_not_blocked(self):
        # The other half, pinned so the block above cannot leak into the mode
        # where fixing IS allowed: same eight 3h rows, disable ticked -- the old
        # schedule is switched off and the plan's route created.
        account = _own_village_account()
        rows = _fanned(20011, cycle_hours=3, start_id=710000)
        svc = _FakeLiveSvc(existing={20003: rows})

        res = _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert len(svc.created) == 1, "the plan's 6h route replaces the 3h schedule"
        assert svc.disabled, "the mismatched rows are switched off first"
        _, coords = svc.disabled[0]
        assert len(coords) == 8, "all eight off-schedule rows, not a subset"
        assert [a.status for a in res.actions] == ["created"]

    def test_a_partly_disabled_destination_is_restored_not_duplicated(self):
        # Half the rows off means half the cadence. The plan still wants this
        # destination, so the fix is to re-enable -- never to create more rows
        # on top of the ones already there.
        account = _own_village_account()
        rows = _fanned(20011, start_id=700000)
        for i, row in enumerate(rows):
            row.active = i % 2 == 0
        svc = _FakeLiveSvc(existing={20003: rows})

        _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert svc.created == [], "a partly-live destination must never be duplicated"
        assert svc.disabled == [], "and its live rows are not churned"
        assert svc.enabled, "the disabled half is switched back on"
        _, enabled_coords = svc.enabled[0]
        assert len(enabled_coords) == 2, "exactly the two that were off"


class TestAControlledRunCanTargetOnePair:
    """The first live run against a real account must be exactly one chosen route.

    Without this, "one route" meant "whichever route the cap happened to reach
    first", which is not a controlled test -- it is an uncontrolled one with a
    small blast radius. And a filtered run has to announce that it was filtered,
    because a partial run read as a complete one is how an operator stops
    checking the thing that still needs checking.
    """

    def test_only_origins_runs_just_that_village(self):
        res = _execute(
            _exec_body(dry_run=True, max_routes_per_run=50, only_origins=[20003]),
            connected=False,
        )
        assert res.actions, "the filter must not empty the run"
        assert {a.origin for a in res.actions} == {20003}

    def test_only_destinations_runs_just_that_target(self):
        full = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        target = full.actions[0].destination

        res = _execute(
            _exec_body(dry_run=True, max_routes_per_run=50, only_destinations=[target]),
            connected=False,
        )
        assert {a.destination for a in res.actions} == {target}
        assert len(res.actions) < len(full.actions), "the filter must actually narrow it"

    def test_both_filters_together_isolate_one_pair(self):
        full = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        first = full.actions[0]

        res = _execute(
            _exec_body(
                dry_run=True,
                max_routes_per_run=50,
                only_origins=[first.origin],
                only_destinations=[first.destination],
            ),
            connected=False,
        )
        assert len(res.actions) == 1
        assert res.actions[0].origin == first.origin
        assert res.actions[0].destination == first.destination

    def test_a_filtered_run_says_it_was_filtered_and_what_it_skipped(self):
        full = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        res = _execute(
            _exec_body(dry_run=True, max_routes_per_run=50, only_origins=[20003]),
            connected=False,
        )
        skipped = len(full.actions) - len(res.actions)

        assert res.filtered_to, "a narrowed run must not look like a complete one"
        assert "origins" in res.filtered_to
        assert f"{skipped} other planned route(s) were NOT considered" in res.filtered_to

    def test_an_unfiltered_run_reports_no_filter(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        assert res.filtered_to is None

    def test_a_filter_matching_nothing_creates_nothing_rather_than_everything(self):
        # Fail closed: a typo in a village id must produce an empty run, never a
        # full one. This is the difference between a no-op and a live account
        # getting the entire plan.
        res = _execute(
            _exec_body(dry_run=True, max_routes_per_run=50, only_origins=[99999999]),
            connected=False,
        )
        assert [a for a in res.actions if a.status == "would_create"] == []
        assert res.created_game_rows == 0

    def test_the_filter_is_recorded_in_the_trace(self):
        import json
        from pathlib import Path

        svc = _FakeLiveSvc()
        res = _run_live(
            svc,
            _two_origin_account(),
            max_routes_per_run=50,
            only_origins=[20003],
        )
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        assert events[0]["filtered_to"], "the trace must never read as a full run"
        assert events[0]["planned_routes_excluded_by_filter"] == 1
        assert {e["origin"] for e in events if e["kind"] == "origin_read"} == {20003}
        assert len(svc.created) == 1, "only the targeted village was written to"


class TestTheWriteEndpointRejectsWhatItDoesNotUnderstand:
    """A discarded safety filter is worse than a rejected request.

    A browser holding a newer bundle sent only_origins/only_destinations to a
    backend that predated them. Pydantic ignores unknown fields by default, so
    the filter vanished silently: the run considered the whole plan, picked a
    different village pair than the one asked for, on a 1-hour cycle (24 game
    rows instead of the intended 1), and nothing in the response said so. The
    operator would have authorised that believing it was one route.
    """

    def test_an_unknown_field_is_a_422_not_a_full_run(self):
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            _exec_body(dry_run=True, some_filter_this_server_never_heard_of=[41212])

    def test_the_filters_this_server_does_know_still_work(self):
        # The other half of the contract: forbidding extras must not break the
        # fields that exist, or every ordinary run would 422.
        res = _execute(
            _exec_body(dry_run=True, max_routes_per_run=50, only_origins=[20003]),
            connected=False,
        )
        assert {a.origin for a in res.actions} == {20003}

    def test_an_ordinary_run_with_no_filters_is_unaffected(self):
        res = _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)
        assert res.actions
        assert res.filtered_to is None


class TestCreatedMeansVerifiedNotAccepted:
    """`POST /trade-routes` answers 200 with an EMPTY body.

    That is not evidence of creation. The identical response is what "accepted
    and silently did nothing" looks like, so trusting it reports routes that do
    not exist -- and poisons the next run, which finds them missing and creates
    them again. The only honest answer is to read the marketplace back, which is
    also exactly what the game's own UI does after a create.
    """

    def test_a_normal_create_is_confirmed_against_the_page(self):
        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["created", "created"]
        assert res.problems == []

    def test_a_create_that_produces_no_route_is_reported_as_not_created(self):
        # The whole point: the game says yes, the page says nothing is there.
        svc = _FakeLiveSvc(phantom_creates=True)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert svc.created, "the writes were still sent"
        assert [a.status for a in res.actions] == ["not_created", "not_created"]
        assert res.created == 0, "a run that created nothing must not claim otherwise"

    def test_it_says_plainly_that_nothing_was_created(self):
        svc = _FakeLiveSvc(phantom_creates=True)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert res.problems, "silence here would be a false success"
        joined = " ".join(res.problems)
        assert "accepted the create but no route appeared" in joined
        assert "do not assume otherwise" in joined

    def test_a_phantom_create_still_counts_as_outstanding_work(self):
        # It must come back on a later run, not be written off as done.
        svc = _FakeLiveSvc(phantom_creates=True)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        assert res.remaining >= 2

    def test_a_failed_read_back_is_unverified_not_failed(self):
        # "I could not check" and "it did not work" are different answers.
        # Collapsing them would have the operator delete routes that exist.
        svc = _FakeLiveSvc(confirm_raises={20003})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        by_origin = {a.origin: a.status for a in res.actions}
        assert by_origin[20003] == "created_unverified"
        assert by_origin[20011] == "created", "the other village verified fine"
        joined = " ".join(res.problems)
        assert "could not re-read the marketplace to confirm" in joined

    def test_the_verification_is_recorded_in_the_trace(self):
        import json
        from pathlib import Path

        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        verified = [e for e in events if e["kind"] == "verified"]
        assert verified, "the read-back must be in the audit record"
        assert verified[0]["claimed"] == 1, "one create request per origin here"
        # ROWS, not requests: one 6-hour create is four daily rows in the game,
        # which is what the double now makes appear. The old expectation of 1
        # was a property of a double that under-modelled the fan-out, not of the
        # field -- `new_rows_found` has always been len(fresh).
        assert verified[0]["new_rows_found"] == 4
        assert verified[0]["new_route_ids"]

    def test_a_run_that_creates_nothing_does_not_read_back(self):
        # No write, no verification: it would be a request for nothing.
        svc = _FakeLiveSvc()
        _execute(_exec_body(dry_run=False, disable_existing=False, max_routes_per_run=50), svc=svc)
        svc.created.clear()
        res = _execute(
            _exec_body(dry_run=False, disable_existing=False, max_routes_per_run=50), svc=svc
        )

        assert svc.created == []
        import json
        from pathlib import Path

        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]
        assert [e for e in events if e["kind"] == "verified"] == []


class TestASessionThatDiesMidRun:
    """The HttpClient can go away underneath a run: a disconnect, a worker
    reload, a captcha-driven teardown. What must never happen is a run that
    reports success for writes it could not make, or loses the record of writes
    it did make before the session went."""

    def test_a_network_failure_mid_run_keeps_the_earlier_writes_in_the_response(self):
        # The real service catches NetworkError around its own POST and returns a
        # "failed" result rather than raising, so this is the shape production
        # actually produces: one write landed, the next did not, and the run has
        # to report both truthfully.
        from travian_api.services.trade_route_service import RouteActionResult

        class _DyingSvc(_FakeLiveSvc):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._n = 0

            async def create_route(self, route, *, stop_check=None):
                self._n += 1
                if self._n == 2:
                    return RouteActionResult(
                        route.origin_village_id,
                        route.dest_x,
                        route.dest_y,
                        "failed",
                        "session closed underneath the run",
                    )
                return await super().create_route(route, stop_check=stop_check)

        svc = _DyingSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        statuses = [a.status for a in res.actions]
        assert "created" in statuses, "the write that landed must still be reported"
        assert "failed" in statuses, "and the one that did not must be reported as failed"
        assert res.created == 1
        assert res.remaining >= 1, "the failed route is still outstanding work"

    def test_an_error_that_escapes_the_service_still_names_what_committed(self):
        # The other half: if anything DOES escape (a bug, a teardown mid-await),
        # the 500 must carry the committed count and the trace id rather than
        # discarding the record. Pinned here as a session-death scenario too,
        # because that is the realistic way it happens.
        from travian_api.exceptions import NetworkError

        class _HardDyingSvc(_FakeLiveSvc):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._n = 0

            async def create_route(self, route, *, stop_check=None):
                self._n += 1
                if self._n == 2:
                    raise NetworkError("session closed underneath the run")
                return await super().create_route(route, stop_check=stop_check)

        svc = _HardDyingSvc()
        with pytest.raises(HTTPException) as caught:
            _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        detail = str(caught.value.detail)
        assert caught.value.status_code == 500
        assert "creating 1 route(s)" in detail, "the committed write must be named"
        assert "trace" in detail and "revert-plan" in detail

    def test_a_dead_session_during_the_confirmation_does_not_claim_success(self):
        # The write went out; the read-back could not. That is unverified, not
        # created -- and definitely not failed.
        svc = _FakeLiveSvc(confirm_raises={20003, 20011})
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert {a.status for a in res.actions} == {"created_unverified"}
        assert res.created == 0, "nothing may be counted as confirmed"
        assert res.problems, "and the operator must be told to check"


class TestTwoRunsCannotRaceTheSameVillage:
    """Two reconciliations of one village in parallel would each read a state the
    other is about to change, and both would create the same route."""

    def test_the_account_lock_serialises_whole_runs(self):
        # Already covered for the 409 path; this pins that the lock is what does
        # it, by holding it and observing the second run refuse rather than read.
        svc = _FakeLiveSvc()

        async def _drive():
            await svc.execute_lock.acquire()
            try:
                with (
                    _patch(dist_module, "session_manager", _SessMgr(svc, True)),
                    _patch(dist_module.random, "shuffle", lambda seq: None),
                    pytest.raises(HTTPException) as caught,
                ):
                    await post_execute(_exec_body(dry_run=False), _USER)
                return caught.value.status_code
            finally:
                svc.execute_lock.release()

        assert asyncio.run(_drive()) == 409
        assert svc.listed == [], "a refused run must not even read a marketplace"

    def test_the_per_origin_lock_exists_and_is_keyed_by_village(self):
        # Defence in depth under the account lock: two callers of the SERVICE
        # (not the endpoint) must still not interleave on one village.
        from travian_api.services.trade_route_service import TradeRouteService

        svc = TradeRouteService(SimpleNamespace())
        order = []

        async def _hold(tag, vid):
            async with svc.origin_lock(vid):
                order.append(f"{tag}-in")
                await asyncio.sleep(0)
                order.append(f"{tag}-out")

        async def _both_same_village():
            await asyncio.gather(_hold("a", 20003), _hold("b", 20003))

        asyncio.run(_both_same_village())
        assert order == ["a-in", "a-out", "b-in", "b-out"], (
            "one village's reconciliation must not interleave with another's"
        )

    def test_two_different_villages_are_not_serialised_against_each_other(self):
        # The lock must be per village, or a 25-village run would be needlessly
        # sequential in a way that also lengthens the burst window.
        from travian_api.services.trade_route_service import TradeRouteService

        svc = TradeRouteService(SimpleNamespace())
        order = []

        async def _hold(tag, vid):
            async with svc.origin_lock(vid):
                order.append(f"{tag}-in")
                await asyncio.sleep(0)
                order.append(f"{tag}-out")

        async def _two_villages():
            await asyncio.gather(_hold("a", 20003), _hold("b", 20011))

        asyncio.run(_two_villages())
        assert order == ["a-in", "b-in", "a-out", "b-out"], "different villages may overlap"


class TestADisableIsAlsoVerified:
    """A disable is a claim until the page is read back, exactly like a create.

    And an undisabled stale route is the worse of the two failures: an uncreated
    route means resources that did not move, while a route the app believes it
    switched off keeps shipping cargo the plan has already spent elsewhere.
    """

    def _account_with_a_stale_route(self):
        return _own_village_account()

    def test_a_disable_that_took_effect_is_reported_cleanly(self):
        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(9, _UNWANTED_DEST, active=True)]})
        res = _run_live(
            svc, self._account_with_a_stale_route(), disable_existing=True, max_routes_per_run=50
        )

        assert svc.disabled, "the stale route was disabled"
        assert not [p for p in res.problems if "STILL ACTIVE" in p]

    def test_a_disable_that_did_not_take_effect_is_reported_loudly(self):
        # The game accepted the PUT and the row is still enabled.
        class _IgnoresDisables(_FakeLiveSvc):
            async def disable_routes(self, vid, routes, *, stop_check=None):
                result = await super().disable_routes(vid, routes, stop_check=stop_check)
                # Report success but leave the rows exactly as they were.
                for row in self._existing.get(vid, []):
                    row.active = True
                return result

        svc = _IgnoresDisables(existing={20003: [ExistingRoute(9, _UNWANTED_DEST, active=True)]})
        res = _run_live(
            svc, self._account_with_a_stale_route(), disable_existing=True, max_routes_per_run=50
        )

        joined = " ".join(res.problems)
        assert "STILL ACTIVE" in joined, "a disable that did nothing must not read as done"
        assert "shipping resources the plan does not account for" in joined

    def test_the_disable_verification_is_in_the_trace(self):
        import json
        from pathlib import Path

        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(9, _UNWANTED_DEST, active=True)]})
        res = _run_live(
            svc, self._account_with_a_stale_route(), disable_existing=True, max_routes_per_run=50
        )
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        verified = [e for e in events if e["kind"] == "verified_disables"]
        assert verified, "the audit record must show the disable was checked"
        assert verified[0]["claimed"] == [9]
        assert verified[0]["still_active"] == []

    def test_a_disable_only_run_still_reads_back(self):
        # No creates at all: the verification must not be skipped just because
        # nothing was created. It was gated on creates alone before.
        account = _own_village_account()
        svc = _FakeLiveSvc(
            existing={
                20003: [
                    ExistingRoute(9, _UNWANTED_DEST, active=True),
                    *_fanned(20011, start_id=10),
                ]
            }
        )
        res = _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert svc.created == [], "the plan's route already exists"
        assert svc.disabled, "the stale one was disabled"
        import json
        from pathlib import Path

        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]
        assert [e for e in events if e["kind"] == "verified_disables"], (
            "a run that only disabled must still confirm it"
        )


class TestCargoDriftIsCorrected:
    """A route is created once; the plan moves every time production does.

    Without this the live routes keep the cargo they were born with and slowly
    come to describe a different account than the sheet does, with nothing
    detecting the divergence. That is the failure mode of a planner nobody
    revisits: it silently stops being true.
    """

    def _account(self):
        return _own_village_account()

    def _existing(self, crop):
        from travian_api.services.distribution.allocation import Resource

        return {20003: _fanned(20011, start_id=800, cargo={Resource.CROP: crop})}

    def test_matching_cargo_is_left_alone(self):
        # The plan ships 100 crop (see _own_village_account).
        svc = _FakeLiveSvc(existing=self._existing(100))
        res = _run_live(svc, self._account(), max_routes_per_run=50, update_drifted=True)

        assert res.updates == [], "an unchanged route must not be rewritten"
        assert [a.status for a in res.actions] == ["skipped"]

    def test_a_small_difference_is_not_worth_a_request(self):
        # Within tolerance: rewriting for this would churn every run and spend
        # activity budget to change nothing that matters.
        svc = _FakeLiveSvc(existing=self._existing(150))
        res = _run_live(svc, self._account(), max_routes_per_run=50, update_drifted=True)

        assert res.updates == []

    def test_real_drift_is_corrected_in_one_request(self):
        svc = _FakeLiveSvc(existing=self._existing(9000))
        res = _run_live(svc, self._account(), max_routes_per_run=50, update_drifted=True)

        assert len(res.updates) == 1
        assert "cargo reset on 4 row(s)" in res.updates[0]
        assert [a.status for a in res.actions] == ["updated"]
        assert svc.created == [], "correcting a route is not creating one"

    def test_it_is_off_unless_asked_for(self):
        # Correcting cargo overwrites a route the operator may have tuned by
        # hand, so it cannot be a silent side effect of an ordinary run.
        svc = _FakeLiveSvc(existing=self._existing(9000))
        res = _run_live(svc, self._account(), max_routes_per_run=50)

        assert res.updates == []
        assert [a.status for a in res.actions] == ["skipped"]
        assert "cargo stale" in res.actions[0].detail, "but the operator is told"

    def test_unknown_live_cargo_is_never_treated_as_drift(self):
        # The page did not say. Absence of evidence must not rewrite every route.
        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(800, 20011, active=True, cargo=None)]})
        res = _run_live(svc, self._account(), max_routes_per_run=50, update_drifted=True)

        assert res.updates == []

    def test_a_failed_update_says_the_route_is_still_shipping_the_old_amounts(self):
        from travian_api.services.trade_route_service import RouteActionResult

        class _RefusesUpdates(_FakeLiveSvc):
            async def update_cargo(
                self, vid, routes, cargo, *, dest_x=None, dest_y=None, stop_check=None
            ):
                return RouteActionResult(vid, 0, 0, "failed", "rejected (test)")

        svc = _RefusesUpdates(existing=self._existing(9000))
        res = _run_live(svc, self._account(), max_routes_per_run=50, update_drifted=True)

        assert res.updates == []
        joined = " ".join(res.problems)
        assert "could not be corrected" in joined
        assert "still shipping the old amounts" in joined

    def test_the_update_is_recorded_in_the_trace(self):
        import json
        from pathlib import Path

        svc = _FakeLiveSvc(existing=self._existing(9000))
        res = _run_live(svc, self._account(), max_routes_per_run=50, update_drifted=True)
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        updated = [e for e in events if e["kind"] == "decision" and e["decision"] == "updated"]
        assert updated, "a write must always be in the audit record"
        assert updated[0]["route_ids"] == [800, 801, 802, 803]
        assert events[-1]["cargo_updated"] == 1


class TestEveryKindOfWriteIsVerified:
    """Creates and disables were read back; re-enables and cargo updates were not.

    An origin whose only write was a re-enable, or only a cargo correction, got
    no read-back at all — so a service that reported success without the page
    changing produced a clean run. The whole argument for verifying a create
    applies unchanged to the other two: the response says the request was
    accepted, not that the state changed.
    """

    def _events(self, res):
        import json
        from pathlib import Path

        return [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

    def test_a_re_enable_only_run_still_reads_back(self):
        svc = _FakeLiveSvc(existing={20003: _fanned(20011, active=False)})
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert res.re_enables, "the fixture is meant to re-enable"
        kinds = [e["kind"] for e in self._events(res)]
        assert "verified_reenables" in kinds

    def test_a_re_enable_the_game_ignored_is_reported(self):
        class _IgnoresEnables(_FakeLiveSvc):
            async def enable_routes(self, vid, routes, *, stop_check=None):
                result = await super().enable_routes(vid, routes, stop_check=stop_check)
                for row in self._existing.get(vid, []):
                    row.active = False  # claim success, change nothing
                return result

        svc = _IgnoresEnables(existing={20003: _fanned(20011, active=False)})
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50)

        joined = " ".join(res.problems)
        assert "STILL DISABLED" in joined
        assert "nothing is shipping" in joined

    def test_a_re_enabled_route_is_not_reported_as_already_active(self):
        # The response used to contradict itself: re_enables named the route and
        # actions called it "route already active".
        svc = _FakeLiveSvc(existing={20003: _fanned(20011, active=False)})
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["re_enabled"]
        assert "switched back on" in res.actions[0].detail

    def test_a_cargo_update_only_run_still_reads_back(self):
        from travian_api.services.distribution.allocation import Resource

        svc = _FakeLiveSvc(existing={20003: _fanned(20011, cargo={Resource.CROP: 9000})})
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50, update_drifted=True)

        assert res.updates
        kinds = [e["kind"] for e in self._events(res)]
        assert "verified_updates" in kinds

    def test_a_cargo_update_the_game_ignored_is_reported(self):
        from travian_api.services.distribution.allocation import Resource
        from travian_api.services.trade_route_service import RouteActionResult

        class _IgnoresUpdates(_FakeLiveSvc):
            async def update_cargo(
                self, vid, routes, cargo, *, dest_x=None, dest_y=None, stop_check=None
            ):
                # Report success, leave the cargo exactly as it was.
                return RouteActionResult(vid, 0, 0, "updated", f"{len(routes)} route(s)")

        svc = _IgnoresUpdates(existing={20003: _fanned(20011, cargo={Resource.CROP: 9000})})
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50, update_drifted=True)

        joined = " ".join(res.problems)
        assert "still shows the old amounts" in joined
        assert "shipping something the plan did not ask for" in joined


class TestTheUpdateBurstIsBounded:
    """Cargo corrections were the one write with no cap at all.

    `max_routes_per_run` bounds creates and marketplace reads. Cargo updates
    fired one PACED PUT per desired route outside that accounting, so a drifted
    account turned a run the operator capped at three into a long burst of writes
    — each with its own human delay, from a run they believed was small.
    """

    def _drifted(self, n):
        """*n* drifted destinations, each fully fanned out."""
        from travian_api.services.distribution.allocation import Resource

        rows = []
        for d in range(n):
            rows.extend(_fanned(20011 + d, start_id=900 + 10 * d, cargo={Resource.CROP: 9000}))
        return {20003: rows}

    def _two_destinations(self):
        """One origin, two destinations, both already served."""
        rows = (
            SheetRow(
                origin=20003,
                destination=20011,
                cargo={Resource.CROP: 100},
                cycle_hours=6,
                dispatch_minute=100,
                arrival_minute=0,
                merchants=1,
            ),
            SheetRow(
                origin=20003,
                destination=20012,
                cargo={Resource.CROP: 100},
                cycle_hours=6,
                dispatch_minute=400,
                arrival_minute=0,
                merchants=1,
            ),
        )
        return SimpleNamespace(
            plan=SimpleNamespace(is_feasible=True, warnings=(), rows=rows),
            names={20003: "03", 20011: "11", 20012: "12"},
            coords={20003: (0, 0), 20011: (10, 0), 20012: (0, 10)},
            warnings=[],
            dropped_allocations=[],
        )

    def test_updates_stop_at_the_cap(self):
        # Two drifted destinations, cap of one: the second must be left alone AND
        # the operator told, or a stale route reads as a clean skip.
        existing = {
            20003: [
                *_fanned(20011, start_id=901, cargo={Resource.CROP: 9000}),
                *_fanned(20012, dispatch_minute=400, start_id=911, cargo={Resource.CROP: 9000}),
            ]
        }
        svc = _FakeLiveSvc(existing=existing)

        res = _run_live(svc, self._two_destinations(), max_routes_per_run=1, update_drifted=True)

        assert len(svc.updated) == 1, f"cap was 1, fired {len(svc.updated)}"
        assert any("update cap reached" in a.detail for a in res.actions), (
            "and the operator is told the rest were left alone"
        )

    def test_within_the_cap_it_still_corrects(self):
        svc = _FakeLiveSvc(existing=self._drifted(1))
        res = _run_live(svc, _own_village_account(), max_routes_per_run=5, update_drifted=True)
        assert len(res.updates) == 1


class TestMarketplaceReadsBillTheActivityCeiling:
    """The reads were free, so a run reported roughly half the traffic it spent.

    The ceiling is shared with the farm and oasis loops, so under-reporting here
    does not just understate this feature — it lets the OTHER loops overspend the
    account's daily total.
    """

    def _service(self):
        from travian_api.services.trade_route_service import TradeRouteService

        logged: list[float] = []
        client = SimpleNamespace(
            settings=SimpleNamespace(base_url="https://example.invalid"),
            human_delay=SimpleNamespace(wait=_noop),
            activity_scheduler=SimpleNamespace(log_activity=logged.append),
            get_html=_empty_marketplace,
            post_json=_empty_readback,
        )
        return TradeRouteService(client, live_enabled=True, reconciler_verified=True), logged

    def test_opening_the_marketplace_is_billed(self):
        service, logged = self._service()
        asyncio.run(service.open_marketplace(20003))
        assert len(logged) == 1, "two GETs, billed as one page visit"

    def test_refreshing_it_is_billed(self):
        # Still billed after moving to GraphQL: the request class changed, the
        # throttler gap it consumed did not.
        service, logged = self._service()
        asyncio.run(service.refresh_marketplace(20003))
        assert len(logged) == 1


async def _noop(*a, **k):
    return None


async def _empty_marketplace(path, **kw):
    return (
        "<html><body><script>window.Travian.React.TradeRoutes.render("
        '{viewData: {"ownPlayer":{"village":{"marketplace":{"tradeRoutes":[]}}}}}'
        ");</script></body></html>"
    )


async def _empty_readback(path, payload, **kw):
    # What the marketplace query answers for a village with no routes.
    return {
        "data": {
            "ownPlayer": {
                "id": 1,
                "currentVillageId": 20003,
                "village": {"marketplace": {"tradeRoutes": []}},
            }
        }
    }


# A destination no plan in these tests ever wants, so a live route to it is
# unambiguously stale wherever it is found.
_ORPHAN_DEST = 20099


def _one_origin_account():
    """A plan that uses ONLY 20003 as an origin, on an account that has 20011 too.

    This is the shape that matters and the shape the tests lacked: a village can
    hold routes from a PREVIOUS plan while playing no part in the current one.
    """
    return _account(
        [_row(20003, -1, 40, 40, 100)],
        {20003: (0, 0), 20011: (10, 0), -1: (40, 40)},
        {20003: "03", 20011: "11", -1: "A"},
    )


class TestReconcilingEveryVillageNotJustThePlansOrigins:
    """A distribution plan is a conservation system, so half of one is worse than
    either whole.

    Every village's retention is balanced against every other's. One surviving
    route from a previous plan keeps shipping into a village the new plan sized
    for less -- so the receiver overflows AND the sender drains, and the account
    ends up in a state that is neither plan. "Mostly reconciled" is not a weaker
    version of correct here; it is its own failure.

    Visiting only the origins the CURRENT plan uses cannot find those routes: a
    village dropped from the plan is exactly the village whose routes are stale,
    and exactly the one never read. Switching between a day and a night profile
    does this every time.
    """

    def test_by_default_a_village_the_plan_dropped_is_never_even_read(self):
        # Documents the existing bound rather than condemning it: re-reading
        # every marketplace on every run is real traffic, and in steady state
        # there is nothing to find.
        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        _run_live(svc, _one_origin_account(), disable_existing=True, max_routes_per_run=50)

        assert svc.listed == [20003], "only the plan's own origin is visited"
        assert svc.disabled == [], "so 20011's stale route is never found"

    def test_reconcile_all_origins_reads_every_village_in_the_snapshot(self):
        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=50,
            reconcile_all_origins=True,
        )

        assert sorted(svc.listed) == [20003, 20011], "every own village is swept"
        assert [vid for vid, _ in svc.disabled] == [20011], "and the orphan is switched off"

    def test_a_village_with_no_planned_routes_does_not_crash_the_sweep(self):
        # 20011 is absent from desired_by_origin entirely. Indexing that map
        # instead of getting from it would raise KeyError mid-run, after earlier
        # villages had already been written to.
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=50,
            reconcile_all_origins=True,
        )

        assert sorted(svc.listed) == [20003, 20011]
        assert res.problems == [], "an empty village is not a problem, just empty"

    def test_a_create_cap_of_zero_is_a_disable_only_sweep(self):
        # The ordering guarantee, without a two-phase refactor: run once with no
        # create budget and the game is left holding NOTHING the plan does not
        # want. Every later capped run then adds a safe subset -- the account is
        # never in the conflicting state, only ever an incomplete one.
        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        res = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=0,
            reconcile_all_origins=True,
        )

        assert svc.created == [], "no create budget means nothing is created"
        assert [vid for vid, _ in svc.disabled] == [20011], "but staleness is still cleared"
        assert sorted(svc.listed) == [20003, 20011], "and every village is still swept"
        assert res.remaining, "the plan's own route is reported as still to do"


class TestABoundedSweepSaysWhatItDidNotReach:
    """A sweep is a guarantee only when it is complete, so a short one must say so.

    A full reconciliation cannot fit in one HTTP call: fifty paced reads alone
    run past the client's two-minute timeout before a single write delay, idle
    browse or session break is added, and those are the things that make the
    traffic look human. So the sweep is chunked, and the caller loops.

    That makes the reporting load-bearing. "Swept as far as village nine" read as
    "swept everything" is precisely the false confidence this whole change exists
    to remove -- one unvisited village can still hold a route the plan rejected,
    and one such route breaks the conservation the plan rests on.
    """

    def test_a_bounded_sweep_reports_the_villages_it_did_not_reach(self):
        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        res = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=0,
            reconcile_all_origins=True,
            max_origins_per_run=1,
        )

        assert len(svc.listed) == 1, "the bound must actually bound the reads"
        assert res.swept_origins == svc.listed, "and report exactly what it read"
        assert res.unswept_origins == [20011], "and name what is still outstanding"
        assert svc.disabled == [], "20011's stale route is untouched -- it was not reached"

    def test_the_second_chunk_finishes_what_the_first_left(self):
        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        first = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=0,
            reconcile_all_origins=True,
            max_origins_per_run=1,
        )
        second = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=0,
            reconcile_all_origins=True,
            only_origins=first.unswept_origins,
        )

        assert second.swept_origins == [20011]
        assert second.unswept_origins == [], "nothing outstanding: the sweep is complete"
        assert [vid for vid, _ in svc.disabled] == [20011], "and the orphan is finally off"

    def test_an_unbounded_sweep_reports_nothing_outstanding(self):
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=0,
            reconcile_all_origins=True,
        )

        assert sorted(res.swept_origins) == [20003, 20011]
        assert res.unswept_origins == []

    def test_a_plan_origin_sweep_never_claims_to_have_swept_the_account(self):
        # Without reconcile_all_origins the run is not a sweep at all, so it must
        # not report swept/unswept lists that could be mistaken for one.
        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        res = _run_live(svc, _one_origin_account(), disable_existing=True, max_routes_per_run=50)

        assert res.swept_origins == [], "this run did not sweep the account"
        assert res.unswept_origins == []


class TestASweepDoesNotReadAsASweep:
    """The traffic SHAPE is the tell, not the request spacing.

    The throttler spaces requests and SessionTempo drifts the pace, but a run of
    marketplace visits with nothing else between them is a pattern no player
    produces however well-spaced it is. The farm-list, scouting and build-queue
    loops all inject idle browsing for this reason; the trade-route path was the
    only write path in the app that did not.
    """

    def test_idle_browsing_is_injected_between_villages(self):
        seen = []

        class _Injector:
            async def maybe_inject_noise(self, village_id=None):
                seen.append(village_id)
                return True

        svc = _FakeLiveSvc(existing={20011: []})
        svc.http_client.noise_injector = _Injector()
        _run_live(
            svc,
            _one_origin_account(),
            max_routes_per_run=0,
            reconcile_all_origins=True,
        )

        assert seen == [20003, 20011], "every visited village gets the chance to browse"

    def test_a_failing_idle_browse_never_breaks_the_run(self):
        # Camouflage is not the operation. A dead navigator must not undo a
        # disable that already landed in the game.
        class _Broken:
            async def maybe_inject_noise(self, village_id=None):
                raise RuntimeError("navigator died")

        svc = _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]})
        svc.http_client.noise_injector = _Broken()
        res = _run_live(
            svc,
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=0,
            reconcile_all_origins=True,
        )

        assert [vid for vid, _ in svc.disabled] == [20011], "the write still landed"
        assert res.problems == [], "a failed browse is not an operation problem"

    def test_an_unfinished_sweep_asks_the_caller_to_wait_a_varying_time(self):
        svc = _FakeLiveSvc(existing={20011: []})
        waits = set()
        for _ in range(6):
            res = _run_live(
                svc,
                _one_origin_account(),
                max_routes_per_run=0,
                reconcile_all_origins=True,
                max_origins_per_run=1,
            )
            assert res.unswept_origins == [20011]
            assert res.next_chunk_wait_seconds is not None
            assert 45.0 <= res.next_chunk_wait_seconds <= 240.0
            waits.add(res.next_chunk_wait_seconds)

        # A client that comes back on a metronome is its own signature, however
        # long the interval, so the suggested gap must actually vary.
        assert len(waits) > 1, "a fixed gap would just be a slower fingerprint"

    def test_a_finished_sweep_asks_for_no_wait(self):
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            _one_origin_account(),
            max_routes_per_run=0,
            reconcile_all_origins=True,
        )

        assert res.unswept_origins == []
        assert res.next_chunk_wait_seconds is None, "nothing left to come back for"


def _row_with_cycle(origin, destination, cycle, dispatch_minute=0):
    return SheetRow(
        origin=origin,
        destination=destination,
        cargo={Resource.CROP: 100},
        cycle_hours=cycle,
        dispatch_minute=dispatch_minute,
        arrival_minute=0,
        merchants=2,
    )


class TestTheRowFootprintCanBeCappedNotJustTheRequestCount:
    """The operator authorises a footprint, not a request count.

    A "route" is not what lands in the game. Travian turns one "repeat every N
    hours" request into 24/N separate daily rows, so a run capped at three routes
    on a one-hour cycle writes SEVENTY-TWO rows -- and removing them later means
    deleting every one. The run already REPORTS this (max_game_rows_this_run) but
    nothing bounded it, so the number the operator agreed to and the number they
    got were different units.

    Measured on a real plan: 36 route requests came to 442 rows, 104 of them on a
    single village.
    """

    def _one_route(self, cycle):
        return _account(
            [_row_with_cycle(20003, -1, cycle, 100)],
            {20003: (0, 0), -1: (40, 40)},
            {20003: "03", -1: "A"},
        )

    def test_a_route_whose_fan_out_exceeds_the_budget_is_deferred(self):
        # A 1h cycle is 24 rows. Ten is not enough for it, and a route cannot be
        # created "partly" -- so it waits rather than overshooting the budget.
        svc = _FakeLiveSvc()
        res = _run_live(svc, self._one_route(1), max_routes_per_run=50, max_game_rows_per_run=10)

        assert svc.created == [], "24 rows do not fit in a 10-row budget"
        assert res.remaining == 1, "and it is reported as still to do"

    def test_the_same_route_is_created_once_the_budget_covers_it(self):
        svc = _FakeLiveSvc()
        _run_live(svc, self._one_route(1), max_routes_per_run=50, max_game_rows_per_run=24)

        assert len(svc.created) == 1, "exactly 24 fits in 24"

    def test_the_budget_is_spent_across_routes_not_per_route(self):
        account = _account(
            [_row_with_cycle(20003, -1, 2, 100), _row_with_cycle(20011, -2, 2, 700)],
            {20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", 20011: "11", -1: "A", -2: "B"},
        )
        svc = _FakeLiveSvc()
        res = _run_live(svc, account, max_routes_per_run=50, max_game_rows_per_run=12)

        assert len(svc.created) == 1, "each 2h route is 12 rows; only one fits"
        assert res.remaining == 1

    def test_zero_is_unbounded_so_every_existing_run_is_unchanged(self):
        account = _account(
            [_row_with_cycle(20003, -1, 1, 100), _row_with_cycle(20011, -2, 1, 700)],
            {20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", 20011: "11", -1: "A", -2: "B"},
        )
        svc = _FakeLiveSvc()
        _run_live(svc, account, max_routes_per_run=50, max_game_rows_per_run=0)

        assert len(svc.created) == 2, "no row budget means the old behaviour exactly"


class TestRoutesTheOperatorWantsLeftAlone:
    """The plan is authoritative, but not everything in the game came from a plan.

    The reconciler's rule is "active, identifiable, and not wanted by the plan =>
    stale", which is right for routes a previous plan created and wrong for a
    route the operator made by hand. Without an exemption the app switches such a
    route off, the operator switches it back on, and the next run switches it off
    again -- the two fight, and the app always wins.

    So a destination can be declared off-limits. Off by default: silently
    protecting things would make `disable_existing` quietly stop working, which
    is the failure mode the reconciler exists to prevent.
    """

    def test_a_stale_route_to_a_protected_village_is_left_running(self):
        account = _own_village_account()
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(1, _UNWANTED_DEST, dest_x=None, dest_y=None)]}
        )
        res = _run_live(
            svc,
            account,
            disable_existing=True,
            max_routes_per_run=50,
            protect_destinations=[str(_UNWANTED_DEST)],
        )

        assert svc.disabled == [], "a protected destination is never switched off"
        # Silence here would look like a clean reconciliation while the route
        # keeps shipping resources the plan does not account for.
        assert any("protected" in w.lower() for w in res.disables + res.warnings), (
            "the run must say it left something running on purpose"
        )

    def test_the_same_route_is_disabled_without_the_exemption(self):
        # The control. If this ever passes with the protection applied, the
        # exemption has stopped being an exemption and become a bug.
        account = _own_village_account()
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(1, _UNWANTED_DEST, dest_x=None, dest_y=None)]}
        )
        _run_live(svc, account, disable_existing=True, max_routes_per_run=50)

        assert [vid for vid, _ in svc.disabled] == [20003]

    def test_a_foreign_destination_is_protected_by_its_coordinates(self):
        # A foreign target has no usable village id -- the plan knows it only by
        # coordinates -- so an id-only exemption could not protect one at all.
        account = _own_village_account()
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(1, _UNWANTED_DEST, dest_x=77, dest_y=88)]}
        )
        _run_live(
            svc,
            account,
            disable_existing=True,
            max_routes_per_run=50,
            protect_destinations=["77|88"],
        )

        assert svc.disabled == [], "coordinates identify a hand-made foreign route"

    def test_protection_does_not_stop_the_plan_creating_its_own_routes(self):
        # An exemption narrows what is DISABLED. It must not also narrow what is
        # created, or protecting one destination would quietly stall the plan.
        account = _two_origin_account()
        svc = _FakeLiveSvc()
        _run_live(
            svc,
            account,
            disable_existing=True,
            max_routes_per_run=50,
            protect_destinations=["999999"],
        )

        assert len(svc.created) == 2, "creates are untouched by a disable exemption"

    def test_a_malformed_entry_is_rejected_rather_than_ignored(self):
        # Silently dropping "4688" (a typo for 46|88) would leave the operator
        # believing a route was protected when it was not -- and the next run
        # would switch it off.
        import pytest as _pytest
        from pydantic import ValidationError

        with _pytest.raises(ValidationError):
            _exec_body(dry_run=True, protect_destinations=["not-a-place"])


class TestTheRunStopsFiringWhenTheGameKeepsRefusing:
    """Repeated rejects are both wasted writes and a loud signal.

    A missing Gold Club stops the run immediately — the code says outright that a
    human would not keep firing rejects. But an ordinary `failed` create did not
    stop anything: the loop moved to the next route and tried again. The Gold Club
    per-village route limit has never been probed on this account (deliberately),
    so "the game refuses this create" is a real and expected outcome, and firing
    twenty more after the first is exactly what a person would not do.
    """

    def test_a_run_of_failures_stops_the_run(self):
        svc = _FakeLiveSvc(create_status="failed")
        res = _run_live(svc, _three_origin_account(), max_routes_per_run=50)

        assert len(svc.created) <= _CONSECUTIVE_FAILURE_LIMIT, (
            "the run must give up, not work through every route"
        )
        assert any("refus" in p.lower() or "fail" in p.lower() for p in res.problems), (
            "and say why it stopped"
        )

    def test_one_failure_among_successes_does_not_stop_anything(self):
        # The counter must be CONSECUTIVE. A single transient reject between two
        # good creates is not the game refusing everything.
        calls = {"n": 0}

        class _OneBadApple(_FakeLiveSvc):
            async def create_route(self, route, stop_check=None):
                calls["n"] += 1
                if calls["n"] == 2:
                    return SimpleNamespace(status="failed", detail="transient")
                return await super().create_route(route, stop_check=stop_check)

        svc = _OneBadApple()
        _run_live(svc, _three_origin_account(), max_routes_per_run=50)

        assert calls["n"] == 3, "all three routes are still attempted"

    def test_a_stopped_create_does_not_consume_row_budget(self):
        # `attempts` is rolled back when a create is stopped before the POST, so
        # the row budget must be too -- otherwise a stopped run silently spends
        # the footprint it never wrote, and the next route is deferred for rows
        # that do not exist in the game.
        class _StopsBeforeWriting(_FakeLiveSvc):
            async def create_route(self, route, stop_check=None):
                return SimpleNamespace(status="stopped", detail="captcha (test)")

        svc = _StopsBeforeWriting()
        res = _run_live(
            svc,
            _account(
                [_row_with_cycle(20003, -1, 1, 100)],
                {20003: (0, 0), -1: (40, 40)},
                {20003: "03", -1: "A"},
            ),
            max_routes_per_run=50,
            max_game_rows_per_run=24,
        )

        assert res.created_game_rows == 0, "nothing was written, so no rows exist"
        assert res.remaining == 1, "and the route is still outstanding"


class TestPruningTheFanOutToTheProfilesHours:
    """The window is enforced by subtraction, and only on confirmed rows.

    Travian has no setting that confines a route to part of the day, so the beat's
    window was a fiction: it counted the in-window firings and the game performed
    all of them. But "repeat every N hours" is 24/N individually deletable rows,
    proven on the live account -- so deleting the ones that depart outside the
    window makes the window real.

    Pruned against `fresh`, the rows the creates are CONFIRMED to have produced.
    Pruning against an unverified read would be deleting rows on a guess.
    """

    def _account(self):
        return _account(
            [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
            {20003: (0, 0), -1: (40, 40)},
            {20003: "03", -1: "A"},
        )

    def test_rows_departing_outside_the_window_are_deleted(self):
        svc = _FakeLiveSvc()
        _run_live(
            svc,
            self._account(),
            max_routes_per_run=50,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

        assert svc.deleted, "the out-of-window rows must actually be removed"

    def test_nothing_is_pruned_without_a_window(self):
        # A round-the-clock profile wants every firing; pruning would delete the
        # route set the operator asked for.
        svc = _FakeLiveSvc()
        _run_live(svc, self._account(), max_routes_per_run=50, prune_to_window=True)

        assert not getattr(svc, "deleted", []), "no window means no pruning"

    def test_nothing_is_pruned_unless_asked(self):
        svc = _FakeLiveSvc()
        _run_live(
            svc,
            self._account(),
            max_routes_per_run=50,
            dispatch_window=[23 * 60, 7 * 60],
        )

        assert not getattr(svc, "deleted", []), "off by default: it deletes rows"


class TestTheRowBudgetCountsWhatSurvives:
    """With pruning the created count is not the footprint.

    A 1h route creates 24 rows and the prune deletes the 16 outside the profile,
    so 8 remain. Charging the budget 24 makes it three times too strict for an
    8-hour window: an operator authorising 24 rows gets one route instead of
    three, and the number they agreed to is not the number they end up holding.

    Charged against what SURVIVES, because that is what the operator would have to
    delete and what the plan's arithmetic rests on. The transient 24 still happen;
    they are writes, not footprint.
    """

    def _one_hourly_route(self):
        return _account(
            [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
            {20003: (0, 0), -1: (40, 40)},
            {20003: "03", -1: "A"},
        )

    def test_a_pruned_route_is_charged_for_its_survivors_not_its_creations(self):
        # 24 created, 8 inside a 480-minute window. A budget of 8 must admit it.
        svc = _FakeLiveSvc()
        res = _run_live(
            svc,
            self._one_hourly_route(),
            max_routes_per_run=50,
            max_game_rows_per_run=8,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

        assert len(svc.created) == 1, "8 surviving rows fit an 8-row budget"
        assert res.remaining == 0

    def test_without_pruning_the_full_fan_out_is_still_charged(self):
        # The control. Nothing is deleted, so all 24 rows are the footprint and an
        # 8-row budget must refuse it.
        svc = _FakeLiveSvc()
        res = _run_live(
            svc,
            self._one_hourly_route(),
            max_routes_per_run=50,
            max_game_rows_per_run=8,
            dispatch_window=[23 * 60, 7 * 60],
        )

        assert svc.created == [], "24 rows do not fit an 8-row budget"
        assert res.remaining == 1
