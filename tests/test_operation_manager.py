"""Tests for the detached operation manager.

These exercise the lifecycle of a managed background task: spawn, normal
completion, explicit stop, exception path, and the find/list helpers.
A throwaway TravianSession stand-in is fine — OperationManager only
passes it through to the op coroutine.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.operation_manager import (
    COMPLETED,
    FAILED,
    RUNNING,
    STOPPED,
    OperationContext,
    OperationManager,
)

# Importing classes alphabetised; the local imports below are used in tests.
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.operation_gate import active_ops, captcha_stop


@pytest.fixture
def fresh_manager(monkeypatch):
    """Each test gets its own manager with a clean active_ops state.

    start() now refuses to spawn against a session that is not the one
    installed in session_manager (the disconnect-during-startup guard). These
    unit tests pass throwaway sessions, so the fixture models production —
    where the session handed to start() IS the installed one — by recording
    each start()'s session as installed and pointing session_manager.get at
    that record. Tests that want to exercise the guard's refusal patch get()
    themselves.
    """
    from travian_api.web import sessions as sessions_mod

    installed: dict[int, object] = {}
    monkeypatch.setattr(sessions_mod.session_manager, "get", installed.get)

    mgr = OperationManager()
    _orig_start = mgr.start

    def _start(*args, **kwargs):
        if "user_id" in kwargs and "session" in kwargs:
            installed[kwargs["user_id"]] = kwargs["session"]
        return _orig_start(*args, **kwargs)

    mgr.start = _start
    yield mgr
    # Cleanup: best-effort cancel any leftover tasks so a flaky test doesn't
    # leak running tasks into the next one.
    for op in list(mgr._ops.values()):
        op.task.cancel()


@pytest.mark.asyncio
async def test_start_refuses_a_session_torn_down_mid_startup(monkeypatch):
    """The WS-handler race: a session was fetched, the client config awaited,
    and /disconnect popped and closed the session in that gap. start() must
    refuse rather than spawn a detached op against the dead HttpClient."""
    from travian_api.web import sessions as sessions_mod

    # session_manager reports a DIFFERENT (or no) session than the one passed.
    monkeypatch.setattr(sessions_mod.session_manager, "get", lambda uid: None)
    mgr = OperationManager()

    ran = False

    async def coro(ctx: OperationContext) -> None:
        nonlocal ran
        ran = True

    op = mgr.start(
        user_id=99,
        label="doomed",
        session_type="test",
        session_label="Test",
        session=SimpleNamespace(),
        coro=coro,
    )

    assert op is None, "start must refuse when the session is no longer installed"
    assert "doomed" not in active_ops.get_active(99)
    await asyncio.sleep(0)
    assert ran is False, "the coroutine must never have been scheduled"


@pytest.mark.asyncio
async def test_start_runs_coro_to_completion(fresh_manager):
    pushed = []

    async def coro(ctx: OperationContext) -> None:
        ctx.push({"type": "log", "message": "hello"})

    op = fresh_manager.start(
        user_id=42,
        label="test-op",
        session_type="test",
        session_label="Test",
        session=SimpleNamespace(),
        coro=coro,
    )
    await op.task

    # Status flipped to COMPLETED and op was unregistered from the manager.
    assert op.status == COMPLETED
    assert fresh_manager.get(op.session_id) is None

    # Buffered messages: session_init + log + operation_complete (+ pushed history).
    session = exec_session_manager.get(op.session_id)
    assert session is not None
    types = [m.get("type") for m in session.messages]
    assert types[0] == "session_init"
    assert "log" in types
    assert types[-1] == "operation_complete"
    assert session.messages[-1]["status"] == COMPLETED
    # ExecutionSession status flipped to disconnected on terminal.
    assert session.status == "disconnected"
    pushed.append(types)


@pytest.mark.asyncio
async def test_should_stop_via_event(fresh_manager):
    sentinel = []

    async def coro(ctx: OperationContext) -> None:
        # Spin until stop is requested. The wait is interruptible.
        while not ctx.should_stop():
            stopped = await ctx.wait_or_stop(0.05)
            if stopped:
                break
        sentinel.append("clean-exit")

    op = fresh_manager.start(
        user_id=43,
        label="stoppable",
        session_type="test",
        session_label="Stoppable",
        session=SimpleNamespace(),
        coro=coro,
    )
    # Give the coro a beat to enter its loop.
    await asyncio.sleep(0.02)
    assert op.status == RUNNING

    assert fresh_manager.request_stop(op.session_id) is True
    await op.task

    # Graceful exit after stop request → terminal status reflects user
    # intent so callers can distinguish cancellation from natural completion.
    assert op.status == STOPPED
    assert sentinel == ["clean-exit"]


@pytest.mark.asyncio
async def test_should_stop_via_captcha_signal(fresh_manager):
    """captcha_stop signals raised after op.start are visible inside the coro."""

    async def coro(ctx: OperationContext) -> None:
        # Loop with a tight poll so we react quickly to captcha_stop.
        for _ in range(200):
            if ctx.should_stop():
                return
            await asyncio.sleep(0.01)

    op = fresh_manager.start(
        user_id=44,
        label="captcha-aware",
        session_type="test",
        session_label="Captcha",
        session=SimpleNamespace(),
        coro=coro,
    )
    await asyncio.sleep(0.02)
    captcha_stop.signal(44)

    await op.task
    # Captcha-driven exit also counts as STOPPED — the op was halted, not
    # naturally completed.
    assert op.status == STOPPED
    captcha_stop.clear(44)


@pytest.mark.asyncio
async def test_exception_sets_failed_and_pushes_error(fresh_manager):
    async def coro(ctx: OperationContext) -> None:
        raise RuntimeError("kaboom")

    op = fresh_manager.start(
        user_id=45,
        label="fails",
        session_type="test",
        session_label="Fails",
        session=SimpleNamespace(),
        coro=coro,
    )
    await op.task

    assert op.status == FAILED
    session = exec_session_manager.get(op.session_id)
    msgs = [m for m in session.messages if m.get("type") == "error"]
    assert msgs and msgs[0]["fatal"] is True
    assert "kaboom" in msgs[0]["message"]


@pytest.mark.asyncio
async def test_cancelled_task_marks_stopped(fresh_manager):
    async def coro(ctx: OperationContext) -> None:
        await asyncio.sleep(5)

    op = fresh_manager.start(
        user_id=46,
        label="long-runner",
        session_type="test",
        session_label="Long",
        session=SimpleNamespace(),
        coro=coro,
    )
    await asyncio.sleep(0.01)
    op.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await op.task

    assert op.status == STOPPED


@pytest.mark.asyncio
async def test_find_by_label_and_list_for_user(fresh_manager):
    async def coro(ctx: OperationContext) -> None:
        await asyncio.sleep(0.5)

    op_a = fresh_manager.start(
        user_id=47,
        label="alpha",
        session_type="test",
        session_label="A",
        session=SimpleNamespace(),
        coro=coro,
    )
    op_b = fresh_manager.start(
        user_id=47,
        label="beta",
        session_type="test",
        session_label="B",
        session=SimpleNamespace(),
        coro=coro,
    )

    assert fresh_manager.find_by_label(47, "alpha") is op_a
    assert fresh_manager.find_by_label(47, "beta") is op_b
    assert fresh_manager.find_by_label(47, "gamma") is None

    listed = fresh_manager.list_for_user(47)
    assert {o.session_id for o in listed} == {op_a.session_id, op_b.session_id}
    assert fresh_manager.list_for_user(99) == []


@pytest.mark.asyncio
async def test_request_stop_unknown_id_returns_false(fresh_manager):
    assert fresh_manager.request_stop("nonexistent") is False


@pytest.mark.asyncio
async def test_unregisters_active_ops_on_completion(fresh_manager):
    async def coro(ctx: OperationContext) -> None:
        return

    op = fresh_manager.start(
        user_id=48,
        label="register-cleanup",
        session_type="test",
        session_label="X",
        session=SimpleNamespace(),
        coro=coro,
    )
    # While running it is registered.
    assert "register-cleanup" in active_ops.get_active(48)
    await op.task
    assert "register-cleanup" not in active_ops.get_active(48)


@pytest.mark.asyncio
async def test_extra_labels_are_reserved_and_released(fresh_manager):
    """run-all-style ops reserve per-resource labels alongside the primary."""

    async def coro(ctx: OperationContext) -> None:
        return

    op = fresh_manager.start(
        user_id=49,
        label="farm-all",
        extra_labels=["farm:101", "farm:102"],
        session_type="test",
        session_label="Y",
        session=SimpleNamespace(),
        coro=coro,
    )
    # Both primary and extras are visible immediately.
    active = set(active_ops.get_active(49))
    assert {"farm-all", "farm:101", "farm:102"} <= active

    await op.task
    # Cleanup releases everything.
    assert active_ops.get_active(49) == []


@pytest.mark.asyncio
async def test_extra_labels_released_on_failure(fresh_manager):
    async def coro(ctx: OperationContext) -> None:
        raise RuntimeError("boom")

    op = fresh_manager.start(
        user_id=50,
        label="farm-all",
        extra_labels=["farm:200"],
        session_type="test",
        session_label="Z",
        session=SimpleNamespace(),
        coro=coro,
    )
    await op.task
    # Even on failure, no labels leak.
    assert active_ops.get_active(50) == []


@pytest.mark.asyncio
async def test_pushed_fatal_error_marks_failed(fresh_manager):
    """Coros that push {type:'error', fatal:True} and return → FAILED."""

    async def coro(ctx: OperationContext) -> None:
        ctx.push({"type": "error", "message": "budget exhausted", "fatal": True})
        # Some coros also push a final status frame; we still want FAILED.
        ctx.push({"type": "status", "data": {"state": "stopped"}})

    op = fresh_manager.start(
        user_id=51,
        label="fatal-no-raise",
        session_type="test",
        session_label="Fatal",
        session=SimpleNamespace(),
        coro=coro,
    )
    await op.task
    assert op.status == FAILED
    session = exec_session_manager.get(op.session_id)
    assert session.messages[-1]["type"] == "operation_complete"
    assert session.messages[-1]["status"] == FAILED


@pytest.mark.asyncio
async def test_atomic_unique_label_blocks_concurrent_start(fresh_manager):
    """Second start() with require_unique_label=True returns None."""

    async def long_running(ctx: OperationContext) -> None:
        await asyncio.sleep(0.5)

    op_a = fresh_manager.start(
        user_id=52,
        label="singleton",
        session_type="test",
        session_label="A",
        session=SimpleNamespace(),
        coro=long_running,
        require_unique_label=True,
    )
    assert op_a is not None

    op_b = fresh_manager.start(
        user_id=52,
        label="singleton",
        session_type="test",
        session_label="B",
        session=SimpleNamespace(),
        coro=long_running,
        require_unique_label=True,
    )
    assert op_b is None  # rejected — duplicate
