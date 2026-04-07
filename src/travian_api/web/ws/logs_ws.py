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

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    channel = "logs"
    await ws_manager.connect(websocket, user_id, channel)

    # Parse initial level filter from query params
    level_param = websocket.query_params.get("level", "info").lower()
    min_level = _LEVEL_MAP.get(level_param, logging.INFO)

    # Use a unique subscriber ID (user_id is fine for single-connection per user)
    sub_id = user_id
    queue = log_stream_manager.subscribe(sub_id)

    try:
        # Send history as catch-up
        history = log_stream_manager.get_history(100)
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
                    except Exception:
                        raise WebSocketDisconnect()

        async def _listen_for_commands():
            nonlocal min_level
            while True:
                try:
                    msg = await websocket.receive_json()
                except (WebSocketDisconnect, Exception):
                    raise WebSocketDisconnect()
                if isinstance(msg, dict):
                    action = msg.get("action", "")
                    if action == "filter":
                        new_level = msg.get("level", "info").lower()
                        min_level = _LEVEL_MAP.get(new_level, logging.INFO)

        stream_task = asyncio.create_task(_stream_logs())
        listen_task = asyncio.create_task(_listen_for_commands())

        done, pending = await asyncio.wait(
            {stream_task, listen_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unexpected error in logs WS: user=%s", user_id)
    finally:
        log_stream_manager.unsubscribe(sub_id)
        await ws_manager.disconnect(user_id, channel)
