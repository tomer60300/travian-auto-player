"""WebSocket endpoint for streaming server-side logs to the frontend.

Endpoint: WS /ws/logs?token=<JWT>&level=INFO

Protocol:
    - On connect: sends history (last 100 entries)
    - Streams new entries as they arrive
    - Client can send filter updates: {"action": "filter", "level": "DEBUG"}
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


@router.websocket("/ws/logs")
async def ws_logs(websocket: WebSocket):
    """Stream server-side log entries to authenticated clients."""

    user_id = await ws_manager.authenticate(websocket, require_travian_session=False)
    if user_id is None:
        return

    channel = "logs"
    await ws_manager.connect(websocket, user_id, channel)

    # Parse initial level filter from query params
    level_param = websocket.query_params.get("level", "info").lower()
    min_level = _LEVEL_MAP.get(level_param, logging.INFO)

    # Unique subscriber ID — supports multiple tabs per user
    # Pass user_id so logs are filtered to this user only (no cross-user leakage)
    sub_id = id(websocket)
    queue = log_stream_manager.subscribe(sub_id, user_id=user_id)

    try:
        # Send history as catch-up (filtered to this user)
        history = log_stream_manager.get_history(100, user_id=user_id)
        filtered_history = [e for e in history if _LEVEL_MAP.get(e.get("level", "info"), logging.INFO) >= min_level]
        await websocket.send_json({"type": "history", "entries": filtered_history})

        # Concurrent tasks: stream logs + listen for filter updates
        async def _stream_logs():
            while True:
                entry = await queue.get()
                entry_level = _LEVEL_MAP.get(entry.get("level", "info"), logging.INFO)
                if entry_level >= min_level:
                    try:
                        await websocket.send_json({"type": "log", **entry})
                    except (WebSocketDisconnect, RuntimeError):
                        return  # socket closed — exit cleanly

        async def _listen_for_commands():
            nonlocal min_level
            try:
                while True:
                    msg = await websocket.receive_json()
                    if isinstance(msg, dict) and msg.get("action") == "filter":
                        new_level = msg.get("level", "info").lower()
                        min_level = _LEVEL_MAP.get(new_level, logging.INFO)
            except (WebSocketDisconnect, RuntimeError):
                return  # socket closed — exit cleanly

        tasks = [
            asyncio.create_task(_stream_logs()),
            asyncio.create_task(_listen_for_commands()),
        ]
        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            # Retrieve result so the exception propagates to the outer
            # handler instead of triggering "Task exception was never retrieved".
            for t in done:
                t.result()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unexpected error in logs WS: user=%s", user_id)
    finally:
        log_stream_manager.unsubscribe(sub_id)
        await ws_manager.disconnect(user_id, channel, websocket)
