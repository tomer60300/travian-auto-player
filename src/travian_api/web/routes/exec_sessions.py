"""REST + WebSocket routes for remote execution session mirroring.

REST:
    GET  /api/sessions          — list the current user's execution sessions
    POST /api/sessions/stop-all — signal all active operations to stop

WebSocket:
    WS /ws/sessions/{session_id}/stream?token=<JWT> — subscribe to a session's
    live output (sends history on connect, then streams new messages).
    Also accepts ``{"action": "stop"}`` from the client to halt the
    underlying operation, enabling cross-device control.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from travian_api.operation_manager import operation_manager
from travian_api.web.auth import get_current_user
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.operation_gate import active_ops, captcha_stop
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# REST: list sessions
# ---------------------------------------------------------------------------


@router.get("/api/sessions")
async def list_sessions(user=Depends(get_current_user)):
    """Return the caller's execution sessions (running + disconnected <24h)."""
    return exec_session_manager.list_for_user(user.id)


@router.get("/api/__diag/sessions")
async def diag_sessions(user=Depends(get_current_user)):
    """Diagnostic snapshot of this user's execution sessions.

    Returns subscriber counts, message-count, status, etc. — meant to be
    cross-referenced against the client-side `__resumableDebug.history()`
    when the page appears to hang. Read-only, no side effects.
    """
    sessions = exec_session_manager.list_for_user(user.id)
    enriched = []
    for s in sessions:
        sess = exec_session_manager.get(s["id"])
        sub_count = len(sess._subscribers) if sess is not None else 0
        last_msg_ts = None
        last_msg_type = None
        if sess is not None and sess.messages:
            last = sess.messages[-1]
            if isinstance(last, dict):
                last_msg_ts = last.get("ts")
                last_msg_type = last.get("type")
        enriched.append({
            **s,
            "subscriber_count": sub_count,
            "last_message_ts": last_msg_ts,
            "last_message_type": last_msg_type,
        })
    return {"sessions": enriched, "user_id": user.id}


@router.post("/api/sessions/stop-all")
async def stop_all_sessions(user=Depends(get_current_user)):
    """Signal all active operations for this user to stop gracefully."""
    captcha_stop.signal(user.id)
    active = active_ops.get_active(user.id)
    logger.info("Stop-all requested by user %s, active ops: %s", user.id, active)
    return {"stopped": True, "active_operations": active}


@router.post("/api/sessions/{session_id}/stop")
async def stop_one_session(session_id: str, user=Depends(get_current_user)):
    """Signal a single operation to stop gracefully.

    The bulk ``/stop-all`` is appropriate for "kill switch" UX. This
    endpoint lets the per-session detail view target one op without
    collateral. Returns 404 when the session doesn't exist or doesn't
    belong to the caller.
    """
    session = exec_session_manager.get(session_id)
    if session is None or session.user_id != user.id:
        # FastAPI does not honor Flask-style ``return body, status_code``
        # tuples — raise HTTPException so the client actually sees 404.
        raise HTTPException(status_code=404, detail="Session not found")
    ok = operation_manager.request_stop(session_id)
    logger.info(
        "Stop requested for session %s by user %s (op_found=%s)",
        session_id, user.id, ok,
    )
    return {"stopped": True, "op_running": ok, "session_id": session_id}


# ---------------------------------------------------------------------------
# WebSocket: stream session output
# ---------------------------------------------------------------------------


@router.websocket("/ws/sessions/{session_id}/stream")
async def session_stream_ws(websocket: WebSocket, session_id: str):
    """Subscribe to an execution session's message stream.

    Protocol:
        1. Authenticate via JWT query param.
        2. Verify session exists and belongs to the calling user.
        3. Send session_meta + full history.
        4. If session is already disconnected, send session_ended and close.
        5. Otherwise stream live messages until the session ends or the
           client disconnects.
    """
    user_id = await ws_manager.authenticate(websocket, require_travian_session=False)
    if user_id is None:
        return

    session = exec_session_manager.get(session_id)
    if session is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Session not found or expired"})
        await websocket.close(code=4004)
        return

    if session.user_id != user_id:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Access denied"})
        await websocket.close(code=4003)
        return

    channel = f"session_stream_{session_id}"
    await ws_manager.connect(websocket, user_id, channel)

    sub_id = id(websocket)
    result = exec_session_manager.subscribe(session_id, sub_id)
    if result is None:
        logger.warning(
            "session_stream subscriber: subscribe() returned None — session vanished. "
            "user=%s session=%s", user_id, session_id,
        )
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await ws_manager.disconnect(user_id, channel, websocket)
        return

    history, queue = result
    logger.info(
        "session_stream subscriber attached: user=%s session=%s sub_id=%s "
        "session.status=%s history_len=%d",
        user_id, session_id, sub_id, session.status, len(history),
    )

    stop_listener: asyncio.Task | None = None
    sent_count = 0

    try:
        # Send metadata
        await websocket.send_json(
            {
                "type": "session_meta",
                "id": session.id,
                "session_type": session.session_type,
                "label": session.label,
                "status": session.status,
                "created_at": session.created_at,
            }
        )
        sent_count += 1

        # Send full history
        await websocket.send_json({"type": "history", "messages": history})
        sent_count += 1

        # If session already ended, tell the client and close gracefully
        if session.status == "disconnected":
            await websocket.send_json({"type": "session_ended"})
            sent_count += 1
            logger.info(
                "session_stream subscriber: session already disconnected, sent "
                "session_ended and closing. user=%s session=%s sub_id=%s sent=%d",
                user_id, session_id, sub_id, sent_count,
            )
            return

        # Cross-device stop: a client connected to the stream can ask the
        # underlying operation to halt without touching the original WS.
        stop_listener = asyncio.create_task(
            _listen_for_stop(websocket, session_id),
            name=f"session-stream-stop:{session_id}",
        )

        # Stream live messages until session ends or client disconnects
        while True:
            data = await queue.get()
            if data is None:
                # None sentinel = session ended
                await websocket.send_json({"type": "session_ended"})
                sent_count += 1
                logger.info(
                    "session_stream subscriber: None sentinel received, sent "
                    "session_ended. session=%s sub_id=%s sent=%d",
                    session_id, sub_id, sent_count,
                )
                break
            await websocket.send_json({"type": "message", "data": data})
            sent_count += 1

    except WebSocketDisconnect:
        logger.info(
            "session_stream subscriber: WebSocketDisconnect. "
            "session=%s sub_id=%s sent=%d",
            session_id, sub_id, sent_count,
        )
    except Exception:
        logger.exception(
            "Error in session stream WS: session=%s user=%s sub_id=%s sent=%d",
            session_id, user_id, sub_id, sent_count,
        )
    finally:
        if stop_listener is not None:
            stop_listener.cancel()
            try:
                await stop_listener
            except (asyncio.CancelledError, Exception):
                pass
        exec_session_manager.unsubscribe(session_id, sub_id)
        await ws_manager.disconnect(user_id, channel, websocket)
        logger.info(
            "session_stream subscriber detached. session=%s sub_id=%s total_sent=%d",
            session_id, sub_id, sent_count,
        )


async def _listen_for_stop(websocket: WebSocket, session_id: str) -> None:
    """Background reader: forward ``{"action": "stop"}`` to the running op.

    The session-stream WS is read-only by default; this listener gives
    subscribers an opt-in way to halt the underlying operation. Unknown
    payloads are ignored so a noisy client can't tear the listener down.
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
                # Don't return — leave the listener alive so a misbehaving
                # client that sends stop multiple times is harmless.
    except (WebSocketDisconnect, RuntimeError):
        return
