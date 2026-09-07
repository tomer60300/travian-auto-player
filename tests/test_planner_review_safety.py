"""Run-specific undo must never absorb changes from a later run."""

import pytest

from travian_api.services.distribution.route_revert import plan_recorded_revert


def row(rid, *, active=True, cargo=100):
    return dict(route_id=rid, dest=7, active=active, cargo={"lumber": cargo}, dispatch_minute=60)


def test_later_created_routes_are_not_selected_by_undo():
    plan = plan_recorded_revert(1, [row(10)], [row(10), row(20)], [row(10), row(20), row(30)], {20})
    assert plan.manual_delete_ids == [20]


def test_later_enabled_changes_are_not_attributed_to_earlier_run():
    plan = plan_recorded_revert(
        1, [row(10)], [row(10), row(20)], [row(10, active=False), row(20)], {20}
    )
    assert plan.to_restore == []


def test_modified_created_route_refuses_automatic_undo():
    with pytest.raises(ValueError, match="changed since"):
        plan_recorded_revert(1, [], [row(20)], [row(20, cargo=200)], {20})


def test_unattributed_new_row_is_not_selected():
    plan = plan_recorded_revert(1, [], [row(20), row(30)], [row(20), row(30)], {20})
    assert plan.manual_delete_ids == [20]


def test_already_deleted_created_route_needs_no_action():
    assert plan_recorded_revert(1, [], [row(20)], [], {20}).is_clean


def test_preexisting_cargo_update_requires_manual_restoration():
    plan = plan_recorded_revert(1, [row(10)], [row(10, cargo=200)], [row(10, cargo=200)], set())
    assert not plan.is_clean


@pytest.mark.parametrize("user_id, account", [(2, "world-a"), (1, "world-b"), (1, None)])
def test_foreign_or_unknown_identity_cannot_read_undo_events(user_id, account):
    from travian_api.services.distribution import execution_trace

    trace = execution_trace.ExecutionTrace()
    trace.event("run_start", user=1, account="world-a")
    trace.close()
    with pytest.raises(FileNotFoundError):
        execution_trace.read_owned_events(trace.run_id, user_id, account)


def test_legacy_trace_without_identity_is_not_automatically_revertible():
    from travian_api.services.distribution import execution_trace

    trace = execution_trace.ExecutionTrace()
    trace.event("run_start", user=1)
    trace.close()
    with pytest.raises(FileNotFoundError):
        execution_trace.read_owned_events(trace.run_id, 1, "world-a")


def test_history_filters_account_and_user_before_limit():
    from travian_api.services.distribution import execution_trace
    from travian_api.services.distribution.run_history import summarise_runs

    owned = execution_trace.ExecutionTrace()
    owned.event("run_start", user=1, account="owned")
    owned.close(created=2)
    for user, account in [(2, "owned"), (1, "foreign"), (1, None)]:
        trace = execution_trace.ExecutionTrace()
        trace.event("run_start", user=user, account=account)
        trace.close(created=99)
    history = summarise_runs(execution_trace.TRACE_DIR, limit=1, owner=(1, "owned"))
    assert [r.run_id for r in history.runs] == [owned.run_id]
    assert history.rollup.total_created == 2
    assert not summarise_runs(execution_trace.TRACE_DIR, owner=(1, None)).runs


def test_endpoint_ignores_a_later_runs_created_route():
    from tests.test_revert_endpoint import _call, _Svc, _trace_with
    from travian_api.services.trade_route_service import ExistingRoute

    trace = _trace_with(20003, [])
    svc = _Svc([ExistingRoute(555, 30540), ExistingRoute(999, 30540)])
    result = _call(trace, svc)
    assert result.created == {20003: [555]}
    assert result.must_delete_by_hand == {20003: [555]}


def test_zero_create_budget_does_not_reenable_disabled_desired_routes():
    from tests.test_distribution_execute import (
        _FOREIGN_REAL_ID,
        _FakeLiveSvc,
        _fanned,
        _run_live,
        _two_origin_account,
    )

    svc = _FakeLiveSvc(existing={20003: _fanned(_FOREIGN_REAL_ID, 40, 40, active=False)})
    _run_live(svc, _two_origin_account(), max_routes_per_run=0)
    assert svc.enabled == []
    assert svc.created == []


def test_unstable_post_run_read_is_not_an_undo_certificate():
    from tests.test_revert_endpoint import _call, _session, _Svc
    from travian_api.services.distribution import execution_trace

    trace = execution_trace.ExecutionTrace()
    trace.event("run_start", user=1, account=execution_trace.account_identity(_session(None)))
    trace.event("origin_read", origin=20003, inventory=[])
    trace.event("verified", origin=20003, rows=[row(555)], undo_created_ids=[555])
    trace.event("read_back_disagreed", origin=20003)
    trace.close()
    svc = _Svc([])
    result = _call(trace.run_id, svc, apply_delete=True)
    assert result.problems
    assert not result.clean
    assert svc.calls == []


def test_routes_already_gone_at_disable_confirmation_need_no_delete():
    from tests.test_revert_endpoint import _call, _Svc, _trace_with
    from travian_api.services.trade_route_service import ExistingRoute, RouteActionResult

    trace = _trace_with(20003, [])
    svc = _Svc(
        [ExistingRoute(555, 30540)],
        disable=RouteActionResult(20003, 0, 0, "disabled", ""),
        confirm_after=[],
    )
    result = _call(trace, svc, apply_delete=True)
    assert result.clean
    assert not any(result.must_delete_by_hand.values())
    assert svc.calls == ["read", "disable", "confirm"]


def test_execution_writes_an_account_bound_undo_record(monkeypatch):
    from tests.test_distribution_execute import (
        _FakeLiveSvc,
        _run_live,
        _SessMgr,
        _two_origin_account,
    )
    from tests.test_revert_endpoint import _call, _session, _Svc
    from travian_api.services.distribution import execution_trace

    monkeypatch.setattr(_SessMgr, "get", lambda self, uid: _session(self._svc))
    svc = _FakeLiveSvc()
    result = _run_live(svc, _two_origin_account(), max_routes_per_run=1)
    events = execution_trace.read_owned_events(
        result.trace_id,
        1,
        execution_trace.account_identity(_session(svc)),
    )
    verified = [e for e in events if e.get("kind") == "verified"]
    assert verified
    assert any(e["undo_created_ids"] for e in verified)
    for event in verified:
        assert set(event["undo_created_ids"]) <= {r["route_id"] for r in event["rows"]}
    undo = _call(result.trace_id, _Svc(svc._existing.get(20003, [])))
    assert not undo.problems
    assert undo.created[20003]


def test_undo_endpoint_refuses_another_user_before_game_reads():
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException

    from tests.test_revert_endpoint import _session, _Svc, _trace_with
    from travian_api.web.routes import distribution as dist

    trace = _trace_with(20003, [])
    svc = _Svc([])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            dist.post_revert_plan(
                dist.RevertPlanRequest(trace_id=trace, apply_delete=True),
                SimpleNamespace(id=2),
                _session(svc),
            )
        )
    assert caught.value.status_code == 404
    assert svc.calls == []


def test_history_endpoint_binds_the_authenticated_user_and_session(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from tests.test_revert_endpoint import _session, _trace_with
    from travian_api.web.routes import distribution as dist

    trace = _trace_with(20003, [])
    monkeypatch.setattr(dist.session_manager, "get", lambda uid: _session(None))
    owned = asyncio.run(dist.get_run_history(limit=1, _user=SimpleNamespace(id=1)))
    assert owned.runs[0].run_id == trace
    foreign = asyncio.run(dist.get_run_history(limit=1, _user=SimpleNamespace(id=2)))
    assert foreign.runs == []
