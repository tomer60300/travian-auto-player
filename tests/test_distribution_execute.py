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
            # `execution_mode` is what authorises a write. `dry_run` is kept for
            # older callers and never decides, so the two must agree or the
            # request is a 422 -- which is exactly why the helper sets both.
            "execution_mode": "preview" if dry_run else "live",
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
        # Whether the game really switched the rows off, INDEPENDENTLY of what
        # its response said. The two were welded together, which made the one
        # case that matters here unrepresentable: a 200 whose body could not be
        # read, over rows that are now off. That is the whole of an `unverified`
        # toggle -- the write landed and the answer did not prove it -- and the
        # caller is supposed to settle it by looking. None keeps the old
        # coupling.
        disable_applies=None,
        enable_status="enabled",
        budget_ok=True,
        read_raises=None,
        phantom_creates=False,
        confirm_raises=None,
        rows_per_create=None,
        delete_status="deleted",
        # Whether the rows really went, INDEPENDENTLY of what the answer said --
        # the same split `disable_applies` makes, and for the same case: a 200
        # whose body could not be read over rows that are now gone.
        delete_applies=None,
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
        self._delete_status = delete_status
        self._delete_applies = (
            delete_status == "deleted" if delete_applies is None else delete_applies
        )
        self._disable_status = disable_status
        self._disable_applies = (
            disable_status == "disabled" if disable_applies is None else disable_applies
        )
        self._enable_status = enable_status
        self._read_raises = read_raises or set()  # origin ids whose read raises
        self.created = []  # PlannedRoute objects a create was ATTEMPTED for
        self.disabled = []  # (origin, sorted tuple of disabled dest coords)
        self.deleted = []  # (origin, sorted tuple of route ids removed for good)
        self.enabled = []  # (origin, sorted tuple of re-enabled dest coords)
        self.listed = []  # origin ids whose marketplace was READ
        self.confirmed = []  # origin ids RE-read to settle what a write did
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
        if self._delete_applies:
            # Actually gone. The read-back is what the production code trusts, so
            # a double that recorded the call and left the rows in place would
            # let a broken delete pass.
            self._existing[vid] = [e for e in self._existing.get(vid, []) if e.route_id not in ids]
        if self._delete_status != "deleted":
            return RouteActionResult(vid, 0, 0, self._delete_status, "delete unconfirmed (test)")
        return RouteActionResult(vid, 0, 0, "deleted")

    async def confirm_routes(self, vid, *, map_span=None):
        self.confirmed.append(vid)
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
        if self._disable_applies:
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
        if self._create_status == "stopped":
            # A real stop happens after the pacing wait and BEFORE the POST
            # (trade_route_service.create_route), so nothing reaches the game.
            # This double used to record the attempt and fan out the rows anyway
            # while returning "stopped" -- a resumed-run test then believed the
            # marketplace held routes a stopped create had never made.
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "stopped", "stopped (test)"
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
        # Unbounded footprint unless a case asks otherwise. These exercise
        # creation, reconciliation and the request caps; the plan they drive is
        # hourly, so the server's own 24-row default would stop them at one
        # route and hide the behaviour under test. The row budget has its own
        # class.
        kw.setdefault("max_game_rows_per_run", 0)
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

    def test_an_unrecognised_disable_status_is_not_read_as_success(self):
        # `else: disables.append(line)` was the DEFAULT branch, so any status
        # `_toggle_routes` does not produce today reads as "switched off" and
        # lets the creates stack on top -- both schedules shipping at once,
        # which is exactly the issue-#61 guard bypassed. The same
        # else-is-success shape f47083f had to remove from `delete_routes`.
        desired = _desired_routes()
        existing = {desired[0].origin: [ExistingRoute(1, _UNWANTED_DEST, 99, 98)]}
        svc = _FakeLiveSvc(existing=existing, disable_status="partial")
        res = self._run(svc, disable_existing=True, max_routes_per_run=50)

        assert svc.created == [], "creates must not stack on rows that may still be live"
        assert any("disable" in p.lower() for p in res.problems), res.problems
        assert res.disables == [], res.disables


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


def _four_route_two_origin_account():
    """Two origins, two routes each -- enough for one origin to trip the
    consecutive-failure limit while a second origin is still unvisited."""
    rows = tuple(
        SheetRow(
            origin=origin,
            destination=destination,
            cargo={Resource.CROP: 100},
            cycle_hours=6,
            dispatch_minute=minute,
            arrival_minute=0,
            merchants=2,
        )
        for origin, destination, minute in (
            (20003, -1, 100),
            (20003, -2, 700),
            (20011, -3, 200),
            (20011, -4, 800),
        )
    )
    plan = SimpleNamespace(is_feasible=True, warnings=(), rows=rows)
    return SimpleNamespace(
        plan=plan,
        names={20003: "03", 20011: "11", -1: "A", -2: "B", -3: "C", -4: "D"},
        coords={
            20003: (0, 0),
            20011: (10, 0),
            -1: (40, 40),
            -2: (50, 50),
            -3: (60, 60),
            -4: (70, 70),
        },
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

    def test_run_start_records_what_was_asked_for_and_what_it_resolved_to(self):
        """Three separate facts, because a trace that records only the outcome
        cannot answer "was this run asked for?" afterwards -- which is the
        question a surprise write raises. The requested mode, the mode it
        resolved to, and whether the server's own switch was even open."""
        import json
        from pathlib import Path

        svc = _FakeLiveSvc()
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)
        start = json.loads(Path(res.trace_path).read_text(encoding="utf-8").splitlines()[0])

        assert start["execution_mode_requested"] == "live"
        assert start["execution_mode_resolved"] == "live"
        assert start["env_brake_open"] is True


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

    def test_an_overshoot_against_the_forecast_is_reported_too(self):
        # The attribution stopped at `want = _game_rows(cycle)`, so a game that
        # fanned out MORE than 24/N was reported as agreeing with the model and
        # the extra rows were counted in neither `created_game_rows` nor
        # `live_game_rows`. The direction that costs merchants and shipments is
        # exactly this one, and it read as clean.
        svc = _FakeLiveSvc(rows_per_create=6)
        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        created = [a for a in res.actions if a.status == "created"]
        assert all(a.game_rows == 4 for a in created), "the forecast is still 4"
        assert all(a.observed_game_rows == 6 for a in created), [
            a.observed_game_rows for a in created
        ]
        assert res.created_game_rows == 12
        assert "the game made 6 route row(s), not the 4" in " ".join(res.problems), res.problems

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


def _two_destination_account():
    """One origin, two OWN destinations -- so both are matched by village id."""
    return _account(
        [
            SheetRow(
                origin=20003,
                destination=20011,
                cargo={Resource.CROP: 100},
                cycle_hours=6,
                dispatch_minute=100,
                arrival_minute=0,
                merchants=2,
            ),
            SheetRow(
                origin=20003,
                destination=20019,
                cargo={Resource.CROP: 100},
                cycle_hours=6,
                dispatch_minute=700,
                arrival_minute=0,
                merchants=2,
            ),
        ],
        {20003: (0, 0), 20011: (10, 0), 20019: (20, 0)},
        {20003: "03", 20011: "11", 20019: "19"},
    )


class TestAnOffScheduleDestinationIsOnlyDisabledIfItCanBeRebuilt:
    """ "We NEVER disable a destination we are about to create" -- and a
    mismatched one is exactly the case that broke it.

    A destination whose live rows run a schedule the plan does not want is
    reconciled by disable-and-recreate. The disable is unbounded; the create is
    bounded by `max_routes_per_run` and by the row budget. So a run capped at one
    create against two mismatched destinations switched BOTH off and rebuilt one:
    the second destination stopped receiving anything at all, under a green
    "created 1 route(s)" with an empty `problems` list.
    """

    def _existing(self):
        # Eight 3h rows to each destination where the plan wants four 6h ones.
        return {
            20003: _fanned(20011, 10, 0, cycle_hours=3, dispatch_minute=100, start_id=710000)
            + _fanned(20019, 20, 0, cycle_hours=3, dispatch_minute=700, start_id=720000)
        }

    def _still_active(self, svc, dest):
        return [e for e in svc._existing[20003] if e.active and e.dest_village_id == dest]

    def test_the_unfundable_destination_keeps_its_rows(self):
        svc = _FakeLiveSvc(existing=self._existing())
        _run_live(svc, _two_destination_account(), disable_existing=True, max_routes_per_run=1)

        assert len(svc.created) == 1, "the cap still bounds creates"
        rebuilt = svc.created[0].dest_village_id
        starved = 20019 if rebuilt == 20011 else 20011
        assert self._still_active(svc, starved), (
            "the destination this run could not rebuild must keep shipping"
        )

    def test_it_says_which_destination_was_left_diverging(self):
        svc = _FakeLiveSvc(existing=self._existing())
        res = _run_live(
            svc, _two_destination_account(), disable_existing=True, max_routes_per_run=1
        )

        rebuilt = svc.created[0].dest_village_id
        starved_name = "19" if rebuilt == 20011 else "11"
        assert any(starved_name in p and "diverging" in p for p in res.problems), res.problems

    def test_the_route_it_could_not_rebuild_is_blocked_not_created(self):
        # Creating on top of the live rows would ship both schedules at once --
        # the same conclusion create-only mode reaches.
        svc = _FakeLiveSvc(existing=self._existing())
        res = _run_live(
            svc, _two_destination_account(), disable_existing=True, max_routes_per_run=1
        )

        blocked = [a for a in res.actions if a.status == "blocked"]
        assert len(blocked) == 1, [a.status for a in res.actions]
        assert "different schedule" in blocked[0].detail

    def test_a_cap_that_covers_both_replaces_both(self):
        # The control: with the budget to rebuild them, nothing is left running.
        svc = _FakeLiveSvc(existing=self._existing())
        res = _run_live(
            svc, _two_destination_account(), disable_existing=True, max_routes_per_run=50
        )

        assert len(svc.created) == 2
        assert res.problems == []
        assert not self._still_active(svc, 20011)[8:], "the 3h rows are gone"

    def test_a_row_budget_too_small_to_rebuild_also_holds_the_rows(self):
        # The other half of the budget: a 6h route is four rows, so a four-row
        # budget funds exactly one replacement.
        svc = _FakeLiveSvc(existing=self._existing())
        res = _run_live(
            svc,
            _two_destination_account(),
            disable_existing=True,
            max_routes_per_run=50,
            max_game_rows_per_run=4,
        )

        assert len(svc.created) == 1
        rebuilt = svc.created[0].dest_village_id
        starved = 20019 if rebuilt == 20011 else 20011
        assert self._still_active(svc, starved)
        assert res.problems, "a destination left diverging is never silent"

    def test_a_filter_excluded_mismatch_is_not_blamed_on_the_budget(self):
        # `_pairs` comes from `desired`, so a destination the run's own filter
        # dropped has no pairs and can never be reserved -- correctly, since a
        # filtered run must not disable what it cannot recreate. The refusal
        # blamed the budget for it, which sends the operator to raise a limit
        # that was never the cause.
        svc = _FakeLiveSvc(existing=self._existing())
        res = _run_live(
            svc,
            _two_destination_account(),
            disable_existing=True,
            max_routes_per_run=50,
            only_destinations=[20011],
        )

        line = next(p for p in res.problems if "19" in p)
        assert "filter" in line, line
        assert "budget" not in line, line

    def _emptied(self, svc):
        return [d for d in (20011, 20019) if not self._still_active(svc, d)]

    def test_a_refused_rebuild_puts_the_destination_it_emptied_back(self):
        # Reserving the budget is necessary and not sufficient. The reservation
        # is spent whatever the game ANSWERS, so a refused create leaves the
        # destination switched off and receiving nothing at all -- and a single
        # refusal is below the consecutive-failure limit, so nothing else says a
        # word. Measured before that was fixed: created=0, disables=['03:
        # disabled 8 route(s)'], problems=[] -- a green run over a village that
        # has stopped being supplied.
        #
        # Naming it was the first half. The second is that the disable is
        # REVERSIBLE and the rows are still there, so a wholly refused rebuild is
        # now put back rather than named and left. It is still accounted for --
        # in `re_enables` instead of `problems` -- and a restore the game refuses
        # goes back to naming it (see TestARefusedReplacementPutsTheOldRoutesBack).
        svc = _FakeLiveSvc(existing=self._existing(), create_status="failed", phantom_creates=True)
        res = _run_live(
            svc, _two_destination_account(), disable_existing=True, max_routes_per_run=1
        )

        assert self._emptied(svc) == [], "a wholly refused rebuild leaves nothing dark"
        restored = [line for line in res.re_enables if "restored" in line]
        assert len(restored) == 1, res.re_enables
        assert any(name in restored[0] for name in ("11", "19")), restored
        assert not any("receiving nothing" in problem for problem in res.problems), res.problems

    def test_a_stop_between_the_disable_and_the_rebuild_names_the_destination(self):
        # `_stop_reason()` is re-checked before EVERY create, which is right --
        # and it fires after the off-schedule rows are already off. The routes
        # then go to `deferred` under the generic "stopped early" line, which
        # reads as ordinary back-pressure: nothing said which destinations had
        # just stopped receiving anything at all.
        svc = _FakeLiveSvc(existing=self._existing())
        _disable = svc.disable_routes

        async def _exhaust_the_budget_after_disabling(vid, routes, *, stop_check=None):
            result = await _disable(vid, routes, stop_check=stop_check)
            svc.budget_ok = False
            return result

        svc.disable_routes = _exhaust_the_budget_after_disabling
        res = _run_live(
            svc, _two_destination_account(), disable_existing=True, max_routes_per_run=50
        )

        emptied = self._emptied(svc)
        assert emptied, "the disable landed before the budget ran out"
        assert svc.created == [], "and no replacement was written"
        names = {20011: "11", 20019: "19"}
        for dest in emptied:
            assert any(names[dest] in problem for problem in res.problems), (
                names[dest],
                res.problems,
            )

    def test_a_rebuild_that_landed_after_a_dead_answer_is_not_reported_as_emptied(self):
        # The other half: same refused answer, but the rows are on the page --
        # the read-back promotes the create, so the destination IS supplied and
        # the line above must be withdrawn rather than left standing.
        svc = _FakeLiveSvc(existing=self._existing(), create_status="failed")
        res = _run_live(
            svc, _two_destination_account(), disable_existing=True, max_routes_per_run=1
        )

        assert self._emptied(svc) == [], "the rebuild landed, so nothing is left empty"
        assert not any("REFUSED the replacement" in p for p in res.problems), res.problems


def _trace_events(path):
    from pathlib import Path

    return [
        json.loads(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestAReplacedDestinationLeavesARecoveryRecordBeforeItIsEmptied:
    """The worst interruption on this path is the process dying between the
    disable and the create.

    A replaced destination is emptied and refilled in two requests. If the run
    ends in between -- a kill, a worker reload, a crash -- the destination is
    receiving NOTHING, and the run's own problems list, which is built at the
    end, was never returned. The trace is the only thing that survives that, so
    the complete OLD configuration has to be recorded and flushed BEFORE the
    disable goes out.

    `ExecutionTrace._write` already flushes every event, so the ORDERING is the
    whole of it -- and the ordering is what these measure, by reading the file at
    the moment the disable is sent rather than at the end of the run.
    """

    def _existing(self):
        # Eight 3h rows to each destination where the plan wants four 6h ones.
        return {
            20003: _fanned(
                20011,
                10,
                0,
                cycle_hours=3,
                dispatch_minute=100,
                start_id=710000,
                cargo={Resource.CROP: 250},
            )
            + _fanned(
                20019,
                20,
                0,
                cycle_hours=3,
                dispatch_minute=700,
                start_id=720000,
                cargo={Resource.CROP: 250},
            )
        }

    def _run(self, svc, **kw):
        kw.setdefault("max_routes_per_run", 1)
        return _run_live(svc, _two_destination_account(), disable_existing=True, **kw)

    def test_the_record_is_on_disk_before_the_disable_is_sent(self):
        class _WatchingSvc(_FakeLiveSvc):
            """Snapshots the trace file at the moment the disable goes out.

            Asserting the event's presence at the END of a run proves nothing --
            an event written afterwards is exactly the bug."""

            def __init__(self, **kw):
                super().__init__(**kw)
                self.at_disable = None

            async def disable_routes(self, vid, routes, *, stop_check=None):
                if self.at_disable is None:
                    self.at_disable = _trace_events(self.trace.path)
                return await super().disable_routes(vid, routes, stop_check=stop_check)

        svc = _WatchingSvc(existing=self._existing())
        self._run(svc)

        assert svc.at_disable is not None, "the disable must have been sent"
        started = [e for e in svc.at_disable if e["kind"] == "replacement_started"]
        assert started, (
            "no replacement_started was on disk when the rows were switched off: "
            f"{[e['kind'] for e in svc.at_disable]}"
        )

    def test_the_record_carries_the_whole_old_configuration(self):
        svc = _FakeLiveSvc(existing=self._existing())
        res = self._run(svc)

        started = [e for e in _trace_events(res.trace_path) if e["kind"] == "replacement_started"]
        assert len(started) == 1, "one destination is reserved at a cap of one"
        record = started[0]
        assert record["origin"] == 20003
        # Eight rows on a 3h cycle, each with everything needed to put it back by
        # hand: the id, what it shipped, when it left, whether it was on.
        assert record["old_row_count"] == 8
        assert record["implied_cycle_hours"] == 3, "24/8 is the cycle to re-enter in game"
        first = {20011: 100, 20019: 700}[int(record["destination"])]
        assert sorted(r["dispatch_minute"] for r in record["old_rows"]) == sorted(
            (first + i * 180) % 1440 for i in range(8)
        )
        for row in record["old_rows"]:
            assert row["cargo"] == {"crop": 250}, row
            assert row["active"] is True
            assert row["route_id"] > 0
        # And what was going to take their place, so the record is a plan and not
        # just an epitaph.
        assert record["planned"] == [
            {"cycle_hours": 6, "dispatch_minute": first, "cargo": {"crop": 100}}
        ]

    def test_the_refused_path_leaves_the_whole_sequence(self):
        from travian_api.services.trade_route_service import RouteActionResult

        class _RefusingSvc(_FakeLiveSvc):
            async def create_route(self, route, *, stop_check=None):
                self.created.append(route)
                return RouteActionResult(
                    route.origin_village_id,
                    route.dest_x,
                    route.dest_y,
                    "failed",
                    "route limit reached (test)",
                )

        svc = _RefusingSvc(existing=self._existing())
        res = self._run(svc)

        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        order = [
            kinds.index(k)
            for k in ("replacement_started", "rows_disabled", "create_attempted", "refused")
        ]
        assert order == sorted(order), kinds
        refused = next(e for e in _trace_events(res.trace_path) if e["kind"] == "refused")
        assert refused["detail"] == "route limit reached (test)", refused
        assert refused["origin"] == 20003
        assert refused["status"] == "failed"

    def test_a_disable_that_did_not_land_still_closes_the_first_half(self):
        """`rows_disabled` is written whatever the game answered. Without it a
        refused disable would leave a dangling record over rows that are still
        shipping -- a recovery alarm for a destination that was never emptied."""
        svc = _FakeLiveSvc(existing=self._existing(), disable_status="failed")
        res = self._run(svc)

        events = _trace_events(res.trace_path)
        assert [e["kind"] for e in events].count("replacement_started") == 1
        (row_event,) = [e for e in events if e["kind"] == "rows_disabled"]
        assert row_event["status"] == "failed"
        assert row_event["route_ids"], "the ids it asked to switch off are the record"

    def test_a_create_the_game_made_is_recorded_as_created(self):
        svc = _FakeLiveSvc(existing=self._existing())
        res = self._run(svc)

        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        assert "created" in kinds and "refused" not in kinds, kinds

    def test_a_run_that_stops_before_the_rebuild_closes_the_record(self):
        """A write-ahead record with no ending means "interrupted, recover by
        hand". That reading only works if every ending the run CAN reach is
        written -- including the one where it gives up on purpose."""
        svc = _FakeLiveSvc(existing=self._existing())
        _disable = svc.disable_routes

        async def _exhaust_the_budget_after_disabling(vid, routes, *, stop_check=None):
            result = await _disable(vid, routes, stop_check=stop_check)
            svc.budget_ok = False
            return result

        svc.disable_routes = _exhaust_the_budget_after_disabling
        res = self._run(svc, max_routes_per_run=50)

        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        assert svc.created == [], "no replacement was written"
        assert kinds.count("replacement_started") == 2, kinds
        assert kinds.count("replacement_abandoned") == 2, kinds


class TestTheMarketplaceIsSteadyBeforeAnythingIsDeleted:
    """A read taken a moment after a write can lag it, and this run turns a page
    into verdicts -- and into DELETEs.

    Two things rest on the read-back: the classification ("the game accepted the
    create but no route appeared") and the window trim, which removes rows. A
    page caught mid-write produces a false verdict on the first and destroys the
    run's own work on the second. So before anything destructive the page is read
    once more -- ONCE. Two identical snapshots prove stability, not freshness: a
    page consistently behind agrees with itself, so retrying until two agree buys
    nothing and has no bound. One read separates the case a re-read fixes from
    the one it does not, and the second is reported rather than retried.
    """

    def _windowed_account(self):
        # An hourly route inside a 23:00-07:00 window: 24 rows created, 8 kept,
        # so a trim is what normally follows.
        return _account(
            [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
            {20003: (0, 0), -1: (40, 40)},
            {20003: "03", -1: "A"},
        )

    def _windowed(self, svc):
        return _run_live(
            svc,
            self._windowed_account(),
            max_routes_per_run=50,
            max_game_rows_per_run=0,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

    def test_a_page_that_lagged_the_create_is_settled_by_one_more_read(self):
        class _LaggingPage(_FakeLiveSvc):
            """The first read-back is still the page as it was before the write."""

            def __init__(self, **kw):
                super().__init__(**kw)
                self._lagged = False

            async def confirm_routes(self, vid, *, map_span=None):
                rows = await super().confirm_routes(vid, map_span=map_span)
                if not self._lagged:
                    self._lagged = True
                    return [e for e in rows if e.route_id <= 900000]
                return rows

        svc = _LaggingPage()
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["created"], [a.detail for a in res.actions]
        assert res.actions[0].observed_game_rows == 4, "classified from the LATER read"
        assert len(svc.confirmed) == 2, f"exactly one extra read, not a retry loop: {svc.confirmed}"

    def test_expectations_met_and_no_trim_costs_no_extra_read(self):
        svc = _FakeLiveSvc()
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["created"]
        assert svc.confirmed == [20003], f"a settled page is read once: {svc.confirmed}"

    def test_a_trim_always_costs_the_stabilising_read_first(self):
        svc = _FakeLiveSvc()
        res = self._windowed(svc)

        assert svc.deleted, "the out-of-window rows are still trimmed"
        # Read-back, stabilising read, then the delete's own confirming read.
        assert svc.confirmed == [20003, 20003, 20003], svc.confirmed
        assert res.problems == [], res.problems

    def test_a_page_that_never_settles_is_never_deleted_from(self):
        import dataclasses

        class _NeverSettles(_FakeLiveSvc):
            """Every read of this marketplace differs from the last."""

            def __init__(self, **kw):
                super().__init__(**kw)
                self._reads = 0

            async def confirm_routes(self, vid, *, map_span=None):
                rows = await super().confirm_routes(vid, map_span=map_span)
                self._reads += 1
                if self._reads % 2 == 0 and rows:
                    rows = [dataclasses.replace(rows[0], active=not rows[0].active)] + rows[1:]
                return rows

        svc = _NeverSettles()
        res = self._windowed(svc)

        assert svc.deleted == [], "a delete on an unstable page can remove this run's own rows"
        assert any("did not agree" in p for p in res.problems), res.problems
        assert any("depart outside the profile hours" in p for p in res.problems), res.problems

    def test_the_disagreement_is_in_the_trace(self):
        import dataclasses

        class _NeverSettles(_FakeLiveSvc):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._reads = 0

            async def confirm_routes(self, vid, *, map_span=None):
                rows = await super().confirm_routes(vid, map_span=map_span)
                self._reads += 1
                if self._reads % 2 == 0 and rows:
                    rows = [dataclasses.replace(rows[0], active=not rows[0].active)] + rows[1:]
                return rows

        svc = _NeverSettles()
        res = self._windowed(svc)

        events = _trace_events(res.trace_path)
        assert [e for e in events if e["kind"] == "read_back_disagreed"], [
            e["kind"] for e in events
        ]

    def test_an_unstable_read_back_makes_the_run_need_attention(self, tmp_path):
        """`read_back_disagreed` is folded into the run summary directly, not
        left to the problems count: an origin the run refused to trim is
        unfinished work whatever else the run reported."""
        from travian_api.services.distribution import run_history

        path = tmp_path / "exec-abc.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(e)
                for e in (
                    {"kind": "run_start", "live_enabled": True},
                    {"kind": "read_back_disagreed", "origin": 20003, "reason": "test"},
                    {"kind": "run_end", "problems": 0, "created": 1},
                )
            )
            + "\n",
            encoding="utf-8",
        )

        summary = run_history._summarise_one(path)

        assert summary.needs_attention is True


def _three_destination_account():
    """One origin, three OWN destinations, so all three match by village id."""
    return _account(
        [
            _row_with_cycle(20003, 20011, 6, 100),
            _row_with_cycle(20003, 20019, 6, 700),
            _row_with_cycle(20003, 20021, 6, 400),
        ],
        {20003: (0, 0), 20011: (10, 0), 20019: (20, 0), 20021: (30, 0)},
        {20003: "03", 20011: "11", 20019: "19", 20021: "21"},
    )


class _AnswerDies(_FakeLiveSvc):
    """The game's ANSWER to a create dies for chosen destinations.

    `create_route` maps a reset connection, a session-expiry redirect and a curl
    failure all to `failed`, and none of them is evidence that nothing was
    created -- which is exactly why the read-back is what settles them.
    """

    def __init__(self, dead=(), **kw):
        super().__init__(**kw)
        self._dead = set(dead)

    async def create_route(self, route, *, stop_check=None):
        from travian_api.services.trade_route_service import RouteActionResult

        if route.dest_village_id in self._dead:
            return RouteActionResult(
                route.origin_village_id,
                route.dest_x,
                route.dest_y,
                "failed",
                "connection reset (test)",
            )
        return await super().create_route(route, stop_check=stop_check)


class _UnsettledPage(_AnswerDies):
    """A marketplace whose first read lags the writes and whose second does not.

    The first read is short one row, so the run's own expectation check fails and
    the stabilising read is taken; the second read shows the row, so the two
    snapshots disagree and nothing on this page can be finalised.
    """

    def __init__(self, dead=(), **kw):
        super().__init__(dead=dead, **kw)
        self._reads = 0

    async def confirm_routes(self, vid, *, map_span=None):
        rows = await super().confirm_routes(vid, map_span=map_span)
        self._reads += 1
        if self._reads == 1:
            return rows[:-1]
        return rows


class TestAbsenceIsNeverFinalisedFromAnUnstablePair:
    """A create is only refused when the page that says so held still.

    The stabilising read already stops an unsettled page from being DELETED
    from. It did not stop the same page from producing a VERDICT: with the two
    reads disagreeing, "the later read classifies" still let a create the game
    was slow to show be recorded as refused an instant before it appeared -- and
    a refusal releases its rows back to the budget, drops the destination into
    the failure streak and can stop the whole run.

    Absence measured on a page that would not hold still is not evidence. The
    action becomes `indeterminate`: the row charge stands, the streak ignores it,
    and the NEXT run's inventory settles it -- a run that reads a real
    marketplace and treats an existing row as satisfied. Presence is still
    accepted, because a row that is demonstrably there is not ambiguous.
    """

    def test_an_absent_create_on_an_unstable_page_is_indeterminate(self):
        svc = _UnsettledPage(dead={20019, 20021})
        res = _run_live(
            svc, _three_destination_account(), max_routes_per_run=50, max_game_rows_per_run=0
        )

        by_dest = {a.destination: a for a in res.actions}
        assert by_dest[20011].status == "created", by_dest[20011].detail
        assert by_dest[20019].status == "indeterminate", by_dest[20019].detail
        assert by_dest[20021].status == "indeterminate", by_dest[20021].detail
        assert res.not_created == 0, "an unsettled page never proves a create was refused"

    def test_presence_is_still_accepted_on_an_unstable_page(self):
        """Attribution of a row that is DEMONSTRABLY there is not ambiguous, so
        the create that made it is still promoted."""

        class _LandsAnyway(_UnsettledPage):
            async def create_route(self, route, *, stop_check=None):
                from travian_api.services.trade_route_service import RouteActionResult

                if route.dest_village_id in self._dead:
                    # The rows appear; only the ANSWER died.
                    await _FakeLiveSvc.create_route(self, route)
                    return RouteActionResult(
                        route.origin_village_id,
                        route.dest_x,
                        route.dest_y,
                        "failed",
                        "connection reset (test)",
                    )
                return await _FakeLiveSvc.create_route(self, route, stop_check=stop_check)

        svc = _LandsAnyway(dead={20019})
        res = _run_live(
            svc, _three_destination_account(), max_routes_per_run=50, max_game_rows_per_run=0
        )

        by_dest = {a.destination: a for a in res.actions}
        assert by_dest[20019].status == "created", by_dest[20019].detail

    def test_the_row_charge_of_an_indeterminate_create_is_retained(self):
        """A refusal releases the rows it was charged for; an indeterminate one
        must not, or the next origin spends a budget that may already be in the
        game."""
        account = _account(
            [
                _row_with_cycle(20003, 20011, 6, 100),
                _row_with_cycle(20003, 20019, 6, 700),
                _row_with_cycle(20033, 20021, 6, 400),
            ],
            {20003: (0, 0), 20011: (10, 0), 20019: (20, 0), 20033: (30, 0), 20021: (40, 0)},
            {20003: "03", 20011: "11", 20019: "19", 20033: "33", 20021: "21"},
        )
        svc = _UnsettledPage(dead={20019})
        # Exactly two 6h routes' worth of rows: the create that landed spends
        # four, the indeterminate one holds four, and nothing is left for the
        # second origin.
        res = _run_live(svc, account, max_routes_per_run=50, max_game_rows_per_run=8)

        by_dest = {a.destination: a for a in res.actions}
        assert by_dest[20019].status == "indeterminate", by_dest[20019].detail
        assert by_dest[20021].status == "deferred", (
            "the indeterminate create's rows are still charged against the budget"
        )
        assert {r.dest_village_id for r in svc.created} == {20011}

    def test_an_indeterminate_create_is_not_a_refusal_in_the_streak(self):
        """Two dead answers trip the consecutive-failure stop at create time.
        Settlement cannot confirm either refusal, so the stop it caused is
        lifted -- an indeterminate is not a refusal in the ledger."""
        svc = _UnsettledPage(dead={20019, 20021})
        res = _run_live(
            svc, _three_destination_account(), max_routes_per_run=50, max_game_rows_per_run=0
        )

        assert not any("in a row" in p for p in res.problems), res.problems
        assert not any("stopped early" in p for p in res.problems), res.problems

    def test_both_snapshots_are_in_the_trace(self):
        svc = _UnsettledPage(dead={20019})
        res = _run_live(
            svc, _three_destination_account(), max_routes_per_run=50, max_game_rows_per_run=0
        )

        disagreed = [e for e in _trace_events(res.trace_path) if e["kind"] == "read_back_disagreed"]
        assert disagreed, [e["kind"] for e in _trace_events(res.trace_path)]
        # Two 6h creates land eight rows; the first read is short exactly one.
        assert len(disagreed[0]["first_rows"]) == 7, disagreed[0]
        assert len(disagreed[0]["second_rows"]) == 8, disagreed[0]
        assert "route_id" in disagreed[0]["first_rows"][0], disagreed[0]

    def test_the_indeterminate_destination_is_named_in_problems(self):
        svc = _UnsettledPage(dead={20019})
        res = _run_live(
            svc, _three_destination_account(), max_routes_per_run=50, max_game_rows_per_run=0
        )

        assert any("19" in p and "could not be settled" in p for p in res.problems), res.problems


def _four_origin_account():
    """Four origins in insertion order [20003, 20011, 20019, 20021].

    The third carries TWO routes, so one of them can land while the other's
    answer dies -- which is what an unsettled page needs in order to produce an
    indeterminate rather than a plain absence.
    """
    return _account(
        [
            _row_with_cycle(20003, 20051, 6, 100),
            _row_with_cycle(20011, 20052, 6, 200),
            _row_with_cycle(20019, 20053, 6, 300),
            _row_with_cycle(20019, 20054, 6, 400),
            _row_with_cycle(20021, 20055, 6, 500),
        ],
        {
            20003: (0, 0),
            20011: (10, 0),
            20019: (20, 0),
            20021: (30, 0),
            20051: (0, 10),
            20052: (10, 10),
            20053: (20, 10),
            20054: (20, 20),
            20055: (30, 10),
        },
        {
            20003: "03",
            20011: "11",
            20019: "19",
            20021: "21",
            20051: "51",
            20052: "52",
            20053: "53",
            20054: "54",
            20055: "55",
        },
    )


class _UnsettledAtOneOrigin(_AnswerDies):
    """Exactly ONE marketplace lags its writes; every other page is steady.

    `_UnsettledPage` makes the FIRST read of the run short, whichever village it
    belongs to. The circuit breaker is about what happens to the villages AFTER
    the unreliable one, so the instability has to be aimed.
    """

    def __init__(self, unstable, **kw):
        super().__init__(**kw)
        self._unstable = unstable
        self._reads_here = 0

    async def confirm_routes(self, vid, *, map_span=None):
        rows = await super().confirm_routes(vid, map_span=map_span)
        if vid != self._unstable:
            return rows
        self._reads_here += 1
        if self._reads_here == 1:
            return rows[:-1]
        return rows


class TestAnUnreliableMarketplaceStopsTheRunWritingToOtherVillages:
    """`indeterminate` is not a refusal, so the streak rightly ignores it -- and
    that left the run free to carry on WRITING to every remaining village after
    the marketplace had demonstrably stopped telling the truth about its state.

    Two reads disagreeing is not a fact about one destination. It is a fact
    about the boundary this whole run reads and writes through: every verdict
    after it -- what exists, what landed, what may be deleted -- rests on a page
    that would not hold still. Creating more routes on top of that is writing
    blind, and each one is a row a later run has to reconcile.

    So an indeterminate settlement is a circuit breaker: the current village is
    finished (its own restore is guarded separately), and no further village is
    written to. Independent of the refusal streak, which counts something else.
    """

    def _run(self, svc):
        return _run_live(
            svc, _four_origin_account(), max_routes_per_run=50, max_game_rows_per_run=0
        )

    def test_the_village_after_an_indeterminate_one_is_never_visited(self):
        svc = _UnsettledAtOneOrigin(20019, dead={20054})
        res = self._run(svc)

        by_dest = {a.destination: a for a in res.actions}
        assert by_dest[20054].status == "indeterminate", by_dest[20054].detail
        assert 20021 not in svc.listed, (
            "the marketplace after an unsettled one must not even be read"
        )
        assert 20055 not in {r.dest_village_id for r in svc.created}
        assert by_dest[20055].status == "deferred", by_dest[20055].detail

    def test_the_response_says_why_it_stopped(self):
        svc = _UnsettledAtOneOrigin(20019, dead={20054})
        res = self._run(svc)

        assert any(
            "marketplace reads disagreed at 19" in p and "no further villages were written to" in p
            for p in res.problems
        ), res.problems

    def test_a_steady_account_visits_every_village(self):
        """The control: with no indeterminate settlement nothing is stopped."""
        svc = _FakeLiveSvc()
        res = self._run(svc)

        assert svc.listed == [20003, 20011, 20019, 20021], svc.listed
        assert not any("marketplace reads disagreed" in p for p in res.problems), res.problems
        assert [a.status for a in res.actions] == ["created"] * 5, [a.status for a in res.actions]

    def test_a_run_of_plain_refusals_still_stops_by_the_streak(self):
        """The breaker is independent of the refusal ledger and does not replace
        it: refusals on a page that reads back consistently still trip the
        consecutive-failure stop, in the streak's own words."""
        svc = _AnswerDies(dead={20051, 20052, 20053, 20054, 20055})
        res = self._run(svc)

        assert any("in a row" in p for p in res.problems), res.problems
        assert not any("marketplace reads disagreed" in p for p in res.problems), res.problems


class TestThePlanCannotEmitTwoRoutesTheReadBackCannotTellApart:
    """The invariant the whole attribution rests on, stated and checked.

    The read-back settles a create per DESTINATION, against the complete
    expected pre-trim multiset of (departure minute, cargo). Two routes to one
    destination therefore no longer have to be separable row by row: a pair that
    lands in full is settled by the multiset matching, a pair one of whose
    creates produced nothing is settled by elimination when only one of them
    wants exactly the missing rows, and anything else is reported
    `indeterminate` and left untrimmed rather than split arbitrarily.

    What remains unsafe is the pair even that has nothing to work with:
    identical cargo on an IDENTICAL full pre-trim fan-out, with different
    windows that overlap. Both creates then want the same rows at the same
    minutes with the same amounts, and the trim is asked to keep rows for both
    at a minute both windows claim. Nothing the planner emits today is such a
    pair -- an assumption until something checks it, whose failure mode is rows
    vanishing from a real account.

    Two exclusions, both forced by plans this app already ships, and both pinned
    below so they cannot be widened by accident.
    """

    def _pair(self, first_minute, second_minute, *, second_cargo=None, dest=20011):
        return _account(
            [
                SheetRow(
                    origin=20003,
                    destination=20011,
                    cargo={Resource.CROP: 100},
                    cycle_hours=6,
                    dispatch_minute=first_minute,
                    arrival_minute=0,
                    merchants=2,
                ),
                SheetRow(
                    origin=20003,
                    destination=dest,
                    cargo=second_cargo or {Resource.CROP: 100},
                    cycle_hours=6,
                    dispatch_minute=second_minute,
                    arrival_minute=0,
                    merchants=2,
                ),
            ],
            {20003: (0, 0), 20011: (10, 0), 20019: (20, 0)},
            {20003: "03", 20011: "11", 20019: "19"},
        )

    def test_an_ambiguous_pair_is_refused_and_both_routes_are_named(self):
        # A 6h cycle from minute 100 fans out to {100, 460, 820, 1180}; one from
        # 460 fans out to the very same four. Same destination, same cargo: the
        # matcher has nothing left to tell them apart.
        svc = _FakeLiveSvc()
        with pytest.raises(HTTPException) as caught:
            _run_live(svc, self._pair(100, 460), max_routes_per_run=50)

        detail = str(caught.value.detail)
        assert caught.value.status_code == 500
        assert "100" in detail and "460" in detail, detail
        assert "identical cargo" in detail, detail
        assert svc.created == [], "nothing may be attempted against a plan like this"

    def test_the_same_pair_on_disjoint_minutes_is_fine(self):
        # {100, 460, 820, 1180} against {130, 490, 850, 1210}: no shared minute,
        # so every row belongs to exactly one route.
        svc = _FakeLiveSvc()
        res = _run_live(svc, self._pair(100, 130), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["created", "created"]

    def test_overlapping_minutes_with_different_cargo_are_not_ambiguous(self):
        # The cargo is the tie-break the matcher uses at a shared minute, so a
        # pair that differs in it is not ambiguous and must not be refused.
        # What happens to the second route is a different mechanism: the
        # reconciler's own within-visit minute claim skips it, because the first
        # create already put a row at every minute it wanted. This asserts only
        # that the invariant did not fire.
        svc = _FakeLiveSvc()
        res = _run_live(
            svc,
            self._pair(100, 460, second_cargo={Resource.CROP: 250}),
            max_routes_per_run=50,
        )

        assert [a.status for a in res.actions] == ["created", "skipped"]

    def test_the_same_schedule_to_a_different_destination_is_fine(self):
        svc = _FakeLiveSvc()
        res = _run_live(svc, self._pair(100, 100, dest=20019), max_routes_per_run=50)

        assert [a.status for a in res.actions] == ["created", "created"]

    def test_a_dry_run_refuses_it_too(self):
        """The preview is where an operator would see this, and a preview that
        passes over a plan the live run refuses is worse than either answer."""

        async def _plan(_body):
            return self._pair(100, 460)

        with (
            _patch(dist_module, "_plan_account", _plan),
            pytest.raises(HTTPException) as caught,
        ):
            _execute(_exec_body(dry_run=True, max_routes_per_run=50), connected=False)

        assert caught.value.status_code == 500

    def test_the_invariant_reads_the_same_minutes_the_matcher_claims_by(self):
        """Both go through `_fanout_minutes`, so they cannot drift apart."""
        route = SimpleNamespace(dispatch_minute=100, cycle_hours=6)

        assert dist_module._fanout_minutes(route) == {100, 460, 820, 1180}

    def _route(self, *, cycle=6, minute=100, cargo=None, window=None, dest=20011):
        return SimpleNamespace(
            origin_village_id=20003,
            dest_village_id=dest,
            dest_x=10,
            dest_y=0,
            dest_name=str(dest),
            cargo=cargo or {Resource.CROP: 100},
            cycle_hours=cycle,
            dispatch_minute=minute,
            window=window,
            segment="",
        )

    def test_two_cycles_that_merely_overlap_are_settled_by_the_destination(self):
        """6h from 100 is {100, 460, 820, 1180} and 3h from 100 contains all
        four, so a per-ROW matcher cannot separate them -- which is why this pair
        used to be refused. The destination's own multiset can: the two expect
        different rows, so whichever of them produced nothing is exactly the
        shortfall and is settled by elimination. Refusing it would refuse a plan
        the settlement handles."""
        pair = [self._route(cycle=6, minute=100), self._route(cycle=3, minute=100)]

        assert dist_module._matcher_ambiguity(pair) is None

    def test_an_identical_fanout_under_overlapping_windows_is_refused(self):
        """The one pair nothing can separate: same cargo, the same 24 departure
        minutes, and windows that both claim some of them."""
        pair = [
            self._route(cycle=1, minute=420, window=(0, 1440)),
            self._route(cycle=1, minute=480, window=(420, 1380)),
        ]

        assert dist_module._matcher_ambiguity(pair) is not None

    def test_two_identical_rows_are_the_same_route_not_an_ambiguous_pair(self):
        """Two foreign targets on one tile produce two identical plan rows. They
        are not two routes the matcher must separate -- the reconciler creates
        the route once, and splitting a claim between two identical routes
        misattributes nothing. Excluded deliberately, and pinned so the
        exclusion is not quietly widened."""
        pair = [self._route(), self._route()]

        assert dist_module._matcher_ambiguity(pair) is None

    def test_a_whole_day_pair_trimmed_to_its_own_hours_is_not_ambiguous(self):
        """The other forced exclusion. A whole-day plan sends one destination
        once per profile, and two hourly routes both fan out across every minute
        of the day BEFORE the trim -- so measured on the raw fan-out, every
        shared destination of every whole-day plan would be refused. What LANDS
        is disjoint, because each route is trimmed to its own profile's hours,
        and the pooled trim works on exactly these surviving minutes."""
        pair = [
            self._route(cycle=1, minute=420, window=(420, 1380)),
            self._route(cycle=1, minute=480, window=(1380, 420)),
        ]

        assert dist_module._matcher_ambiguity(pair) is None


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

    @pytest.mark.parametrize(
        ("model", "kwargs"),
        [
            ("ForeignTarget", {"name": "A", "x": 1, "y": 2, "crop_per_hour": 100}),
            ("VillageConfig", {"village_id": 20003}),
            ("AllocationInput", {"mode": "keep", "value": 0}),
            ("RoleTemplate", {}),
            (
                "DaySegmentInput",
                {"name": "Day", "window": [420, 1380], "allocations": {}},
            ),
        ],
    )
    def test_a_nested_model_refuses_an_unknown_key_too(self, model, kwargs):
        # `extra: "forbid"` reached only the top level, so the guarantee it
        # claims -- "a 422 is the only safe answer to a parameter this endpoint
        # does not understand" -- stopped one level down. Measured: an
        # `exclude_origins_text` key planted on a foreign target inside an
        # otherwise-valid /execute body was accepted and dropped.
        from pydantic import ValidationError

        from travian_api.web.routes import distribution as _dist

        cls = getattr(_dist, model)
        cls.model_validate(kwargs)  # the control: the known keys still validate
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            cls.model_validate({**kwargs, "exclude_origins_text": "20002"})


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
        _body = dict(
            dry_run=False,
            disable_existing=False,
            max_routes_per_run=50,
            max_game_rows_per_run=0,
        )
        _execute(_exec_body(**_body), svc=svc)
        svc.created.clear()
        res = _execute(_exec_body(**_body), svc=svc)

        assert svc.created == []
        import json
        from pathlib import Path

        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]
        assert [e for e in events if e["kind"] == "verified"] == []


class TestACreateWhoseAnswerDiedIsSettledByTheMarketplace:
    """`failed` from a create is not evidence that nothing was created.

    `create_route` maps `NetworkError` to `failed`, and that is what a
    session-expiry redirect on a non-retryable write, a connection reset or a
    curl failure produce -- cases whose own comment says "the original may
    already have taken effect". The read-back gate required `created_here`,
    which a failed create never joins, so a run that had put thirty rows in the
    game reported `created=0` and told the operator the game had refused the
    write. Every other write in this path is settled by looking at the page;
    this one asserted a refusal instead.
    """

    def _run(self, svc, **kw):
        return _run_live(
            svc, _own_village_account(), disable_existing=False, max_routes_per_run=50, **kw
        )

    def test_a_landed_create_is_reported_as_created(self):
        # The double fans the rows out and THEN answers `failed`: the write
        # landed, the answer did not survive.
        svc = _FakeLiveSvc(create_status="failed")
        res = self._run(svc)

        assert [a.status for a in res.actions] == ["created"], [a.detail for a in res.actions]
        assert res.created == 1
        assert res.remaining == 0

    def test_the_rows_it_made_are_counted(self):
        svc = _FakeLiveSvc(create_status="failed")
        res = self._run(svc)

        # A 6h cycle is four daily rows, and they are on the page.
        assert res.created_game_rows == 4

    def test_a_create_that_really_was_refused_stays_failed(self):
        # The control. Nothing appeared on the marketplace, so `failed` is the
        # truth and must survive the read-back.
        svc = _FakeLiveSvc(create_status="failed", phantom_creates=True)
        res = self._run(svc)

        assert [a.status for a in res.actions] == ["failed"]
        assert res.created == 0
        assert res.remaining == 1

    def test_the_marketplace_is_actually_re_read(self):
        svc = _FakeLiveSvc(create_status="failed")
        self._run(svc)

        assert svc.confirmed == [20003], "the create's fate is settled by re-reading the page"

    def test_a_run_whose_creates_all_landed_is_not_reported_as_refused(self):
        # Two dead answers in a row trip _CONSECUTIVE_FAILURE_LIMIT, which
        # stops the run BEFORE the read-back settles either of them. The
        # read-back then promotes both to `created` -- and the verdict that
        # stopped the run stood, so the response said "the game refused 2
        # create(s) in a row" over two routes it had just reported creating.
        svc = _FakeLiveSvc(create_status="failed")
        res = _run_live(
            svc, _two_destination_account(), disable_existing=False, max_routes_per_run=50
        )

        assert res.created == 2, [a.status for a in res.actions]
        assert res.problems == [], res.problems

    def test_the_stop_it_caused_is_lifted_so_the_next_origin_still_runs(self):
        # The material cost: `stopped_early` skips every remaining origin, so a
        # flaky connection capped an account-wide run at two creates and
        # deferred the rest -- exactly the case the read-back exists for.
        svc = _FakeLiveSvc(create_status="failed")
        res = _run_live(
            svc, _four_route_two_origin_account(), disable_existing=False, max_routes_per_run=50
        )

        assert {r.origin_village_id for r in svc.created} == {20003, 20011}, (
            "the second origin was skipped by a stop the read-back withdrew"
        )
        assert res.created == 4
        assert res.remaining == 0

    def test_a_genuine_refusal_still_stops_the_run(self):
        # The control: nothing landed, so the verdict is true and must stand.
        svc = _FakeLiveSvc(create_status="failed", phantom_creates=True)
        res = _run_live(
            svc, _four_route_two_origin_account(), disable_existing=False, max_routes_per_run=50
        )

        assert res.created == 0
        assert any("refused 2 create(s) in a row" in p for p in res.problems), res.problems
        assert any("stopped early" in p for p in res.problems), res.problems

    def test_a_landed_creates_fan_out_is_still_pruned_to_the_window(self):
        # The rows exist, so they depart round the clock unless the trim runs --
        # the trim keyed off `created_here` alone, so an unanswered create left
        # its whole fan-out shipping outside the profile.
        svc = _FakeLiveSvc(create_status="failed")
        _run_live(
            svc,
            _account(
                [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
                {20003: (0, 0), -1: (40, 40)},
                {20003: "03", -1: "A"},
            ),
            max_routes_per_run=50,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

        assert svc.deleted, "the out-of-window rows of a landed create must be pruned too"


class _ScriptedCreateSvc(_FakeLiveSvc):
    """A double whose per-create outcome follows a script, so ONE run can mix a
    dead answer whose rows landed with creates the game really refused.

    Each script entry is ``(answer, landed)``: the status ``create_route``
    returns, and whether the rows appear on the marketplace regardless.
    """

    def __init__(self, script, **kw):
        super().__init__(**kw)
        self._script = list(script)

    async def create_route(self, route, *, stop_check=None):
        answer, landed = self._script.pop(0)
        self._create_status = answer
        self._phantom = not landed
        return await super().create_route(route, stop_check=stop_check)


class TestTheFailureStreakIsRecomputedFromAnOrderedLedger:
    """A promoted dead answer may only lift the stop it actually caused.

    The streak used to be a bare counter that a settlement DECREMENTED, so a
    promotion anywhere in the run cancelled a refusal anywhere else. Outcomes
    [dead answer that landed, success, refusal, refusal] trip the stop on the
    two GENUINE refusals at the end; the decrement made 2 - 1 = 1 and lifted
    both the stop and the problem line over two routes that do not exist.
    A trailing streak recomputed from an ordered ledger keeps them.
    """

    def _four_route_one_origin_account(self):
        return _account(
            [
                _row(20003, -1, 40, 40, 100),
                _row(20003, -2, 50, 50, 300),
                _row(20003, -3, 60, 60, 500),
                _row(20003, -4, 70, 70, 700),
            ],
            {20003: (0, 0), -1: (40, 40), -2: (50, 50), -3: (60, 60), -4: (70, 70)},
            {20003: "03", -1: "A", -2: "B", -3: "C", -4: "D"},
        )

    def test_a_promotion_before_a_success_does_not_lift_a_later_pair(self):
        # A: the answer died but the rows landed. Then a clean create. Then two
        # creates the game really refused -- the pair that trips the limit.
        svc = _ScriptedCreateSvc(
            [("failed", True), ("created", True), ("failed", False), ("failed", False)]
        )
        res = _run_live(
            svc,
            self._four_route_one_origin_account(),
            disable_existing=False,
            max_routes_per_run=50,
        )

        assert [a.status for a in res.actions] == ["created", "created", "failed", "failed"], [
            a.detail for a in res.actions
        ]
        assert any("refused 2 create(s) in a row" in p for p in res.problems), res.problems
        assert any("stopped early" in p for p in res.problems), res.problems

    def test_a_refusal_ending_one_origin_and_one_starting_the_next_still_count_two(self):
        # The streak is RUN-wide: the ledger spans origins, so a single refusal
        # at the end of one origin and one at the start of the next is still two
        # in a row.
        svc = _ScriptedCreateSvc(
            [("created", True), ("failed", False), ("failed", False), ("created", True)]
        )
        res = _run_live(
            svc,
            _four_route_two_origin_account(),
            disable_existing=False,
            max_routes_per_run=50,
        )

        assert any("refused 2 create(s) in a row" in p for p in res.problems), res.problems
        assert len(svc.created) == 3, "the fourth create must never fire"


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


class TestAnUnverifiableCargoUpdateIsSettledByLookingAtTheMarketplace:
    """The bulk PUT answers with an empty body on this account, so `unverified`
    is the NORMAL outcome of a cargo correction, not an exception.

    Settled the way an unverified disable is: the origin is re-read and the
    verdict comes from what the page carries, not from a response shape nobody
    has observed. Reported as corrected only when the rows show the plan's
    amounts.
    """

    def _existing(self, crop):
        from travian_api.services.distribution.allocation import Resource

        return {20003: _fanned(20011, start_id=800, cargo={Resource.CROP: crop})}

    def _svc(self, *, applies):
        from travian_api.services.trade_route_service import RouteActionResult

        class _Unreadable(_FakeLiveSvc):
            async def update_cargo(
                self, vid, routes, cargo, *, dest_x=None, dest_y=None, stop_check=None
            ):
                if not routes:
                    return None
                self.updated.append((vid, tuple(sorted(r.route_id for r in routes)), dict(cargo)))
                if applies:
                    targets = {r.route_id for r in routes}
                    for row in self._existing.get(vid, []):
                        if row.route_id in targets:
                            row.cargo = dict(cargo)
                return RouteActionResult(vid, 0, 0, "unverified", "cannot be confirmed (test)")

        return _Unreadable(existing=self._existing(9000))

    def test_rows_the_game_really_rewrote_are_counted_as_corrected(self):
        svc = self._svc(applies=True)
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50, update_drifted=True)

        assert len(res.updates) == 1, res.problems
        assert "cargo reset on 4 row(s)" in res.updates[0]
        assert [a.status for a in res.actions] == ["updated"]
        assert res.problems == []

    def test_rows_still_carrying_the_old_amounts_are_reported(self):
        svc = self._svc(applies=False)
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50, update_drifted=True)

        assert res.updates == []
        assert [a.status for a in res.actions] == ["skipped"]
        joined = " ".join(res.problems)
        assert "still shows the old amounts" in joined, res.problems

    def test_an_update_only_run_still_reads_back(self):
        svc = self._svc(applies=True)
        _run_live(svc, _own_village_account(), max_routes_per_run=50, update_drifted=True)

        assert svc.confirmed == [20003], "the page is what settles an unreadable answer"


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
        # The same account WITH the sweep, so the empty lists above are a
        # decision and not a field that is always empty. Two assertions that can
        # only ever read `[]` prove nothing about the one that matters.
        swept = _run_live(
            _FakeLiveSvc(existing={20011: [ExistingRoute(7, _ORPHAN_DEST, 60, 60, active=True)]}),
            _one_origin_account(),
            disable_existing=True,
            max_routes_per_run=50,
            reconcile_all_origins=True,
        )
        assert swept.swept_origins == [20003, 20011]


class TestALiveRunWithNoTraceRefusesToStart:
    """No trace means no undo, and the trace can fail to open in silence.

    `ExecutionTrace.__init__` catches `OSError`, logs a warning and carries on
    with tracing off -- correct for observability, wrong for THIS run: the trace
    is the only record of what a live execution put in a real account, because
    the game returns no id when it creates a route. Without it `/revert-plan`
    has nothing to compare against, and its own 500 handler points the operator
    at a trace_id that will 404.
    """

    def _blocked_trace_dir(self, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        return blocker / "traces"

    def test_a_live_run_refuses_when_the_trace_cannot_be_written(self, tmp_path):
        svc = _FakeLiveSvc()
        with (
            _patch(dist_module.execution_trace, "TRACE_DIR", self._blocked_trace_dir(tmp_path)),
            pytest.raises(HTTPException) as caught,
        ):
            _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert caught.value.status_code == 500
        assert "trace" in caught.value.detail.lower()

    def test_nothing_is_written_to_the_game(self, tmp_path):
        svc = _FakeLiveSvc()
        with (
            _patch(dist_module.execution_trace, "TRACE_DIR", self._blocked_trace_dir(tmp_path)),
            contextlib.suppress(HTTPException),
        ):
            _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert svc.created == [] and svc.listed == [], (
            "the refusal must come before the first game request"
        )

    def test_a_dry_run_is_unaffected(self, tmp_path):
        # A preview writes nothing to the account, so there is nothing to undo
        # and no reason to refuse it.
        with _patch(dist_module.execution_trace, "TRACE_DIR", self._blocked_trace_dir(tmp_path)):
            res = _execute(_exec_body(dry_run=True), svc=_dry_svc())

        assert res.dry_run is True
        assert res.actions


class TestASweptAccountWithDeferredCreatesGetsAnotherPass:
    """The frontend's sweep loop documents an UNFILTERED pass, and it could not
    happen.

    Once every village is swept but whole-day creates are still deferred by the
    per-chunk budget, the loop goes back without `only_origins` so those creates
    get their turn. But it breaks on `!wait`, and the wait was `None` whenever
    `unswept_origins` was empty -- so the pass the comment describes was
    unreachable, and "swept" quietly meant "swept but only partly provisioned".
    """

    def _two_routes(self):
        return _account(
            [_row(20003, -1, 40, 40, 100), _row(20003, -2, 50, 50, 700)],
            {20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", 20011: "11", -1: "A", -2: "B"},
        )

    def test_a_finished_sweep_still_holding_creates_asks_the_caller_back(self):
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            self._two_routes(),
            max_routes_per_run=1,
            max_game_rows_per_run=0,
            reconcile_all_origins=True,
        )

        assert res.unswept_origins == [], "every village was visited"
        assert res.remaining >= 1, "and a create is still owed"
        assert res.next_chunk_wait_seconds is not None

    def test_a_reconcile_only_sweep_is_still_finished(self):
        # `max_routes_per_run=0` defers every route by construction and creates
        # nothing ever, so asking the caller back would be an endless loop over
        # a run that cannot make progress.
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            self._two_routes(),
            max_routes_per_run=0,
            reconcile_all_origins=True,
        )

        assert res.unswept_origins == []
        assert res.next_chunk_wait_seconds is None

    def test_a_sweep_that_can_never_fund_the_rest_stops_asking(self):
        # `deferred` is not progress. A route whose surviving fan-out exceeds
        # the WHOLE row budget is deferred by every run alike, so the sweep
        # asked the caller back for ever: measured over five passes, created=0
        # from pass 3 on and a wait every time. The shipped UI survives it by
        # breaking after two identical `remaining` values; any other caller
        # re-reads every village indefinitely.
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            self._two_routes(),
            max_routes_per_run=50,
            max_game_rows_per_run=3,  # a 6h route is 4 rows: neither ever fits
            reconcile_all_origins=True,
        )

        assert svc.created == [], "3 rows cannot hold a 4-row route"
        assert res.remaining == 2
        assert res.unswept_origins == []
        assert res.next_chunk_wait_seconds is None, (
            "another pass would defer exactly the same routes"
        )
        assert any("raise it to at least 4" in p for p in res.problems), res.problems

    def test_a_sweep_with_nothing_left_asks_for_nothing(self):
        svc = _FakeLiveSvc(existing={20011: []})
        res = _run_live(
            svc,
            self._two_routes(),
            max_routes_per_run=50,
            max_game_rows_per_run=0,
            reconcile_all_origins=True,
        )

        assert (res.unswept_origins, res.remaining) == ([], 0)
        assert res.next_chunk_wait_seconds is None


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
            # Bounds read from the source rather than copied. They were hardcoded
            # as 45-240 for the old flat-uniform draw, and when that became a
            # heavy-tailed draw with a 360s cap this test began failing roughly
            # whenever one of its six samples landed in the new tail -- an
            # intermittent red that a single green run after the change did not
            # reveal. A test that names the constants cannot drift out of step
            # with them again.
            assert res.next_chunk_wait_seconds >= dist_module._CHUNK_GAP_FLOOR_S, (
                f"a {res.next_chunk_wait_seconds}s gap is below the "
                f"{dist_module._CHUNK_GAP_FLOOR_S}s floor -- short enough to read as polling"
            )
            assert res.next_chunk_wait_seconds <= dist_module._CHUNK_GAP_CAP_S, (
                f"a {res.next_chunk_wait_seconds}s gap exceeds the "
                f"{dist_module._CHUNK_GAP_CAP_S}s cap -- long enough to read as a hung sweep"
            )
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

    def test_a_dead_answer_holds_its_charge_until_the_read_back_settles_it(self):
        # The refund fired on the `failed` ANSWER and the re-charge only at the
        # end-of-origin read-back, so every create in between spent rows that
        # were already in the game. Five 4h routes are six rows each: at a
        # 24-row budget the run created all five and left THIRTY.
        account = _account(
            [_row_with_cycle(20003, -(i + 1), 4, 100 + i * 10) for i in range(5)],
            {20003: (0, 0), **{-(i + 1): (40 + i, 40) for i in range(5)}},
            {20003: "03", **{-(i + 1): f"A{i}" for i in range(5)}},
        )

        class _FirstAnswerDies(_FakeLiveSvc):
            async def create_route(self, route, *, stop_check=None):
                result = await super().create_route(route, stop_check=stop_check)
                if len(self.created) == 1:
                    return SimpleNamespace(status="failed", detail="answer lost (test)")
                return result

        svc = _FirstAnswerDies()
        _run_live(svc, account, max_routes_per_run=50, max_game_rows_per_run=24)

        rows = sum(len(v) for v in svc._existing.values())
        assert rows <= 24, f"the run left {rows} rows against a budget of 24"

    def test_a_create_that_really_was_refused_never_overspends_either(self):
        # The other half. The read-back finds nothing, so the charge held while
        # the answer was unknown is released -- at the read-back, not when the
        # answer arrived, so nothing fired in between could spend it. The cost
        # is one route deferred to a later run, which is the only direction
        # that cannot leave more rows than the operator agreed to.
        account = _account(
            [_row_with_cycle(20003, -(i + 1), 4, 100 + i * 10) for i in range(5)],
            {20003: (0, 0), **{-(i + 1): (40 + i, 40) for i in range(5)}},
            {20003: "03", **{-(i + 1): f"A{i}" for i in range(5)}},
        )

        class _FirstIsRefused(_FakeLiveSvc):
            async def create_route(self, route, *, stop_check=None):
                if not self.created:
                    self.created.append(route)
                    return SimpleNamespace(status="failed", detail="refused (test)")
                return await super().create_route(route, stop_check=stop_check)

        svc = _FirstIsRefused()
        res = _run_live(svc, account, max_routes_per_run=50, max_game_rows_per_run=24)

        assert sum(len(v) for v in svc._existing.values()) == 18, (
            "three 6-row routes, plus six rows held for a create the page never showed"
        )
        assert res.remaining >= 1, "the route the held charge deferred is reported, not lost"

    def test_a_landed_dead_answer_is_charged_the_same_rows_it_refunded(self):
        # The create path charges and refunds the POST-PRUNE surviving count; the
        # read-back that rehabilitates a landed create re-charged the PRE-PRUNE
        # fan-out. One hourly night route is 24 rows fanned out and 8 after the
        # trim, so a dropped answer spent 24 of a 16-row budget on a route
        # occupying 8 -- and deferred a whole second origin, silently.
        account = _account(
            [
                _row_with_cycle(20003, -1, 1, 23 * 60),
                _row_with_cycle(20011, -2, 1, 23 * 60),
            ],
            {20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", 20011: "11", -1: "A", -2: "B"},
        )

        class _FirstAnswerDies(_FakeLiveSvc):
            async def create_route(self, route, *, stop_check=None):
                result = await super().create_route(route, stop_check=stop_check)
                if len(self.created) == 1:
                    return SimpleNamespace(status="failed", detail="answer lost (test)")
                return result

        svc = _FirstAnswerDies()
        res = _run_live(
            svc,
            account,
            max_routes_per_run=50,
            max_game_rows_per_run=16,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

        assert {r.origin_village_id for r in svc.created} == {20003, 20011}, (
            "8 surviving rows each fit exactly in 16; the second origin was "
            "deferred against rows the trim had already removed"
        )
        assert res.created == 2
        assert res.live_game_rows == 16


class TestTheRowFootprintIsReportedAfterTheTrim:
    """`created_game_rows` measures a state the same run destroys.

    The rows are counted from the read-back that follows the creates, and the
    window prune runs AFTER that -- so the run reported the transient fan-out
    (42 rows on the measured whole-day case) while the marketplace was left
    holding 16. The number is not comparable to `max_game_rows_per_run`, which
    is charged in surviving rows, so the budget did not look like a bound.

    Both are now reported and both are labelled: what the creates made, and what
    this run left live.
    """

    def _account(self):
        return _account(
            [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
            {20003: (0, 0), -1: (40, 40)},
            {20003: "03", -1: "A"},
        )

    def _run(self, svc, **kw):
        return _run_live(
            svc,
            self._account(),
            max_routes_per_run=50,
            max_game_rows_per_run=0,
            dispatch_window=[23 * 60, 7 * 60],
            **kw,
        )

    def test_the_created_count_is_still_what_the_creates_made(self):
        res = self._run(_FakeLiveSvc(), prune_to_window=True)
        assert res.created_game_rows == 24

    def test_the_live_count_is_what_survived_the_trim(self):
        res = self._run(_FakeLiveSvc(), prune_to_window=True)
        assert res.live_game_rows == 8, "eight of the 24 departures fall inside 23:00-07:00"

    def test_a_prune_that_did_not_happen_reports_the_whole_footprint(self):
        # The cell that mattered: a failed prune left 24 rows shipping and the
        # run reported the same 24 it reports when the prune worked.
        res = self._run(_FakeLiveSvc(delete_status="failed"), prune_to_window=True)

        assert res.created_game_rows == 24
        assert res.live_game_rows == 24, "nothing was removed, so nothing is discounted"
        assert res.problems

    def test_without_a_prune_the_two_counts_agree(self):
        res = self._run(_FakeLiveSvc())
        assert (res.created_game_rows, res.live_game_rows) == (24, 24)

    def test_a_dry_run_forecasts_the_surviving_footprint(self):
        async def _plan(_body):
            return self._account()

        with _patch(dist_module, "_plan_account", _plan):
            res = _execute(
                _exec_body(
                    dry_run=True,
                    max_routes_per_run=50,
                    max_game_rows_per_run=0,
                    dispatch_window=[23 * 60, 7 * 60],
                    prune_to_window=True,
                ),
                svc=_dry_svc(live_enabled=True),
            )

        assert res.created_game_rows == 24, "the writes it would make"
        assert res.live_game_rows == 8, "the footprint it would leave"

    def test_the_run_end_records_both(self):
        import json
        from pathlib import Path

        res = self._run(_FakeLiveSvc(), prune_to_window=True)
        events = [
            json.loads(line)
            for line in Path(res.trace_path).read_text(encoding="utf-8").splitlines()
        ]

        assert events[-1]["created_game_rows"] == 24
        assert events[-1]["live_game_rows"] == 8


class TestTheRowBudgetHasADefault:
    """0 meant unbounded, and unbounded was the default.

    The UI defaults the box to 24 and omits the field when it is blank, so the
    server's own default is what a run actually gets -- and an unbounded default
    on the endpoint that writes is the opposite of what every other control here
    does. 24 is a day of hourly rows: one route at the shortest cycle.
    """

    def test_the_server_default_is_twenty_four_rows(self):
        assert ExecuteRequest.model_fields["max_game_rows_per_run"].default == 24

    def test_a_run_that_sends_nothing_is_bounded(self):
        # Two 1h routes are 48 rows. Without a default only the request count
        # bounded them.
        account = _account(
            [_row_with_cycle(20003, -1, 1, 100), _row_with_cycle(20011, -2, 1, 700)],
            {20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", 20011: "11", -1: "A", -2: "B"},
        )
        svc = _FakeLiveSvc()
        res = _run_live(svc, account, max_routes_per_run=50)

        assert len(svc.created) == 1, "24 rows is one hourly route"
        assert res.remaining == 1

    def test_zero_still_means_unbounded_when_asked_for(self):
        account = _account(
            [_row_with_cycle(20003, -1, 1, 100), _row_with_cycle(20011, -2, 1, 700)],
            {20003: (0, 0), 20011: (10, 0), -1: (40, 40), -2: (50, 50)},
            {20003: "03", 20011: "11", -1: "A", -2: "B"},
        )
        svc = _FakeLiveSvc()
        _run_live(svc, account, max_routes_per_run=50, max_game_rows_per_run=0)

        assert len(svc.created) == 2


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

    def _protected_but_drifted(self):
        # The plan's own schedule, so the destination is SATISFIED and the run
        # reaches the cargo check -- but hand-set amounts, which is what a
        # hand-made route has.
        return _FakeLiveSvc(
            existing={
                20003: _fanned(
                    20011,
                    10,
                    0,
                    cycle_hours=6,
                    dispatch_minute=100,
                    start_id=700000,
                    cargo={Resource.CROP: 9999},
                )
            }
        )

    def test_a_protected_routes_cargo_is_not_rewritten_either(self):
        # The exemption says "left untouched" -- and then the drift check
        # collected the same active rows, found the hand-set cargo different
        # from the plan's, and rewrote them. Every rewrite also stamps
        # `deliveries: 1`, so an ally route the operator built at deliveries 3
        # silently drops to a third of its volume and nothing downstream
        # detects it.
        svc = self._protected_but_drifted()
        res = _run_live(
            svc,
            _own_village_account(),
            disable_existing=True,
            max_routes_per_run=50,
            update_drifted=True,
            protect_destinations=["20011"],
        )

        assert svc.updated == [], "a protected row's cargo is the operator's, not the plan's"
        assert res.updates == []
        assert any("protected" in w.lower() for w in res.warnings), res.warnings

    def test_the_same_cargo_is_rewritten_without_the_exemption(self):
        # The control, so the exemption cannot quietly become "never update".
        svc = self._protected_but_drifted()
        _run_live(
            svc,
            _own_village_account(),
            disable_existing=True,
            max_routes_per_run=50,
            update_drifted=True,
        )

        assert svc.updated, "an unprotected drifted row is still corrected"

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
        # `phantom_creates` because this is about creates the game REALLY
        # refused: a `failed` answer over rows that landed is settled by the
        # read-back and withdraws both the verdict and the stop, so a double
        # that fans the rows out would be measuring the opposite case.
        svc = _FakeLiveSvc(create_status="failed", phantom_creates=True)
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


class TestAFailedWindowPruneIsNotSilent:
    """A prune that did not happen must not read as a run with nothing to say.

    `delete_routes` answers `failed` on a network error and `stopped` on the
    captcha/budget check; only `deleted` used to report anything, so a refused
    prune produced a byte-identical response to a successful one -- the same
    "created N route(s)" headline, an empty `problems` list, and therefore a
    clean `needs_attention` in run history -- while every out-of-window row kept
    departing round the clock.
    """

    def _account(self):
        return _account(
            [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
            {20003: (0, 0), -1: (40, 40)},
            {20003: "03", -1: "A"},
        )

    def _run(self, svc):
        return _run_live(
            svc,
            self._account(),
            max_routes_per_run=50,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

    def test_a_refused_prune_names_the_rows_still_departing(self):
        svc = _FakeLiveSvc(delete_status="failed")
        res = self._run(svc)

        assert svc.deleted, "the prune was attempted"
        assert res.problems, "a prune that did not happen must be reported"
        assert any("outside the profile" in p for p in res.problems), res.problems

    def test_the_successful_prune_is_still_silent(self):
        # The control: the two responses must not be identical, which is exactly
        # what they were.
        clean = self._run(_FakeLiveSvc())
        assert clean.problems == []

    def test_a_refused_prune_makes_the_run_need_attention(self, tmp_path):
        from pathlib import Path
        from shutil import copy

        from travian_api.services.distribution.run_history import summarise_runs

        res = self._run(_FakeLiveSvc(delete_status="failed"))
        copy(Path(res.trace_path), tmp_path)
        history = summarise_runs(tmp_path)

        assert history.runs[0].problems
        assert history.runs[0].needs_attention is True

    def test_a_stopped_prune_stops_the_run(self):
        # `stopped` is the captcha/budget answer: nothing was deleted AND the
        # run must not carry on as though it had been.
        svc = _FakeLiveSvc(delete_status="stopped")
        res = self._run(svc)

        assert any("outside the profile" in p for p in res.problems), res.problems
        assert any("stopped early" in p.lower() for p in res.problems), res.problems


class TestAnUnverifiablePruneIsSettledByLookingAtTheMarketplace:
    """A DELETE whose body could not be read is not a failed delete.

    `docs/15` records the empty 200 as the normal shape on this account, so
    `unverified` is the expected answer, not an exception. It is settled the
    same way an unverified disable is: the run re-reads the page and decides
    from state -- gone means pruned, still there means a problem.
    """

    def _run(self, svc):
        return _run_live(
            svc,
            _account(
                [_row_with_cycle(20003, -1, 1, 23 * 60 + 30)],
                {20003: (0, 0), -1: (40, 40)},
                {20003: "03", -1: "A"},
            ),
            max_routes_per_run=50,
            dispatch_window=[23 * 60, 7 * 60],
            prune_to_window=True,
        )

    def test_rows_the_game_really_removed_read_as_pruned(self):
        svc = _FakeLiveSvc(delete_status="unverified", delete_applies=True)
        res = self._run(svc)

        assert res.problems == [], res.problems
        assert any("pruned" in d for d in res.disables), res.disables

    def test_rows_still_on_the_page_are_reported(self):
        svc = _FakeLiveSvc(delete_status="unverified", delete_applies=False)
        res = self._run(svc)

        assert any("STILL THERE" in p for p in res.problems), res.problems


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


class TestConsumptionReachesTheThirdPlanningPath:
    """/execute recomputes the plan server-side, so it needs the spend too.

    Three of the four planning paths share `_plan_account`, but "shares a
    helper" is a claim about today's code, and /execute is the one that WRITES.
    A declared consumption that reached /plan and /day-check and not this one
    would mean the sheet the operator approved and the routes actually created
    were judged against different arithmetic.

    The fourth path, /night-profile, does NOT share `_plan_account` -- which is
    precisely how it came to ignore the field (R3-D2). It has its own test in
    tests/test_night_profile_endpoint.py.
    """

    ARMY = 20011

    def _snapshot(self):
        # Capacities the base fixture leaves unread, because a store with no cap
        # cannot overflow and the finding under test would never fire.
        return [
            {
                "village_id": 20003,
                "name": "03",
                "x": 0,
                "y": 0,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 20_000,
                "clay_per_hour": 0,
                "iron_per_hour": 0,
                "crop_per_hour": 0,
                "lumber_stock": 2_000_000,
                "warehouse_capacity": 5_000_000,
                "granary_capacity": 5_000_000,
            },
            {
                "village_id": self.ARMY,
                "name": "11",
                "x": 4,
                "y": 0,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 0,
                "clay_per_hour": 0,
                "iron_per_hour": 0,
                "crop_per_hour": 0,
                "lumber_stock": 40_000,
                "warehouse_capacity": 80_000,
                "granary_capacity": 80_000,
            },
        ]

    def _run(self, consumption=None):
        config = [{"village_id": 20003}, {"village_id": self.ARMY}]
        if consumption is not None:
            config[1]["consumption_per_hour"] = consumption
        body = _exec_body(
            dry_run=True,
            max_routes_per_run=50,
            snapshot=self._snapshot(),
            config=config,
            allocations={
                "lumber": {
                    "20003": {"mode": "remainder"},
                    str(self.ARMY): {"mode": "absolute", "value": 5_000},
                }
            },
        )
        body.foreign_targets = []
        return _execute(body, connected=False)

    @staticmethod
    def _capped(res):
        return [w for w in res.warnings if w.startswith("11:") and "hits the cap" in w]

    def test_the_dry_run_reports_the_phantom_overflow_without_a_spend(self):
        assert self._capped(self._run()), "the fixture must overflow when nothing is declared"

    def test_declaring_the_spend_silences_it_on_the_write_path_too(self):
        assert self._capped(self._run(consumption={"lumber": 5_000})) == []


class TestAnUnverifiedDisableIsSettledByLookingAtTheMarketplace:
    """A 200 whose body cannot be read is not "it failed".

    `docs/15` records the create's 200 body as EMPTY, and records that
    `routes[].error` came off the game's own `main.js` rather than an observed
    reply -- so on an account that has never run live, "the answer was
    unreadable" is a plausible verdict for EVERY toggle. Reading it as a
    failure deferred the origin and, worse, misreported: `disables` stayed
    empty, so the response and `run_history` said "disabled 0" while N rows
    were off, and the operator was sent to check rows already correct.

    This service already has the right answer for identical evidence. A create
    whose read-back fails is `created_unverified` -- "the write was accepted
    but the read-back failed: probably fine, not confirmed" -- and it does not
    defer the origin. So the toggle is settled the same way: LOOK, and decide
    from state.

    The decision has to be made before the creates, which is why the read-back
    is taken here rather than left to the end-of-origin verification: whether
    to create new routes on top turns on whether the stale rows are really off
    (issue #61).
    """

    def _svc(self, *, really_off):
        return _FakeLiveSvc(
            existing={20003: [ExistingRoute(9, _UNWANTED_DEST, 99, 98, active=True)]},
            disable_status="unverified",
            disable_applies=really_off,
        )

    def test_rows_the_game_really_switched_off_let_the_run_carry_on(self):
        svc = self._svc(really_off=True)

        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert (40, 40) in {(r.dest_x, r.dest_y) for r in svc.created}, (
            "the stale rows are off, so there is nothing to defer for"
        )
        # And the disable is REPORTED. Under the failure reading `disables`
        # stayed empty while the row was off, so the run said "disabled 0".
        assert any("20003" in line or "03" in line for line in res.disables), res.disables
        assert not any("STILL" in p for p in res.problems), res.problems

    def test_rows_still_shipping_defer_the_origin_exactly_as_a_failure_did(self):
        svc = self._svc(really_off=False)

        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}, (
            "a stale row still shipping must stop new routes on that origin"
        )
        assert any("disable" in p.lower() for p in res.problems), res.problems
        assert res.disables == [], "nothing may be claimed disabled while a row is active"

    def test_a_read_back_that_fails_defers_rather_than_guessing(self):
        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(9, _UNWANTED_DEST, 99, 98, active=True)]},
            disable_status="unverified",
            disable_applies=True,
            confirm_raises={20003},
        )

        res = _run_live(svc, _two_origin_account(), max_routes_per_run=50)

        assert (40, 40) not in {(r.dest_x, r.dest_y) for r in svc.created}
        assert any("disable" in p.lower() for p in res.problems), res.problems


class TestAnUnreadableReEnableSaysWhatTheGameSaid:
    """`_toggle_routes` writes an asymmetric detail on purpose.

    A toggle whose response body cannot be read returns "failed" with a detail
    saying the request DID return success, so some or all of the rows may have
    gone through -- for an enable, that they may still be inactive and a later
    run can re-enable them harmlessly. The re-enable branch reported a fixed
    line instead and dropped it, so the operator could not tell "the game said
    no" from "the answer was unreadable and the rows may already be on" -- and
    only the second means the next run's read-back will disagree with the
    trace. The disable branch and the cargo update both carry theirs through.
    """

    UNREADABLE = (
        "enabling of 4 route(s) cannot be confirmed: no 'routes' key in the response. "
        "The request returned success, so some or all may have gone through -- they "
        "may still be inactive, and a later run can re-enable them."
    )

    def _svc(self, detail, status="failed"):
        outer = self

        class _Unconfirmable(_FakeLiveSvc):
            async def enable_routes(self, vid, routes, *, stop_check=None):
                from travian_api.services.trade_route_service import RouteActionResult

                if not routes:
                    return None
                self.enabled.append((vid, tuple(sorted((r.dest_x, r.dest_y) for r in routes))))
                return RouteActionResult(vid, 0, 0, status, detail or outer.UNREADABLE)

        return _Unconfirmable(existing={20003: _fanned(_FOREIGN_REAL_ID, 40, 40, active=False)})

    def test_the_services_own_detail_reaches_the_operator(self):
        res = _run_live(
            self._svc(None, status="unverified"), _two_origin_account(), max_routes_per_run=50
        )

        assert any(self.UNREADABLE in problem for problem in res.problems), res.problems

    def test_the_problem_still_names_the_village_and_the_status(self):
        # RE-SEEDED for the status vocabulary: an unreadable toggle answers
        # `unverified` rather than `failed`, because the request DID return
        # success. The property under test is unchanged -- the village and the
        # service's verdict both reach the operator -- and the verdict is now
        # the one the detail has always described.
        res = _run_live(
            self._svc(None, status="unverified"), _two_origin_account(), max_routes_per_run=50
        )

        (problem,) = [p for p in res.problems if "re-enable" in p]
        assert problem.startswith("03:"), problem
        assert "unverified" in problem, problem

    def test_a_flat_refusal_reads_as_a_refusal(self):
        """The other half of the asymmetry: a detail that is not about an
        unreadable answer must not be dressed up as one."""
        res = _run_live(
            self._svc("2 of 4 route(s) rejected: [901, 902]"),
            _two_origin_account(),
            max_routes_per_run=50,
        )

        assert any("rejected: [901, 902]" in problem for problem in res.problems), res.problems


def _one_mismatched_destination():
    """One origin, one own destination the plan wants on a 6h cycle."""
    return _account(
        [_row_with_cycle(20003, 20011, 6, 100)],
        {20003: (0, 0), 20011: (10, 0)},
        {20003: "03", 20011: "11"},
    )


def _mismatched_rows():
    """Eight 3h rows where the plan wants four 6h ones: diverging, so the
    reconciler disables the destination and rebuilds it."""
    return {20003: _fanned(20011, 10, 0, cycle_hours=3, dispatch_minute=100, start_id=710000)}


class TestARefusedReplacementPutsTheOldRoutesBack:
    """Disable was chosen over delete because it is REVERSIBLE. Never using that
    reversibility wasted the property.

    A destination whose rows run a schedule the plan does not want is emptied and
    refilled in two requests. When the second one is refused the village is left
    receiving NOTHING -- its old rows off, no new ones -- and the run's answer
    was a problem line telling a human to go and switch them back on. The rows
    are still there, still disabled, still exactly as they were recorded before
    the disable went out: the run can put them back itself.

    Guarded, because an automatic write on a half-known state is worse than a
    dark village: every replacement create must be settled and REFUSED, a stable
    read-back must show zero replacement rows, and the old rows must still be
    exactly what the write-ahead record says they were. Anything else -- a
    partial rebuild, an indeterminate create, a destination that changed
    underneath -- is abandoned for a human.
    """

    def _run(self, svc, **kw):
        kw.setdefault("max_routes_per_run", 50)
        kw.setdefault("max_game_rows_per_run", 0)
        return _run_live(svc, _one_mismatched_destination(), **kw)

    def test_a_wholly_refused_replacement_restores_the_old_rows(self):
        svc = _FakeLiveSvc(
            existing=_mismatched_rows(), create_status="failed", phantom_creates=True
        )

        res = self._run(svc)

        assert svc.enabled == [(20003, ((10, 0),) * 8)], svc.enabled
        assert all(e.active for e in svc._existing[20003]), "the old rows are shipping again"
        assert any("restored" in line for line in res.re_enables), res.re_enables

    def test_the_restore_is_traced_from_attempt_to_outcome(self):
        svc = _FakeLiveSvc(
            existing=_mismatched_rows(), create_status="failed", phantom_creates=True
        )

        res = self._run(svc)

        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        assert kinds.count("restore_attempted") == 1, kinds
        assert kinds.count("restored") == 1, kinds
        assert "restore_failed" not in kinds, kinds

    def test_the_receiving_nothing_line_is_withdrawn(self):
        """It said the destination is receiving nothing and told the operator to
        re-enable its old rows in game. Both halves are now false."""
        svc = _FakeLiveSvc(
            existing=_mismatched_rows(), create_status="failed", phantom_creates=True
        )

        res = self._run(svc)

        assert not any("receiving nothing" in p for p in res.problems), res.problems

    def test_a_restore_the_game_refuses_is_reported(self):
        svc = _FakeLiveSvc(
            existing=_mismatched_rows(),
            create_status="failed",
            phantom_creates=True,
            enable_status="failed",
        )

        res = self._run(svc)

        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        assert "restore_failed" in kinds, kinds
        assert any("could not be switched back on" in p for p in res.problems), res.problems

    def test_a_destination_that_changed_underneath_is_not_restored(self):
        """The write-ahead record is the only description of what was switched
        off. If the marketplace no longer matches it, this run does not know what
        it would be turning on."""

        class _ARowVanished(_FakeLiveSvc):
            """Someone deleted one of the old rows in another tab while this run
            was between its two requests."""

            async def disable_routes(self, vid, routes, *, stop_check=None):
                result = await super().disable_routes(vid, routes, stop_check=stop_check)
                self._existing[vid] = self._existing[vid][:-1]
                return result

        svc = _ARowVanished(
            existing=_mismatched_rows(), create_status="failed", phantom_creates=True
        )

        res = self._run(svc)

        assert svc.enabled == [], "nothing is switched on against a record that no longer holds"
        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        assert "replacement_abandoned" in kinds, kinds
        assert any("no longer matches" in p for p in res.problems), res.problems

    def test_an_indeterminate_replacement_is_never_restored(self):
        """An indeterminate create may already be in the game. Switching the old
        rows back on would then ship BOTH schedules at once, which is the exact
        state disable-and-recreate exists to avoid."""

        class _Unsettled(_FakeLiveSvc):
            def __init__(self, **kw):
                super().__init__(**kw)
                self._reads = 0

            async def confirm_routes(self, vid, *, map_span=None):
                rows = await super().confirm_routes(vid, map_span=map_span)
                self._reads += 1
                if self._reads % 2 == 1:
                    return rows[:-1]
                return rows

        svc = _Unsettled(existing=_mismatched_rows(), create_status="failed", phantom_creates=True)

        res = self._run(svc)

        assert [a.status for a in res.actions] == ["indeterminate"], [
            (a.status, a.detail) for a in res.actions
        ]
        assert svc.enabled == [], "an unsettled replacement is never undone automatically"

    def test_a_replacement_that_landed_is_left_alone(self):
        """The control: a rebuild that worked must not be undone."""
        svc = _FakeLiveSvc(existing=_mismatched_rows())

        res = self._run(svc)

        assert svc.enabled == [], svc.enabled
        assert [a.status for a in res.actions] == ["created"], [a.detail for a in res.actions]


class TestTheDisableRecordSaysWhatHappenedToEachRow:
    """`rows_disabled` is the write-ahead chain's closing half, and the only
    record of what was switched off if the run dies before it can rebuild.

    "Whatever the game answered" was enough to stop the record dangling and not
    enough to recover from. The answer is one status for a whole batch, and the
    one answer that matters most -- `unverified`, which is what a reset, a
    session-expiry redirect or an unreadable body produce -- says nothing at all
    about the rows. Reading it as a refusal is the same over-statement the
    service itself stopped making. The record now carries a verdict per ROW:
    `confirmed` (off), `failed` (not off) and `unknown` (nobody can say), taken
    from the disable's own read-back where there is one.
    """

    def _existing(self):
        return {20003: _fanned(20011, 10, 0, cycle_hours=3, dispatch_minute=100, start_id=710000)}

    def _rows(self, res):
        (event,) = [e for e in _trace_events(res.trace_path) if e["kind"] == "rows_disabled"]
        return event

    def _run(self, svc):
        return _run_live(
            svc,
            _one_mismatched_destination(),
            max_routes_per_run=50,
            max_game_rows_per_run=0,
        )

    def test_a_clean_disable_confirms_every_row(self):
        svc = _FakeLiveSvc(existing=self._existing())

        event = self._rows(self._run(svc))

        assert {r["verdict"] for r in event["rows"]} == {"confirmed"}, event
        assert sorted(r["route_id"] for r in event["rows"]) == event["route_ids"], event

    def test_a_refused_disable_marks_every_row_failed(self):
        svc = _FakeLiveSvc(existing=self._existing(), disable_status="failed")

        event = self._rows(self._run(svc))

        assert {r["verdict"] for r in event["rows"]} == {"failed"}, event

    def test_an_unreadable_answer_over_rows_that_went_off_is_not_a_refusal(self):
        """The whole point: the PUT's body could not be read and the rows ARE
        off. Recording that as a refusal sends the operator to switch off rows
        already switched off, and describes an account that does not exist."""
        svc = _FakeLiveSvc(
            existing=self._existing(), disable_status="unverified", disable_applies=True
        )

        event = self._rows(self._run(svc))

        assert {r["verdict"] for r in event["rows"]} == {"confirmed"}, event

    def test_an_unreadable_answer_over_rows_still_shipping_is_a_failure(self):
        svc = _FakeLiveSvc(
            existing=self._existing(), disable_status="unverified", disable_applies=False
        )

        event = self._rows(self._run(svc))

        assert {r["verdict"] for r in event["rows"]} == {"failed"}, event

    def test_rows_the_read_back_could_not_see_are_unknown(self):
        """An unreadable answer and an unreadable page. Two unknowns in a row
        are not evidence of anything, and the record must not pretend."""
        svc = _FakeLiveSvc(
            existing=self._existing(),
            disable_status="unverified",
            disable_applies=True,
            confirm_raises={20003},
        )

        event = self._rows(self._run(svc))

        assert {r["verdict"] for r in event["rows"]} == {"unknown"}, event

    def test_the_record_still_closes_before_the_replacement_is_written(self):
        """The read-back the verdicts come from is a READ; it must not push the
        closing half of the chain past the create."""
        svc = _FakeLiveSvc(
            existing=self._existing(), disable_status="unverified", disable_applies=True
        )

        res = self._run(svc)

        kinds = [e["kind"] for e in _trace_events(res.trace_path)]
        order = [kinds.index(k) for k in ("replacement_started", "rows_disabled", "created")]
        assert order == sorted(order), kinds
