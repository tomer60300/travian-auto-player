"""Helpers for the resumable-operation WS pattern.

Every long-running WS handler that delegates to :class:`OperationManager`
has the same plumbing once the op is spawned: register the WS in
``ws_manager``, subscribe to the exec session, replay history, forward
``{"action": "stop"}`` to the op, and tail live messages until the op ends
or the WS dies.

Pulling that into one place keeps each handler focused on what it actually
*does* — the operation coro factory and its config parsing — instead of
re-implementing identical glue.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket, WebSocketDisconnect

from travian_api.operation_manager import operation_manager
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)


async def _send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def forward_stop_to_op(websocket: WebSocket, session_id: str) -> None:
    """Forward ``{"action": "stop"}`` from the client to the running op.

    Survives JSON parse errors and ignores unknown actions so a noisy client
    can't tear the listener down. Returns when the WS dies.
    """
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(msg, dict) and msg.get("action") == "stop":
                operation_manager.request_stop(session_id)
                return
    except (WebSocketDisconnect, RuntimeError):
        # Client gone — do NOT stop the op, just exit. The op continues
        # server-side and the user can reconnect via /ws/sessions/{id}/stream.
        return


async def subscribe_and_tail(
    websocket: WebSocket,
    user_id: int,
    channel: str,
    session_id: str,
) -> None:
    """Subscribe to *session_id*, replay history, forward stops, tail live.

    Caller is responsible for the auth / policy / op-spawn dance leading up
    to this. Caller has already accepted the websocket. This function owns
    the connect/disconnect into ``ws_manager`` and the subscribe/unsubscribe
    into ``exec_session_manager``. WS death does not stop the op.
    """
    # The starter handler already called websocket.accept() to read the
    # config; calling accept() a second time would crash Starlette.
    await ws_manager.connect(websocket, user_id, channel, accept=False)
    sub_id = id(websocket)
    sub = exec_session_manager.subscribe(session_id, sub_id)
    if sub is None:
        # Race: op finished before we could subscribe. Tell the client.
        await _send(
            websocket,
            {"type": "operation_complete", "status": "completed"},
        )
        await ws_manager.disconnect(user_id, channel, websocket)
        return

    history, queue = sub

    # Replay buffered history first so the client sees session_init + any
    # already-pushed messages before live ones.
    for past in history:
        if not await _send(websocket, past):
            exec_session_manager.unsubscribe(session_id, sub_id)
            await ws_manager.disconnect(user_id, channel, websocket)
            return

    # Race guard: the op may have terminated between operation_manager.start
    # and our subscribe() call (fast no-op coros, validation-only paths).
    # mark_disconnected has already fanned its None sentinel to existing
    # subscribers, so this newcomer would never receive one and queue.get()
    # would block forever. The history we just replayed already contains
    # the real operation_complete frame (OperationManager._run always
    # pushes it before mark_disconnected); we must NOT synthesize another
    # — that would clobber a real failed/stopped terminal with completed.
    session = exec_session_manager.get(session_id)
    if session is not None and session.status == "disconnected":
        exec_session_manager.unsubscribe(session_id, sub_id)
        await ws_manager.disconnect(user_id, channel, websocket)
        return

    stop_listener = asyncio.create_task(
        forward_stop_to_op(websocket, session_id),
        name=f"stop-listener:{session_id}",
    )

    logger.info(
        "Resumable WS subscriber attached: user=%s channel=%s session=%s sub_id=%s",
        user_id,
        channel,
        session_id,
        sub_id,
    )
    sent_count = 0
    try:
        while True:
            data = await queue.get()
            if data is None:
                # Op terminated; ExecutionSession marked disconnected.
                logger.info(
                    "Resumable WS subscriber: None sentinel received "
                    "(op terminated). session=%s sub_id=%s sent=%d",
                    session_id,
                    sub_id,
                    sent_count,
                )
                break
            if not await _send(websocket, data):
                logger.warning(
                    "Resumable WS subscriber: send_json failed (client gone). "
                    "session=%s sub_id=%s sent=%d msg_type=%s",
                    session_id,
                    sub_id,
                    sent_count,
                    data.get("type") if isinstance(data, dict) else "?",
                )
                break
            sent_count += 1
    except (WebSocketDisconnect, RuntimeError) as exc:
        logger.info(
            "Resumable WS subscriber disconnect: session=%s sub_id=%s sent=%d reason=%s",
            session_id,
            sub_id,
            sent_count,
            type(exc).__name__,
        )
    except Exception:
        logger.exception(
            "Resumable WS tail error: user=%s channel=%s session=%s sub_id=%s sent=%d",
            user_id,
            channel,
            session_id,
            sub_id,
            sent_count,
        )
    finally:
        stop_listener.cancel()
        try:
            await stop_listener
        except (asyncio.CancelledError, Exception):
            pass
        exec_session_manager.unsubscribe(session_id, sub_id)
        await ws_manager.disconnect(user_id, channel, websocket)
        logger.info(
            "Resumable WS subscriber detached: session=%s sub_id=%s total_sent=%d",
            session_id,
            sub_id,
            sent_count,
        )
