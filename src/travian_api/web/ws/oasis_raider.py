"""WebSocket handler for Oasis Raider — live raid progress streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.services.oasis_raider_service import OasisRaiderConfig, OasisRaiderService
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.operation_gate import operation_gate
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "oasis_raider"


async def _send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def _listen_for_stop(websocket: WebSocket, stop_event: asyncio.Event) -> None:
    """Background task: listen for stop messages while sweep runs."""
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
                if msg.get("action") == "stop":
                    stop_event.set()
                    return
            except (json.JSONDecodeError, TypeError):
                pass
    except (WebSocketDisconnect, RuntimeError):
        stop_event.set()


@router.websocket("/ws/oasis-raider")
async def oasis_raider_ws(websocket: WebSocket) -> None:
    """Oasis Raider WS: auth, receive config, run sweep with live log streaming."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    op_type = "oasis-raider"

    if not operation_gate.acquire(user_id, op_type):
        await websocket.close(code=4009, reason="An oasis raider operation is already running")
        return

    try:
        op_started_at = time.monotonic()

        await ws_manager.connect(websocket, user_id, CHANNEL)
        exec_session = exec_session_manager.create(user_id, "oasis-raider", "Oasis Raider")

        async def tracked_send(data: dict) -> bool:
            ok = await _send(websocket, data)
            if ok:
                exec_session_manager.push(exec_session.id, data)
            return ok

        await tracked_send({"type": "session_init", "session_id": exec_session.id})

        # ── Wait for start command ───────────────────────────────────
        try:
            raw = await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            await tracked_send({"type": "error", "message": "Invalid JSON"})
            return

        if msg.get("action") != "start":
            await tracked_send({"type": "error", "message": "Expected start action"})
            return

        # ── Parse config ─────────────────────────────────────────────
        cfg = msg.get("config", {})
        config = OasisRaiderConfig(
            radius=cfg.get("radius", 15),
            troops=cfg.get("troops", {}),
            max_targets=cfg.get("max_targets", 0),
            bonus_filter=cfg.get("bonus_filter", []),
            sleep_interval=cfg.get("sleep_interval", 60),
            dry_run=cfg.get("dry_run", False),
            village_id=cfg.get("village_id"),
            repeat_interval_seconds=cfg.get("repeat_interval_seconds", 0),
        )

        if not config.troops:
            await tracked_send({"type": "error", "message": "No troops configured"})
            return

        await tracked_send({"type": "status", "data": {"state": "running"}})

        # ── Set up service + callbacks ───────────────────────────────
        service = OasisRaiderService(session)
        stop_event = asyncio.Event()

        async def send_log(
            category: str, emoji: str, message: str, level: str = "info",
        ) -> None:
            ts = time.time()
            await tracked_send({
                "type": "log",
                "data": {
                    "timestamp": ts,
                    "emoji": emoji,
                    "category": category,
                    "message": message,
                    "level": level,
                },
            })
            log_stream_manager.push({
                "timestamp": ts,
                "level": level,
                "source": "oasis_raider",
                "message": f"[{category}] {emoji} {message}",
                "user_id": user_id,
            })

        async def check_stop() -> bool:
            return stop_event.is_set()

        # ── Run sweep with background stop listener ──────────────────
        listener = asyncio.create_task(_listen_for_stop(websocket, stop_event))

        try:
            iteration = 0
            while not stop_event.is_set():
                # Check if captcha was just resolved — stop instead of auto-resuming
                if operation_gate.check_should_stop(user_id, op_started_at):
                    await tracked_send({"type": "error", "message": "Stopped after captcha resolution — restart manually"})
                    await tracked_send({"type": "status", "data": {"state": "stopped"}})
                    break

                # Activity budget check
                try:
                    session.http_client.check_activity_budget()
                except ActivityBudgetExhausted as exc:
                    await tracked_send({"type": "error", "message": str(exc)})
                    await tracked_send({"type": "status", "data": {"state": "stopped"}})
                    break

                iteration += 1
                if iteration > 1:
                    await send_log(
                        "RECURRING", "🔁",
                        f"Starting iteration #{iteration} (repeat_interval={config.repeat_interval_seconds}s)",
                        "info",
                    )
                    await tracked_send({"type": "status", "data": {"state": "running"}})

                stats = await service.run_sweep(config, send_log, check_stop)
                await tracked_send({"type": "summary", "data": stats})

                # If user stopped mid-sweep or recurring is disabled — exit
                if stop_event.is_set() or config.repeat_interval_seconds <= 0:
                    break

                # Wait for repeat_interval_seconds (interruptible by stop)
                wait_secs = config.repeat_interval_seconds
                await send_log(
                    "RECURRING", "⏱️",
                    f"Iteration #{iteration} complete — next run in {wait_secs}s",
                    "info",
                )
                await tracked_send({"type": "status", "data": {"state": "sleeping"}})

                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_secs)
                    # stop_event fired → user stopped during wait
                    break
                except asyncio.TimeoutError:
                    pass  # timeout reached, run next iteration

            state = "stopped" if stop_event.is_set() else "completed"
            await tracked_send({"type": "status", "data": {"state": state}})
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as exc:
            logger.exception("Oasis raider error for user %s", user_id)
            await tracked_send({"type": "error", "message": str(exc)})
            await tracked_send({"type": "status", "data": {"state": "stopped"}})
        finally:
            listener.cancel()
            try:
                await listener
            except asyncio.CancelledError:
                pass

    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        logger.exception("Unexpected error in oasis raider WS for user %s", user_id)
    finally:
        operation_gate.release(user_id, op_type)
        exec_session_manager.mark_disconnected(exec_session.id)
        await ws_manager.disconnect(user_id, CHANNEL, websocket)
