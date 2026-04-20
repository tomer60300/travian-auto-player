"""REST + WebSocket routes for remote execution session mirroring.

REST:
    GET /api/sessions — list the current user's execution sessions

WebSocket:
    WS /ws/sessions/{session_id}/stream?token=<JWT> — subscribe to a session's
    live output (sends history on connect, then streams new messages)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, status

from travian_api.web.auth import get_current_user
from travian_api.web.execution_sessions import exec_session_manager
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
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await ws_manager.disconnect(user_id, channel, websocket)
        return

    history, queue = result

    try:
        # Send metadata
        await websocket.send_json({
            "type": "session_meta",
            "id": session.id,
            "session_type": session.session_type,
            "label": session.label,
            "status": session.status,
            "created_at": session.created_at,
        })

        # Send full history
        await websocket.send_json({"type": "history", "messages": history})

        # If session already ended, tell the client and close gracefully
        if session.status == "disconnected":
            await websocket.send_json({"type": "session_ended"})
            return

        # Stream live messages until session ends or client disconnects
        while True:
            data = await queue.get()
            if data is None:
                # None sentinel = session ended
                await websocket.send_json({"type": "session_ended"})
                break
            await websocket.send_json({"type": "message", "data": data})

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(
            "Error in session stream WS: session=%s user=%s", session_id, user_id,
        )
    finally:
        exec_session_manager.unsubscribe(session_id, sub_id)
        await ws_manager.disconnect(user_id, channel, websocket)
