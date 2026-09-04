"""Provisioning gets interrupted. Does resuming it still land somewhere correct?

The whole-day provisioning of a real 25-village account is ~190 paced requests
over many chunks and tens of minutes. Over that span an interruption is not an
edge case, it is the expected path: a captcha appears, the daily activity budget
runs out, a marketplace read fails, the operator closes the tab, or the game
accepts a create and silently produces nothing.

Every one of those leaves the account HALF PROVISIONED -- some villages done,
one village mid-write, the rest untouched. What must then be true is that
resuming finishes the job and settles, without duplicating what the interrupted
run already created. Nothing tested that: the existing suite covers each
interruption's immediate response (it stops, it reports, it defers) but never
what the NEXT run does with the wreckage.

These drive interruption then resumption against the same fake marketplace, and
require convergence with no duplicates.
"""

import pytest

from travian_api.services.trade_route_service import RouteActionResult
from travian_api.web.routes import distribution as dist_module

from .test_distribution_execute import _execute, _FakeLiveSvc, _patch
from .test_execution_converges import _clear_writes, _drive_to_fixed_point, _writes
from .test_whole_day_execute import ACCOUNTS, _segments_body


def _run_once(svc, body=None, accounts=ACCOUNTS):
    async def _plan(_body, dispatch_window=None, overnight=None):
        return accounts[tuple(dispatch_window)]

    with (
        _patch(dist_module, "_plan_account", _plan),
        _patch(dist_module.random, "shuffle", lambda seq: None),
    ):
        return _execute(body or _segments_body(), svc=svc)


def _live_rows(svc):
    return [r for rows in svc._existing.values() for r in rows]


def _row_fingerprints(svc):
    """(destination, departure minute) for every ACTIVE live row.

    Duplicates matter here: two active rows to one destination at the same
    minute means the same delivery fires twice, which is the corruption an
    interrupted-then-resumed run could plausibly cause.
    """
    return sorted(
        (r.dest_village_id, (r.departure_at % 86400) // 60 if r.departure_at else -1)
        for r in _live_rows(svc)
        if r.active
    )


class TestACaptchaMidProvisioning:
    def test_resuming_after_a_stopped_run_still_converges(self):
        """`stopped` is what a captcha or a stop signal produces after the
        pacing wait and before the POST: nothing was written for that route."""
        svc = _FakeLiveSvc(create_status="stopped")
        res = _run_once(svc)

        # `svc.created` records only creates that reached the game, and a stop
        # lands before the POST -- so the evidence the attempt happened is in
        # the response, not the fake's write log.
        assert any(a.status in ("deferred", "blocked", "failed") for a in res.actions), (
            f"the fixture must have attempted and failed a create; got "
            f"{sorted({a.status for a in res.actions})}"
        )
        assert not _live_rows(svc), "a stopped create must leave no rows behind"

        # The captcha clears; the operator runs again.
        svc._create_status = "created"
        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled after resuming; writes per pass {counts}"

    def test_a_stop_partway_through_leaves_no_duplicate_on_resume(self):
        """The dangerous shape: the FIRST route of a village is created, then the
        run stops. The resumed run must add only what is missing."""
        svc = _FakeLiveSvc()
        stop_after = {"n": 0}

        real_create = svc.create_route

        async def _create_then_stop(route, *, stop_check=None):
            stop_after["n"] += 1
            if stop_after["n"] > 1:
                return RouteActionResult(
                    route.origin_village_id, route.dest_x, route.dest_y, "stopped", "captcha (test)"
                )
            return await real_create(route, stop_check=stop_check)

        svc.create_route = _create_then_stop
        _run_once(svc)
        after_interruption = _row_fingerprints(svc)
        assert after_interruption, "one route should have landed before the stop"

        svc.create_route = real_create
        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled after a partial run; {counts}"
        final = _row_fingerprints(svc)
        assert len(final) == len(set(final)), (
            f"duplicate active rows after resuming a partial run: "
            f"{[f for f in final if final.count(f) > 1]} -- the same delivery now fires twice"
        )
        # What the interrupted run created must still be there, not rebuilt.
        assert set(after_interruption) <= set(final), (
            "the resumed run destroyed rows the interrupted run had already made"
        )


class TestTheDailyBudgetRunningOut:
    def test_resuming_after_an_exhausted_budget_converges(self):
        """ActivityBudgetExhausted is the stealth layer refusing to spend more
        today. Tomorrow's run has to be able to finish the job."""
        svc = _FakeLiveSvc()
        svc.budget_ok = False
        _run_once(svc)

        svc.budget_ok = True
        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled after the budget refilled; {counts}"
        final = _row_fingerprints(svc)
        assert len(final) == len(set(final)), f"duplicates after a budget interruption: {final}"


class TestAGameThatAcceptsAndDoesNothing:
    def test_a_phantom_create_is_retried_and_then_settles(self):
        """The failure a 200 with an empty body cannot rule out: the game accepts
        the create and produces no route. The run reports it unverified; the NEXT
        run must create it for real rather than believing the first one."""
        svc = _FakeLiveSvc(phantom_creates=True)
        res = _run_once(svc)

        assert not _live_rows(svc), "a phantom create leaves the marketplace empty"
        assert res.created_unverified or res.not_created, (
            "a create that produced nothing must be reported as unverified or "
            "not-created, never as created"
        )

        svc._phantom = False
        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled after phantom creates stopped; {counts}"


class TestAMarketplaceThatWillNotRead:
    def test_a_village_whose_read_fails_is_finished_on_the_next_run(self):
        svc = _FakeLiveSvc(read_raises={20003})
        _run_once(svc)

        assert not svc.created, "nothing may be written to a village that could not be read"

        svc._read_raises = set()
        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled after the read recovered; {counts}"

    def test_a_verify_read_that_fails_does_not_cause_a_duplicate_next_run(self):
        """The nastiest ordering: the create SUCCEEDED, then the confirming read
        failed. The run cannot know what landed. If the next run trusts the
        pessimistic reading it creates a second copy of a route that exists."""
        svc = _FakeLiveSvc(confirm_raises={20003})
        res = _run_once(svc)

        assert res.created_unverified or res.problems, (
            "an unconfirmable create must be reported, not counted as verified"
        )

        svc._confirm_raises = set()
        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled after verification recovered; {counts}"
        final = _row_fingerprints(svc)
        assert len(final) == len(set(final)), (
            f"the run that could not verify its own create caused a duplicate: "
            f"{[f for f in final if final.count(f) > 1]}"
        )


class TestTheCapsThemselvesAreAnInterruption:
    @pytest.mark.parametrize("routes_per_run", [1, 2])
    def test_provisioning_a_chunk_at_a_time_reaches_the_same_place(self, routes_per_run):
        """This is how the operator will actually run it: a small cap, many
        passes. The end state must match what one unbounded run produces --
        otherwise the careful way of doing it is the wrong way."""
        unbounded = _FakeLiveSvc()
        _drive_to_fixed_point(unbounded, _segments_body, ACCOUNTS)

        chunked = _FakeLiveSvc()
        passes, counts = _drive_to_fixed_point(
            chunked,
            lambda: _segments_body(max_routes_per_run=routes_per_run),
            ACCOUNTS,
            max_passes=12,
        )

        assert passes is not None, (
            f"a cap of {routes_per_run} never finished provisioning; writes per pass {counts}"
        )
        assert _row_fingerprints(chunked) == _row_fingerprints(unbounded), (
            "chunked provisioning reached a different game state than one unbounded run"
        )

    def test_the_row_budget_also_finishes_eventually(self):
        """max_game_rows_per_run bounds the ROWS a run may create, which is what
        the operator sets for a careful first pass. It must not be able to
        deadlock: a route whose fan-out exceeds the budget forever would defer
        forever."""
        svc = _FakeLiveSvc()

        passes, counts = _drive_to_fixed_point(
            svc,
            lambda: _segments_body(max_game_rows_per_run=8),
            ACCOUNTS,
            max_passes=12,
        )

        assert passes is not None, (
            f"an 8-row budget never finished; writes per pass {counts} -- if a single "
            f"route's surviving fan-out exceeds the budget it defers on every run"
        )


class TestNothingIsWrittenTwiceAcrossTheWholeSequence:
    def test_the_converged_account_holds_exactly_the_planned_rows(self):
        """The end-to-end statement: after provisioning settles, every active row
        in the game corresponds to a planned departure, and every planned
        departure exists exactly once. This is the property the operator is
        really buying -- the game matching the sheet."""
        from travian_api.web.routes.distribution import _planned_minutes

        svc = _FakeLiveSvc()
        _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)
        _clear_writes(svc)
        _run_once(svc)
        assert _writes(svc) == 0, "not converged; the assertions below would be premature"

        # What the two profiles between them asked for.
        wanted: list[tuple[int, int]] = []
        for account in ACCOUNTS.values():
            for row in account.plan.rows:
                # Rebuild the PlannedRoute shape _planned_minutes needs.
                from travian_api.services.trade_route_service import PlannedRoute

                window = next(w for w, a in ACCOUNTS.items() if a is account)
                route = PlannedRoute(
                    origin_village_id=row.origin,
                    dest_village_id=row.destination,
                    dest_x=0,
                    dest_y=0,
                    dest_name="",
                    cargo=dict(row.cargo),
                    cycle_hours=row.cycle_hours,
                    merchants=row.merchants,
                    dispatch_minute=row.dispatch_minute,
                    window=tuple(window),
                )
                wanted.extend((row.destination, m) for m in _planned_minutes(route))

        assert _row_fingerprints(svc) == sorted(wanted), (
            "the settled game state is not exactly the planned row set"
        )
