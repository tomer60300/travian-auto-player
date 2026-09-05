"""Shutdown stops the operations before it closes the clients they are using.

``lifespan`` called ``session_manager.disconnect_all()``, which closes every
``HttpClient`` and saves its cookie jar. ``operation_manager`` was never
imported by ``app.py`` at all, so ``OperationManager._ops`` was not iterated:
running tasks were neither signalled nor awaited. A build queue or a live
``/execute`` mid-request when uvicorn takes SIGINT therefore outlived its
client and fired its next game request into a closed one, while ``_save_cookies``
ran against a jar a request might still have been mutating.

The codebase already knew the hazard from the other direction:
``session_manager.disconnect()`` refuses a *user-initiated* disconnect with a
409 while operations are running. ``disconnect_all()`` had no such guard, and on
shutdown refusing is not an option -- stopping and waiting is.

So: signal every operation, wait for the tasks with a timeout, and only then
close the clients. A straggler past the timeout is logged by name rather than
waited on forever, because a shutdown that does not finish is its own failure.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.operation_manager import Operation, OperationManager


def _op(manager, session_id, task, stop_event):
    op = Operation(
        session_id=session_id,
        user_id=1,
        label=f"label-{session_id}",
        task=task,
        stop_event=stop_event,
        started_at=0.0,
    )
    manager._ops[session_id] = op
    return op


def test_every_running_operation_is_signalled_and_awaited():
    async def run():
        manager = OperationManager()
        stopped: list[str] = []

        async def body(name, event):
            await event.wait()
            stopped.append(name)

        for name in ("a", "b"):
            event = asyncio.Event()
            _op(manager, name, asyncio.create_task(body(name, event)), event)

        stragglers = await manager.stop_all(grace_seconds=5.0)
        return stopped, stragglers

    stopped, stragglers = asyncio.run(run())
    assert sorted(stopped) == ["a", "b"]
    assert stragglers == []


def test_an_operation_that_ignores_the_signal_is_reported_not_waited_on():
    async def run():
        manager = OperationManager()

        async def deaf():
            await asyncio.sleep(60)

        event = asyncio.Event()
        task = asyncio.create_task(deaf())
        _op(manager, "stubborn", task, event)

        stragglers = await manager.stop_all(grace_seconds=0.05)
        task.cancel()
        return stragglers, event.is_set()

    stragglers, signalled = asyncio.run(run())
    assert signalled, "it must still have been asked to stop"
    assert stragglers == ["label-stubborn:stubborn"]


def test_stopping_nothing_is_free():
    assert asyncio.run(OperationManager().stop_all()) == []


def test_an_already_finished_operation_is_not_a_straggler():
    async def run():
        manager = OperationManager()

        async def done():
            return None

        task = asyncio.create_task(done())
        await task
        _op(manager, "finished", task, asyncio.Event())
        return await manager.stop_all(grace_seconds=0.05)

    assert asyncio.run(run()) == []


@pytest.mark.asyncio
async def test_the_lifespan_stops_operations_before_it_closes_the_clients(monkeypatch):
    """Ordering is the whole point: closing first is what broke the in-flight op."""
    import travian_api.web.app as app_module
    from travian_api.debug_dump import debug_dumper
    from travian_api.web.execution_sessions import exec_session_manager
    from travian_api.web.routes import recon as recon_module
    from travian_api.web.sessions import session_manager

    order: list[str] = []

    async def noop_init_db():
        return None

    async def fake_stop_all(grace_seconds: float = 10.0):
        order.append("stop_all")
        return []

    async def fake_disconnect_all():
        order.append("disconnect_all")

    async def fake_load(_db):
        return False

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(app_module, "init_db", noop_init_db)
    monkeypatch.setattr(app_module.operation_manager, "stop_all", fake_stop_all)
    monkeypatch.setattr(session_manager, "disconnect_all", fake_disconnect_all)
    monkeypatch.setattr(recon_module, "load_stored_credentials", fake_load)
    monkeypatch.setattr("travian_api.web.models.db.async_session_factory", _Db)
    monkeypatch.setattr(exec_session_manager, "start_cleanup", lambda: None)
    monkeypatch.setattr(exec_session_manager, "stop_cleanup", lambda: None)
    monkeypatch.setattr(debug_dumper, "start_cleanup", lambda: None)
    monkeypatch.setattr(debug_dumper, "stop_cleanup", lambda: None)
    monkeypatch.setattr(
        "travian_api.services.recon_account.recon_account_manager",
        SimpleNamespace(shutdown=lambda: asyncio.sleep(0)),
    )

    async with app_module.lifespan(app_module.app):
        pass

    assert order == ["stop_all", "disconnect_all"]
