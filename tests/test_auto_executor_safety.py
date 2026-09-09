"""The auto-executor safety review of 2026-09-08, turned into standing tests.

`docs/resource-planner-auto-executor-review-2026-09-08.md` reproduced seven
ways the automatic reconciliation sweep can lose work, keep going after a stop,
or write against an inventory it had no right to trust. Its own probes asserted
the BROKEN behaviour, on purpose, and were deleted with the review; these assert
the safe behaviour instead, so a regression is a red test rather than a live
write against the operator's account.

One class per finding, named for it. Every case here failed before the fix.
"""

from __future__ import annotations

import contextlib
import json
import os
import time

import pytest

from travian_api.parsers.html_parser import (
    MarketplaceModelInvalid,
    read_trade_routes,
    read_trade_routes_from_view,
)


def _page(model: object) -> str:
    """A marketplace page carrying *model* the way the real one carries it.

    The wrapper is what `_trade_route_view_data` looks for, so a page built this
    way is RECOGNISED -- which is the whole point of these cases. A page that
    carried no model at all already failed closed before this review; the gap
    was a wrapper whose contents could not be trusted.
    """
    return f"<html><script>TradeRoutes.render({{viewData: {json.dumps(model)}}})</script></html>"


def _model(collections: object, *, village: int | None = 20003) -> dict:
    own: dict[str, object] = {"id": 4001, "village": {"marketplace": {"tradeRoutes": collections}}}
    if village is not None:
        own["currentVillageId"] = village
    return {"ownPlayer": own}


def _collection(dest: int = 20010, *, source: int = 20003, routes: list | None = None) -> dict:
    return {
        "from": {"id": source, "name": "02"},
        "to": {"id": dest, "mapId": 50000, "name": "V01"},
        "routes": routes
        if routes is not None
        else [
            {
                "id": 600001,
                "enabled": True,
                "carriedResources": {"lumber": 10, "clay": 0, "iron": 0, "crop": 0},
                "departureAt": 1700082800,
                "repeat": 1,
                "merchants": 1,
            }
        ],
    }


class TestAE03MalformedInventoryIsNotAnEmptyVillage:
    """A recognised wrapper whose contents are malformed must not read as "no
    routes here".

    That reading is the expensive one: the reconciler creates the whole plan on
    top of whatever is already running. A village with genuinely no routes and a
    model we could not parse are different answers and only one of them is safe
    to build on.
    """

    @pytest.mark.parametrize(
        ("name", "model"),
        [
            ("empty model", {}),
            ("null marketplace", {"ownPlayer": {"id": 4001, "village": {"marketplace": None}}}),
            ("null tradeRoutes", _model(None)),
            ("tradeRoutes is not a list", _model({"nope": 1})),
        ],
    )
    def test_a_structurally_broken_model_is_refused(self, name, model):
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(_page(model))

    def test_an_unaddressable_destination_holding_routes_is_refused(self):
        """The case that loses data rather than inventing it.

        The collection carries real route ids but no destination we can address,
        so the old reader skipped it silently -- and every route inside it
        vanished from the inventory the reconciler then wrote against.
        """
        broken = {"to": {"name": "somewhere"}, "routes": [{"id": 600002}]}
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(_page(_model([broken])))

    def test_an_unparseable_route_row_is_refused(self):
        collection = _collection(routes=[{"id": 600001}, {"no_id": True}])
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(_page(_model([collection])))

    def test_a_genuinely_empty_marketplace_is_still_empty(self):
        """The other half of the rule. Refusing everything would be safe and
        useless: a village with no routes must still read as no routes."""
        assert read_trade_routes(_page(_model([]))) == []

    def test_a_collection_with_no_rows_is_not_a_defect(self):
        """A destination the operator has set up but scheduled nothing to."""
        assert read_trade_routes(_page(_model([_collection(routes=[])]))) == []

    def test_the_graphql_readback_applies_the_same_rule(self):
        """One reader, one standard. The read-back decides whether a write
        landed, so a malformed answer there must not read as "nothing was
        created" either."""
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes_from_view(_model([{"to": {}, "routes": [{"id": 1}]}]))


class TestAE04InventoryMustBelongToTheVillageAskedFor:
    """The initial read pins the village in the URL and then believes whatever
    comes back.

    A `?newdid=` from a concurrent loop, a redirect, or a stale page is enough to
    hand back another village's marketplace, and the reconciler will classify
    those routes as this origin's existing schedule -- then disable, replace or
    duplicate against them. The GraphQL read-back already checks
    `currentVillageId`; this is the same check at the boundary that decides the
    first write.
    """

    def test_the_model_states_which_village_it_describes(self):
        from travian_api.parsers.html_parser import marketplace_village_id

        assert marketplace_village_id(_model([], village=20003)) == 20003

    def test_a_model_that_will_not_say_is_refused(self):
        from travian_api.parsers.html_parser import marketplace_village_id

        assert marketplace_village_id(_model([], village=None)) is None

    def test_rows_carry_the_village_they_were_read_from(self):
        """`from.id` is the per-collection anchor, and it is what catches a
        model that names the right village at the top and carries another one's
        routes underneath."""
        rows = read_trade_routes(_page(_model([_collection(source=20003)])))
        assert [r["from_village_id"] for r in rows] == [20003]


class TestAE06TraceLossIsVisibleAndFailsClosed:
    """Write-ahead evidence that silently stopped being written is worse than
    no evidence at all: the run still looks fully recorded.

    The review injected a disk-full handle into a real trace and the executor
    created a route, reported no problems, and left a zero-byte file. Logging
    must still never raise -- a failed `flush` after a successful game write is
    the worst possible moment to throw -- so the fix is not "raise here", it is
    "remember, and let the caller refuse the NEXT mutation".
    """

    def _trace(self, tmp_path):
        from travian_api.services.distribution import execution_trace as et

        et.TRACE_DIR = tmp_path
        return et.ExecutionTrace(run_id="probe-run")

    def test_a_healthy_trace_reports_healthy(self, tmp_path):
        trace = self._trace(tmp_path)
        trace.event("run_start", user=1)

        assert trace.persistence_failed is False
        assert trace.persistence_error is None

    def test_a_failed_write_is_remembered_without_raising(self, tmp_path):
        trace = self._trace(tmp_path)

        class Full:
            def write(self, _data):
                raise OSError("simulated disk full")

            def flush(self):
                raise OSError("simulated disk full")

            def close(self):
                pass

        trace._handle = Full()
        trace.event("about_to_create", origin=20003)  # must not raise

        assert trace.persistence_failed is True
        assert "disk full" in (trace.persistence_error or "")

    def test_the_first_failure_is_the_one_reported(self, tmp_path):
        """Later failures are usually the same cause; the first is the one with
        the context that explains when evidence stopped."""
        trace = self._trace(tmp_path)

        class Failing:
            def __init__(self):
                self.n = 0

            def write(self, _data):
                self.n += 1
                raise OSError(f"failure {self.n}")

            def flush(self):
                pass

            def close(self):
                pass

        trace._handle = Failing()
        trace.event("one")
        trace.event("two")

        assert "failure 1" in (trace.persistence_error or "")

    def test_a_disabled_trace_is_not_a_failed_one(self, tmp_path):
        """Tracing that never opened is a different state from tracing that
        stopped mid-run: the executor already refuses to start without a trace,
        so this must not be conflated with losing one."""
        trace = self._trace(tmp_path)
        trace.enabled = False
        trace.event("ignored")

        assert trace.persistence_failed is False

    def test_no_live_write_is_attempted_once_evidence_is_lost(self, tmp_path):
        """The half that matters. Remembering the loss is bookkeeping; refusing
        the next mutation is the safety property.

        Asserted at `_require_live`, which every live write funnels through, so
        it holds for creates, toggles, cargo updates and deletes alike rather
        than for whichever call site a test happened to pick.
        """
        from travian_api.services.trade_route_service import (
            ExecutionEvidenceLost,
            TradeRouteService,
        )

        service = TradeRouteService.__new__(TradeRouteService)
        service.live_enabled = True
        service.trace = self._trace(tmp_path)

        service._require_live()  # healthy trace: writes are allowed

        service.trace._handle = None
        service.trace._persistence_error = "about_to_create: simulated disk full"
        with pytest.raises(ExecutionEvidenceLost) as caught:
            service._require_live()
        # The message has to say the rows already written are real. Reading it
        # as a rollback is the mistake that turns a partial run into a duplicate
        # one on the next attempt.
        assert "NOT rolled back" in str(caught.value)

    def test_a_run_with_no_trace_at_all_is_not_blocked_here(self, tmp_path):
        """Tracing disabled is refused before a live run starts, not by this
        gate -- and preview runs legitimately carry no trace."""
        from travian_api.services.trade_route_service import TradeRouteService

        service = TradeRouteService.__new__(TradeRouteService)
        service.live_enabled = True
        service.trace = None

        service._require_live()


class TestAE07OneExecutorPerAccount:
    """Two service objects for one game account must not reconcile it at once.

    The lock used to be built per instance, so two `TradeRouteService` objects
    -- two browser tabs, or the same account reached through two sessions --
    each held their own and both could read the marketplace before either
    wrote. The review demonstrated the independence; these pin the sharing.
    """

    def _service(self, base: str, user: str):
        from types import SimpleNamespace

        from travian_api.services.trade_route_service import TradeRouteService

        client = SimpleNamespace(settings=SimpleNamespace(base_url=base, username=user))
        return TradeRouteService(http_client=client)

    def test_two_services_on_one_account_share_the_lock(self):
        import asyncio

        async def check():
            a = self._service("https://ts2.example", "player")
            b = self._service("https://ts2.example", "player")
            assert a.execute_lock is b.execute_lock
            async with a.execute_lock:
                assert b.execute_lock.locked(), "the second service must see it held"

        asyncio.run(check())

    def test_a_trailing_slash_is_the_same_account(self):
        import asyncio

        async def check():
            a = self._service("https://ts2.example", "player")
            b = self._service("https://ts2.example/", "player")
            assert a.execute_lock is b.execute_lock

        asyncio.run(check())

    def test_different_accounts_do_not_block_each_other(self):
        """Serialising unrelated accounts would be safe and wrong: a second
        world's run would queue behind the first for no reason."""
        import asyncio

        async def check():
            a = self._service("https://ts2.example", "player")
            b = self._service("https://ts5.example", "player")
            assert a.execute_lock is not b.execute_lock
            async with a.execute_lock:
                assert not b.execute_lock.locked()

        asyncio.run(check())

    def test_a_service_that_cannot_name_its_account_still_serialises(self):
        """If we cannot tell two services apart, sharing one lock is the safe
        mistake; running them concurrently is the expensive one."""
        import asyncio

        async def check():
            a = self._service("", "")
            b = self._service("", "")
            assert a.execute_lock is b.execute_lock

        asyncio.run(check())

    def test_the_lock_survives_being_used_from_a_second_event_loop(self):
        """`asyncio.Lock` binds to the first loop that awaits it. A module-level
        lock reused across `asyncio.run` calls would raise on the second one --
        which is why the registry is keyed by loop as well as by account."""
        import asyncio

        async def once():
            service = self._service("https://ts2.example", "player")
            async with service.execute_lock:
                pass

        asyncio.run(once())
        asyncio.run(once())


class TestAE05StaleCargoIsUnfinishedWork:
    """A route left carrying the wrong cargo is not a finished route.

    When the per-run cap is spent the executor records a `deferred` trace
    decision and emits a `skipped` action -- both truthful -- but never added
    the route to the deferred list the completion contract is computed from. So
    `remaining` came back 0 with a schedule still shipping the old amount, and
    the browser's completion predicate reported success.

    The detail line was right and the aggregate was wrong, which is the worst
    combination: the number is what the sweep reads.

    Reaching the branch needs the budget SPENT, not merely small: cap 0 is
    "reconcile only" and defers everything before this point, which was already
    correct. So the account below drifts two destinations and allows one update.
    """

    def _account(self):
        from types import SimpleNamespace

        from travian_api.services.distribution.allocation import Resource
        from travian_api.web.routes.distribution import SheetRow

        rows = tuple(
            SheetRow(
                origin=20003,
                destination=dest,
                cargo={Resource.CROP: 100},
                cycle_hours=6,
                dispatch_minute=minute,
                arrival_minute=0,
                merchants=2,
            )
            for dest, minute in ((20011, 100), (20012, 100))
        )
        return SimpleNamespace(
            plan=SimpleNamespace(is_feasible=True, warnings=(), rows=rows),
            names={20003: "03", 20011: "11", 20012: "12"},
            coords={20003: (0, 0), 20011: (10, 0), 20012: (20, 0)},
            warnings=[],
            dropped_allocations=[],
        )

    def _run(self, cap: int):
        from travian_api.services.distribution.allocation import Resource

        from .test_distribution_execute import _FakeLiveSvc, _fanned, _run_live

        drifted = {Resource.CROP: 9000}
        svc = _FakeLiveSvc(
            existing={
                20003: _fanned(20011, start_id=800, cargo=drifted)
                + _fanned(20012, start_id=900, cargo=drifted)
            }
        )
        return svc, _run_live(svc, self._account(), update_drifted=True, max_routes_per_run=cap)

    def test_a_capped_update_still_counts_as_outstanding(self):
        svc, res = self._run(cap=1)

        assert len(svc.updated) == 1, "the cap must still stop the second write"
        assert res.remaining >= 1, (
            "the second route was knowingly left carrying stale cargo, so the run is "
            "not finished; remaining=0 is what let the sweep declare COMPLETE"
        )

    def test_the_action_still_says_why(self):
        """The detail was never the problem and must not regress into silence."""
        _svc, res = self._run(cap=1)

        assert any("cargo stale" in (a.detail or "") for a in res.actions)

    def test_an_uncapped_run_corrects_both_and_reports_nothing_outstanding(self):
        """The other half: once the updates actually fire there is no
        outstanding work, or every drifted account looks permanently
        unfinished."""
        svc, res = self._run(cap=50)

        assert len(svc.updated) == 2
        assert res.remaining == 0


class TestAE02TheResponseSaysWhetherItStopped:
    """A terminal stop the client cannot see is a stop that does not stop anything.

    `stopped_early` and `gold_club_blocked` were locals written to the trace, and
    the browser's automatic sweep branched on them as if they were response
    fields. They came back `undefined`, read as falsy, and the sweep asked for
    the next chunk through captchas, spent budgets and unreadable pages alike.
    """

    def test_the_fields_exist_on_the_schema(self):
        """Pinned against the model, not a mock. The whole defect was a browser
        reading fields the real response never had, so a frontend fixture that
        invents them would reproduce the bug rather than catch it."""
        from travian_api.web.routes.distribution import ExecuteResponse

        for field in ("stopped_early", "gold_club_blocked", "stop_reason", "deferred_origins"):
            assert field in ExecuteResponse.model_fields, field

    def test_a_clean_run_says_it_did_not_stop(self):
        from .test_distribution_execute import _FakeLiveSvc, _own_village_account, _run_live

        res = _run_live(_FakeLiveSvc(), _own_village_account(), max_routes_per_run=50)

        assert res.stopped_early is False
        assert res.gold_club_blocked is False
        assert res.stop_reason is None

    def test_the_stop_reason_is_absent_rather_than_empty_when_nothing_stopped(self):
        """None, not "": a client showing `stop_reason` must not render a blank
        banner over a run that finished."""
        from .test_distribution_execute import _FakeLiveSvc, _own_village_account, _run_live

        res = _run_live(_FakeLiveSvc(), _own_village_account(), max_routes_per_run=50)

        assert res.stop_reason is None


class TestAE01UnfinishedWorkSurvivesAFilteredChunk:
    """`remaining` is a count for the origins in ONE request.

    The sweep narrows the next chunk to the villages it has not visited, so a
    village that was visited and only partly provisioned is not in that request
    -- and the zero that comes back says nothing about it. It drops out of the
    loop and the UI reports COMPLETE over routes that were never created.

    The response now names the villages the count belongs to, so a caller can
    union them across chunks instead of replacing its own total each time.
    """

    def test_a_village_with_deferred_work_is_named(self):
        from .test_auto_executor_safety import TestAE05StaleCargoIsUnfinishedWork

        _svc, res = TestAE05StaleCargoIsUnfinishedWork()._run(cap=1)

        assert res.remaining >= 1
        assert res.deferred_origins == [20003], (
            "the count has to be attributable to a village, or a later chunk that "
            "does not include that village cannot know the work is still owed"
        )

    def test_a_run_with_nothing_left_names_nobody(self):
        from .test_distribution_execute import _FakeLiveSvc, _own_village_account, _run_live

        res = _run_live(_FakeLiveSvc(), _own_village_account(), max_routes_per_run=50)

        assert res.deferred_origins == []

    def test_the_named_origins_match_the_count(self):
        """Not a strict equality -- one village can owe several routes -- but a
        non-zero count with no origins named is the exact shape of the bug."""
        from .test_auto_executor_safety import TestAE05StaleCargoIsUnfinishedWork

        _svc, res = TestAE05StaleCargoIsUnfinishedWork()._run(cap=1)

        assert bool(res.remaining) == bool(res.deferred_origins)


class TestTheRefusalReachesCallersAsTheErrorTheyHandle:
    """`MarketplaceModelInvalid` is a `ValueError`, and every executor path
    catches `TravianError`.

    Caught during self-review of the AE-03 fix: making the read-back's reader
    strict without translating its error at the service boundary turned a
    partly-readable read-back into an unhandled 500 -- and the read-back runs
    AFTER a write, which is the one moment a crash costs the most. Strictness
    at the parser, one vocabulary at the service.
    """

    def _service(self, view):
        import asyncio
        from types import SimpleNamespace

        from travian_api.services.trade_route_service import TradeRouteService

        service = TradeRouteService(
            http_client=SimpleNamespace(settings=SimpleNamespace(base_url="x", username="y"))
        )

        async def _refresh(_village_id):
            return view

        service.refresh_marketplace = _refresh
        return service, asyncio

    def test_a_half_read_readback_raises_the_handled_error(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, aio = self._service(_model([{"to": {}, "routes": [{"id": 1}]}]))

        with pytest.raises(MarketplaceUnreadable):
            aio.run(service.confirm_routes(20003))

    def test_a_readable_readback_still_works(self):
        service, aio = self._service(_model([_collection()]))

        rows = aio.run(service.confirm_routes(20003))

        assert [r.route_id for r in rows] == [600001]


class TestNestedMalformedRowsAreRefusedToo:
    """The second pass over AE-03, from independent verification.

    The first fix validated the collection and each row but walked the rows
    container with `... or []`, so `null`, `{}` and `""` still read as "no routes
    at this destination" -- the same wrong answer one level deeper -- and a
    non-iterable or a non-numeric cargo escaped as a bare TypeError/ValueError,
    bypassing the service's translation and surfacing as a 500.
    """

    def _one(self, routes):
        collection = {"from": {"id": 20003}, "to": {"id": 20010, "mapId": 50000, "name": "V01"}}
        return _page(_model([{**collection, "routes": routes}]))

    @pytest.mark.parametrize(
        ("name", "routes"),
        [
            ("null", None),
            ("object", {}),
            ("empty string", ""),
            ("a number", 5),
        ],
    )
    def test_a_rows_container_that_is_not_a_list_is_refused(self, name, routes):
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(self._one(routes))

    def test_cargo_that_is_not_numeric_is_refused(self):
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(self._one([{"id": 1, "carriedResources": {"lumber": "lots"}}]))

    def test_cargo_that_is_not_an_object_is_refused(self):
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(self._one([{"id": 1, "carriedResources": 7}]))

    def test_a_cargo_object_stating_nothing_is_refused(self):
        """`{}` is not "carries nothing", it is "we were told nothing". The
        difference decides whether a correct route gets rewritten."""
        with pytest.raises(MarketplaceModelInvalid):
            read_trade_routes(self._one([{"id": 1, "carriedResources": {}}]))

    def test_a_partial_cargo_object_is_read_with_the_rest_as_zero(self):
        """Deliberately NOT refused. The real model states all four every time,
        but refusing a partial one bets the whole executor on that holding for
        every route in every state, and the cost of being wrong is that every
        read fails. Stating some amounts is information; stating none is not."""
        rows = read_trade_routes(self._one([{"id": 1, "carriedResources": {"lumber": 5}}]))

        assert rows[0]["cargo"] == {"lumber": 5, "clay": 0, "iron": 0, "crop": 0}

    def test_nothing_escapes_as_a_bare_python_error(self):
        """The property behind all of the above: whatever the game sends, this
        reader answers with rows, None, or `MarketplaceModelInvalid` -- never a
        TypeError the service has no translation for."""
        for routes in (None, {}, "", 5, [{"id": 1, "carriedResources": {"lumber": "x"}}]):
            try:
                read_trade_routes(self._one(routes))
            except MarketplaceModelInvalid:
                continue
            except Exception as exc:  # noqa: BLE001 - that is the point
                raise AssertionError(f"{routes!r} escaped as {type(exc).__name__}") from exc

    def test_an_empty_rows_list_is_still_a_real_answer(self):
        assert read_trade_routes(self._one([])) == []


class TestEveryTerminalStopSaysSo:
    """`stopped_early` has to be true wherever the run stopped short, including
    the earliest exit of all.

    Budget exhaustion at preflight returned `stopped_early=false` with a null
    reason. Nothing had been written, but the field is about whether the run
    finished the work it was given -- and false is the one answer that tells an
    automatic sweep to carry on.
    """

    def test_budget_exhausted_before_the_first_request_is_a_stop(self):
        from .test_distribution_execute import _FakeLiveSvc, _own_village_account, _run_live

        svc = _FakeLiveSvc(budget_ok=False)
        res = _run_live(svc, _own_village_account(), max_routes_per_run=50)

        assert res.stopped_early is True
        assert res.stop_reason and "budget" in res.stop_reason.lower()
        assert svc.created == [], "and nothing was written"


class TestUpdateOnlyProgressStillConverges:
    """A chunk that corrected cargo and deferred more is progress.

    Continuation was gated on CREATE attempts alone, so a cargo-only sweep got
    no next-chunk delay: the work was remembered, never asked for again, and the
    convergence could not finish.
    """

    def _sweep(self, cap: int):
        """A reconcile sweep, which is the mode continuation exists for."""
        from travian_api.services.distribution.allocation import Resource

        from .test_auto_executor_safety import TestAE05StaleCargoIsUnfinishedWork
        from .test_distribution_execute import _FakeLiveSvc, _fanned, _run_live

        drifted = {Resource.CROP: 9000}
        svc = _FakeLiveSvc(
            existing={
                20003: _fanned(20011, start_id=800, cargo=drifted)
                + _fanned(20012, start_id=900, cargo=drifted)
            }
        )
        return svc, _run_live(
            svc,
            TestAE05StaleCargoIsUnfinishedWork()._account(),
            update_drifted=True,
            max_routes_per_run=cap,
            reconcile_all_origins=True,
        )

    def test_a_capped_update_run_asks_for_another_chunk(self):
        svc, res = self._sweep(cap=1)

        assert len(svc.updated) == 1, "precondition: one update fired, one was capped"
        assert res.remaining >= 1, "precondition: work is still owed"
        assert res.next_chunk_wait_seconds, (
            "an update fired and another is deferred, so there is a next chunk to ask "
            "for; a null wait ends the sweep with the cargo still wrong"
        )

    def test_a_sweep_with_nothing_left_asks_for_no_further_chunk(self):
        """The other half: continuation must still terminate."""
        _svc, res = self._sweep(cap=50)

        assert res.remaining == 0
        assert res.next_chunk_wait_seconds is None


class TestAE07CrossProcessLease:
    """The durable half of "one live executor per account".

    `execute_lock` is an `asyncio.Lock`: it serialises one interpreter and
    nothing else, so the server on :80 and a debug server on :8001 could both
    read the marketplace before either wrote. A lease file's creation is atomic,
    so the exclusion survives the process boundary.
    """

    def _isolated(self, tmp_path):
        from travian_api.services import account_lease

        account_lease.LEASE_DIR = tmp_path
        return account_lease

    def test_a_second_holder_is_refused(self, tmp_path):
        al = self._isolated(tmp_path)

        # One `with`, three contexts: hold the lease, arm the expectation, then
        # try to take it again. The second acquisition raises on __enter__,
        # which is exactly what `pytest.raises` is here to catch.
        with (
            al.account_execute_lease("acct"),
            pytest.raises(al.AccountBusy),
            al.account_execute_lease("acct"),
        ):
            pass

    def test_the_refusal_names_the_holder_and_the_way_out(self, tmp_path):
        """An operator who cannot tell WHICH process has it, or how to clear a
        lease left by a crash, is stuck."""
        al = self._isolated(tmp_path)

        with (
            al.account_execute_lease("acct"),
            pytest.raises(al.AccountBusy) as caught,
            al.account_execute_lease("acct"),
        ):
            pass

        message = str(caught.value)
        assert "pid" in message
        assert "delete" in message

    def test_a_different_account_is_not_blocked(self, tmp_path):
        al = self._isolated(tmp_path)

        with al.account_execute_lease("acct-a"), al.account_execute_lease("acct-b"):
            pass  # two worlds are not one account

    def test_it_is_released_when_the_run_raises(self, tmp_path):
        """A run that failed still has to let the next one in, or one crash
        locks the account until the TTL expires."""
        al = self._isolated(tmp_path)

        with contextlib.suppress(RuntimeError), al.account_execute_lease("acct"):
            raise RuntimeError("the run blew up")

        with al.account_execute_lease("acct"):
            pass

    def test_a_lease_left_by_a_dead_process_is_taken_over_once_it_is_stale(self, tmp_path):
        """Staleness is by AGE. Asking whether the holder is alive would mean
        `os.kill(pid, 0)`, and on Windows that TERMINATES the process rather
        than probing it -- the check would kill the run it asked about."""
        al = self._isolated(tmp_path)
        stranded = al._lease_path("acct")
        al.LEASE_DIR.mkdir(parents=True, exist_ok=True)
        stranded.write_text('{"pid": 999999, "host": "gone", "started": 0}', encoding="utf-8")
        os.utime(stranded, (0, time.time() - al.LEASE_TTL_SECONDS - 60))

        with al.account_execute_lease("acct"):
            pass  # taken over

    def test_a_fresh_lease_is_not_treated_as_stale(self, tmp_path):
        al = self._isolated(tmp_path)
        held = al._lease_path("acct")
        al.LEASE_DIR.mkdir(parents=True, exist_ok=True)
        held.write_text('{"pid": 4242, "host": "other", "started": 0}', encoding="utf-8")

        with pytest.raises(al.AccountBusy), al.account_execute_lease("acct"):
            pass

    def test_the_lease_file_does_not_name_the_account(self, tmp_path):
        """It sits in a directory anyone on the machine can list, and the key is
        a server URL plus a login."""
        al = self._isolated(tmp_path)

        assert (
            "player@example.com"
            not in al._lease_path("https://ts2.example|player@example.com").name
        )


class TestTheStallGuardMeasuresTheWorkItAskedFor:
    """Server-side half of the browser fix, pinned where it can be.

    The guard used to compare aggregate counts across differently filtered
    chunks: village A holding one deferred route and the next chunk finishing
    village B both read as "1", so the sweep declared a stall and stopped before
    ever going back for A. Two different villages are not two failed attempts at
    the same work. `deferred_origins` is what lets the client compare like with
    like.
    """

    def test_the_response_names_the_villages_so_progress_is_comparable(self):
        from .test_auto_executor_safety import TestAE05StaleCargoIsUnfinishedWork

        _svc, res = TestAE05StaleCargoIsUnfinishedWork()._run(cap=1)

        assert res.deferred_origins, (
            "a count alone cannot distinguish 'the same village again' from "
            "'a different village with the same amount of work left'"
        )
