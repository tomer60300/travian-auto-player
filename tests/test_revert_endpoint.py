"""The undo endpoint itself. `plan_revert` was well covered; this was not.

`POST /distribution/routes/revert-plan` is the safety net for a live run, so its
failure modes matter more than most: it is the thing you reach for when something
has already gone into a real account. Two of them were wrong.
"""

import asyncio
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.exceptions import NetworkError
from travian_api.services.distribution import execution_trace
from travian_api.services.trade_route_service import (
    ExistingRoute,
    MarketplaceUnreadable,
    RouteActionResult,
    TradeRoutePayloadUnverified,
)
from travian_api.web.routes import distribution as dist

_USER = SimpleNamespace(id=1)


def _trace_with(origin: int, inventory: list[dict]) -> str:
    """Write a finished trace whose recorded pre-state is `inventory`."""
    trace = execution_trace.ExecutionTrace()
    trace.event("run_start", dry_run=False)
    trace.event("origin_read", origin=origin, inventory=inventory)
    trace.close(created=1)
    return trace.run_id


class _Svc:
    def __init__(self, now, *, disable=None, raises=None, confirm_after=None):
        self._now = now
        self._disable = disable
        self._raises = raises
        self._confirm_after = confirm_after
        self.calls: list[str] = []

    def origin_lock(self, vid):
        @contextlib.asynccontextmanager
        async def _cm():
            yield

        return _cm()

    async def list_existing_routes(self, vid, *, map_span=None):
        self.calls.append("read")
        return list(self._now)

    async def confirm_routes(self, vid, *, map_span=None):
        self.calls.append("confirm")
        if isinstance(self._confirm_after, Exception):
            raise self._confirm_after
        return list(self._confirm_after if self._confirm_after is not None else self._now)

    async def disable_routes(self, vid, routes, *, stop_check=None):
        self.calls.append("disable")
        if self._raises is not None:
            raise self._raises
        return self._disable


def _call(trace_id, svc, **kw):
    body = dist.RevertPlanRequest(trace_id=trace_id, origins=[20003], **kw)
    session = SimpleNamespace(trade_route_service=svc)
    return asyncio.run(dist.post_revert_plan(body, _USER, session))


class TestItDoesNotThrowAwayTheManualSteps:
    def test_live_writes_being_off_is_a_problem_not_a_500(self):
        # The automated half is unavailable; the half only a human can do is
        # still the entire point of the response.
        trace = _trace_with(20003, [])
        svc = _Svc(
            [ExistingRoute(555, 30540, active=True)],
            raises=TradeRoutePayloadUnverified("live writes are disabled"),
        )

        res = _call(trace, svc, apply_disable=True)

        assert res.must_delete_by_hand == {20003: [555]}, "the manual steps survive"
        assert any("STILL RUNNING" in p for p in res.problems)
        assert any("manual steps below still apply" in p for p in res.problems)
        assert res.disabled_now == {}

    def test_a_network_failure_on_the_disable_is_also_reported_not_raised(self):
        trace = _trace_with(20003, [])
        svc = _Svc([ExistingRoute(555, 30540, active=True)], raises=NetworkError("boom"))
        res = _call(trace, svc, apply_disable=True)
        assert res.must_delete_by_hand == {20003: [555]}
        assert any("STILL RUNNING" in p for p in res.problems)


class TestADisableIsConfirmedBeforeItIsClaimed:
    def test_a_confirmed_disable_is_reported_as_confirmed(self):
        trace = _trace_with(20003, [])
        created = ExistingRoute(555, 30540, active=True)
        svc = _Svc(
            [created],
            disable=RouteActionResult(20003, 0, 0, "disabled", "1 route(s)"),
            confirm_after=[ExistingRoute(555, 30540, active=False)],
        )

        res = _call(trace, svc, apply_disable=True)

        assert res.disabled_now == {20003: [555]}
        assert svc.calls == ["read", "disable", "confirm"]
        assert any("confirmed inert" in s for s in res.steps)
        assert res.problems == []

    def test_a_disable_the_game_ignored_is_not_claimed(self):
        trace = _trace_with(20003, [])
        svc = _Svc(
            [ExistingRoute(555, 30540, active=True)],
            disable=RouteActionResult(20003, 0, 0, "disabled", "1 route(s)"),
            confirm_after=[ExistingRoute(555, 30540, active=True)],  # still on
        )

        res = _call(trace, svc, apply_disable=True)

        assert res.disabled_now == {}, "an unverified disable must not be claimed"
        assert any("STILL RUNNING" in p for p in res.problems)

    def test_an_unreadable_confirmation_says_treat_them_as_running(self):
        trace = _trace_with(20003, [])
        svc = _Svc(
            [ExistingRoute(555, 30540, active=True)],
            disable=RouteActionResult(20003, 0, 0, "disabled", "1 route(s)"),
            confirm_after=MarketplaceUnreadable("soft block"),
        )

        res = _call(trace, svc, apply_disable=True)

        assert res.disabled_now == {}
        assert any("until you have looked" in p for p in res.problems)


class TestAnUnverifiedDisableStillGetsItsReadBack:
    """The verdict this endpoint exists to get right.

    `confirm_routes` sat inside `if result.status == "disabled"`, so a toggle
    whose 200 body could not be read SKIPPED the read entirely and fell to the
    else branch -- which reports the created routes as "STILL RUNNING". The one
    endpoint built to make an undo trustworthy asserted the opposite of the
    truth whenever the game had in fact switched them off. `unverified` is
    exactly the case a read-back answers, so it takes the same path a
    `disabled` does and the PAGE decides.
    """

    def test_rows_the_read_back_finds_off_are_claimed_as_disabled(self):
        trace = _trace_with(20003, [])
        svc = _Svc(
            [ExistingRoute(555, 30540, active=True)],
            disable=RouteActionResult(
                20003, 0, 0, "unverified", "disabling of 1 route(s) cannot be confirmed"
            ),
            confirm_after=[ExistingRoute(555, 30540, active=False)],
        )

        res = _call(trace, svc, apply_disable=True)

        assert svc.calls == ["read", "disable", "confirm"], "the read-back must not be skipped"
        assert res.disabled_now == {20003: [555]}
        assert any("confirmed inert" in s for s in res.steps)
        assert not any("STILL RUNNING" in p for p in res.problems), res.problems

    def test_rows_the_read_back_finds_on_are_still_reported(self):
        trace = _trace_with(20003, [])
        svc = _Svc(
            [ExistingRoute(555, 30540, active=True)],
            disable=RouteActionResult(
                20003, 0, 0, "unverified", "disabling of 1 route(s) cannot be confirmed"
            ),
            confirm_after=[ExistingRoute(555, 30540, active=True)],  # still on
        )

        res = _call(trace, svc, apply_disable=True)

        assert res.disabled_now == {}
        assert any("STILL RUNNING" in p for p in res.problems)

    def test_a_flat_refusal_never_reaches_the_read_back(self):
        """The other half: `failed` is the game saying no, and there is nothing
        to look at. It must stay in the else branch."""
        trace = _trace_with(20003, [])
        svc = _Svc(
            [ExistingRoute(555, 30540, active=True)],
            disable=RouteActionResult(20003, 0, 0, "failed", "1 of 1 route(s) rejected: [555]"),
        )

        res = _call(trace, svc, apply_disable=True)

        assert svc.calls == ["read", "disable"], "a refusal costs no extra request"
        assert res.disabled_now == {}
        assert any("STILL RUNNING" in p for p in res.problems)


class TestItRefusesRatherThanGuesses:
    def test_a_missing_trace_is_a_404(self):
        with pytest.raises(HTTPException) as caught:
            _call("aaaaaaaaaaaa", _Svc([]))
        assert caught.value.status_code == 404

    def test_a_trace_id_that_is_not_a_trace_id_is_rejected(self):
        # It is interpolated into a filename. Traversal was an authenticated
        # arbitrary-.jsonl read, and a wrong file makes every live route look
        # newly created -- which apply_disable would then switch off.
        from pydantic import ValidationError

        for bad in ("x/../../etc", "../evil", "AAAAAAAAAAAA", "short", "z" * 12):
            with pytest.raises(ValidationError):
                dist.RevertPlanRequest(trace_id=bad, origins=[1])

    def test_an_even_map_span_is_refused_as_the_plan_refuses_it(self):
        # A world is centred on 0|0, so its width is odd; an even span shifts
        # every tile index by half a field and silently skews every distance
        # derived from it. `/plan` has refused that from the start. The UNDO --
        # the one endpoint here that deletes rows for good, and the one that
        # needs correct tiles most -- took the same field and checked nothing.
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            dist.RevertPlanRequest(trace_id="a" * 12, map_span=400)

    def test_an_odd_span_is_still_accepted(self):
        assert dist.RevertPlanRequest(trace_id="a" * 12, map_span=401).map_span == 401

    def test_a_run_that_read_nothing_has_nothing_to_revert(self):
        trace = execution_trace.ExecutionTrace()
        trace.event("run_start")
        trace.close()
        with pytest.raises(HTTPException) as caught:
            _call(trace.run_id, _Svc([]))
        assert caught.value.status_code == 409

    def test_an_unchanged_village_is_reported_clean(self):
        row = {"route_id": 555, "dest": 30540, "active": True}
        trace = _trace_with(20003, [row])
        svc = _Svc([ExistingRoute(555, 30540, active=True)])

        res = _call(trace, svc)

        assert res.clean is True
        assert res.must_delete_by_hand == {}
        assert "nothing to revert" in " ".join(res.steps)


class TestItCanFinishTheUndoItself:
    """`delete_routes` was verified, tested — and had no production caller, while
    this endpoint told the operator to go and delete by hand. Deletion is now
    available here, still as its own opt-in, because it is the one irreversible
    step: a disabled route can be switched back on, a deleted one cannot."""

    def _svc(self, *, after_delete):
        created = ExistingRoute(555, 30540, active=True)
        svc = _Svc([created], disable=RouteActionResult(20003, 0, 0, "disabled", "1"))
        svc._confirm_after = [ExistingRoute(555, 30540, active=False)]
        svc._after_delete = after_delete

        async def _delete(vid, routes, *, stop_check=None):
            svc.calls.append("delete")
            svc._confirm_after = svc._after_delete
            return RouteActionResult(vid, 0, 0, "deleted", f"{len(routes)} route(s)")

        svc.delete_routes = _delete
        return svc

    def test_it_deletes_and_confirms_the_rows_are_gone(self):
        trace = _trace_with(20003, [])
        svc = self._svc(after_delete=[])  # the page comes back empty

        res = _call(trace, svc, apply_disable=True, apply_delete=True)

        assert res.deleted_now == {20003: [555]}
        assert res.must_delete_by_hand == {}, "nothing left for a person to do"
        assert any("confirmed gone" in s for s in res.steps)
        assert res.problems == []

    def test_the_disable_runs_before_the_delete(self):
        # Ordering is the contract: stop the resources moving first, because that
        # part is reversible and the removal is not.
        trace = _trace_with(20003, [])
        svc = self._svc(after_delete=[])
        _call(trace, svc, apply_disable=True, apply_delete=True)

        assert svc.calls.index("disable") < svc.calls.index("delete")

    def test_a_delete_the_game_ignored_is_not_claimed(self):
        trace = _trace_with(20003, [])
        # The rows survive the delete.
        svc = self._svc(after_delete=[ExistingRoute(555, 30540, active=False)])

        res = _call(trace, svc, apply_disable=True, apply_delete=True)

        assert res.deleted_now == {}
        assert any("STILL THERE" in p for p in res.problems)
        assert res.must_delete_by_hand == {20003: [555]}, "so it stays on the manual list"

    def test_an_unverifiable_delete_is_settled_by_the_read_back(self):
        # The endpoint re-reads the page one line later; refusing on the response
        # shape threw that away and told the operator to delete by hand rows the
        # game had already removed.
        trace = _trace_with(20003, [])
        svc = self._svc(after_delete=[])

        async def _delete(vid, routes, *, stop_check=None):
            svc.calls.append("delete")
            svc._confirm_after = svc._after_delete
            return RouteActionResult(vid, 0, 0, "unverified", "cannot be confirmed (test)")

        svc.delete_routes = _delete

        res = _call(trace, svc, apply_disable=True, apply_delete=True)

        assert res.deleted_now == {20003: [555]}
        assert res.must_delete_by_hand == {}
        assert res.problems == []

    def test_an_unverifiable_delete_that_left_the_rows_is_still_reported(self):
        trace = _trace_with(20003, [])
        svc = self._svc(after_delete=[ExistingRoute(555, 30540, active=False)])

        async def _delete(vid, routes, *, stop_check=None):
            svc.calls.append("delete")
            svc._confirm_after = svc._after_delete
            return RouteActionResult(vid, 0, 0, "unverified", "cannot be confirmed (test)")

        svc.delete_routes = _delete

        res = _call(trace, svc, apply_disable=True, apply_delete=True)

        assert res.deleted_now == {}
        assert any("STILL THERE" in p for p in res.problems)

    def test_without_the_opt_in_nothing_is_deleted(self):
        trace = _trace_with(20003, [])
        svc = self._svc(after_delete=[])

        res = _call(trace, svc, apply_disable=True)

        assert "delete" not in svc.calls
        assert res.deleted_now == {}
        assert res.must_delete_by_hand == {20003: [555]}

    def test_the_manual_instructions_no_longer_claim_the_app_cannot_delete(self):
        trace = _trace_with(20003, [])
        svc = _Svc([ExistingRoute(555, 30540, active=True)])

        res = _call(trace, svc)

        text = " ".join(res.steps)
        assert "no verified delete request" not in text
        assert "apply_delete" in text, "it should offer the option it now has"
