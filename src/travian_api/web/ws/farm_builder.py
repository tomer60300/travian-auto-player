"""WebSocket handler for Farm Builder — runs preview or full-build jobs.

Endpoints
---------
WS /ws/farm-builder/run?token=<JWT>

Protocol (mirrors scout_auto / farm_run conventions)
----------------------------------------------------
Client -> server:
  {"action": "preview", "config": {...}}       # runs phases 1-2 only
  {"action": "run",     "config": {...}, "survivors": [...]}  # runs phases 3-6
  {"action": "stop"}

Server -> client:
  {"type": "session_init", "session_id": "..."}
  {"type": "log", "data": {"category", "emoji", "message", "level", "timestamp"}}
  {"type": "preview_result", "data": {...}}
  {"type": "phase_start", "phase": "...", "total": N}
  {"type": "phase_progress", "phase": "...", "completed": K, "total": N}
  {"type": "target_result", "phase": "...", "x": .., "y": .., ...}
  {"type": "phase_done", "phase": "...", "summary": {...}}
  {"type": "complete", "report": {...}}
  {"type": "status", "data": {"state": "running|stopped|completed"}}
  {"type": "error", "message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.services.farm_builder_service import FarmBuilderService
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.models.db import async_session_factory
from travian_api.web.models.farm_builder import FarmBuilderRunHistory
from travian_api.web.operation_gate import operation_gate
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "farm_builder"


async def _send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def _listen_for_stop(websocket: WebSocket, stop_event: asyncio.Event) -> None:
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


@router.websocket("/ws/farm-builder/run")
async def farm_builder_ws(websocket: WebSocket) -> None:
    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    op_type = "farm-builder"

    if not operation_gate.acquire(user_id, op_type):
        await websocket.close(code=4009, reason="A farm builder operation is already running")
        return

    try:
        op_started_at = time.monotonic()

        await ws_manager.connect(websocket, user_id, CHANNEL)
        exec_session = exec_session_manager.create(user_id, "farm-builder", "Farm Builder")

        async def tracked_send(data: dict) -> bool:
            ok = await _send(websocket, data)
            if ok:
                exec_session_manager.push(exec_session.id, data)
            return ok

        async def send_log(category: str, emoji: str, message: str, level: str = "info") -> None:
            ts = time.time()
            await tracked_send(
                {
                    "type": "log",
                    "data": {
                        "timestamp": ts,
                        "emoji": emoji,
                        "category": category,
                        "message": message,
                        "level": level,
                    },
                }
            )
            log_stream_manager.push(
                {
                    "timestamp": ts,
                    "level": level,
                    "source": "farm_builder",
                    "message": f"[{category}] {emoji} {message}",
                    "user_id": user_id,
                }
            )

        await tracked_send({"type": "session_init", "session_id": exec_session.id})

        # Wait for start command (preview or run)
        try:
            raw = await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return

        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            await tracked_send({"type": "error", "message": "Invalid JSON"})
            return

        action = msg.get("action")
        if action not in ("preview", "run"):
            await tracked_send({"type": "error", "message": "Expected 'preview' or 'run' action"})
            return

        cfg = msg.get("config") or {}
        if not cfg.get("home_villages"):
            await tracked_send({"type": "error", "message": "No home_villages in config"})
            return

        svc = FarmBuilderService(session)
        stop_event = asyncio.Event()

        async def check_stop() -> bool:
            return stop_event.is_set()

        # Check if captcha was just resolved
        if operation_gate.check_should_stop(user_id, op_started_at):
            await tracked_send(
                {"type": "error", "message": "Stopped after captcha resolution — restart manually"}
            )
            await tracked_send({"type": "status", "data": {"state": "stopped"}})
            return

        # Activity budget check before starting
        try:
            session.http_client.check_activity_budget()
        except ActivityBudgetExhausted as exc:
            await tracked_send({"type": "error", "message": str(exc)})
            await tracked_send({"type": "status", "data": {"state": "stopped"}})
            return

        await tracked_send({"type": "status", "data": {"state": "running"}})
        listener = asyncio.create_task(_listen_for_stop(websocket, stop_event))

        history_id: int | None = None

        try:
            if action == "preview":
                result = await svc.preview(cfg, send_log, check_stop)
                if result.get("stopped"):
                    await tracked_send({"type": "status", "data": {"state": "stopped"}})
                else:
                    await tracked_send({"type": "preview_result", "data": result})
                    await tracked_send({"type": "status", "data": {"state": "completed"}})
            else:  # run
                survivors = msg.get("survivors") or []
                if not survivors:
                    await tracked_send(
                        {"type": "error", "message": "No survivors list provided for run"}
                    )
                    await tracked_send({"type": "status", "data": {"state": "stopped"}})
                else:
                    # Record history row
                    try:
                        async with async_session_factory() as db:
                            row = FarmBuilderRunHistory(
                                user_id=user_id,
                                session_id=exec_session.id,
                                status="running",
                                total_targets=len(survivors),
                            )
                            db.add(row)
                            await db.commit()
                            await db.refresh(row)
                            history_id = row.id
                    except Exception as exc:
                        logger.warning("Failed to create history row: %s", exc)

                    report = await svc.run_full(cfg, survivors, send_log, check_stop)
                    await tracked_send({"type": "complete", "report": report})
                    final_state = "stopped" if report.get("stopped") else "completed"
                    await tracked_send({"type": "status", "data": {"state": final_state}})

                    # Update history
                    if history_id is not None:
                        try:
                            from datetime import datetime

                            async with async_session_factory() as db:
                                from sqlalchemy import update

                                await db.execute(
                                    update(FarmBuilderRunHistory)
                                    .where(FarmBuilderRunHistory.id == history_id)
                                    .values(
                                        status=final_state,
                                        added=report.get("added", 0),
                                        skipped=report.get("skipped", 0),
                                        failed=report.get("failed", 0),
                                        report_json=json.dumps(report),
                                        ended_at=datetime.now(UTC),
                                    )
                                )
                                await db.commit()
                        except Exception as exc:
                            logger.warning("Failed to update history row: %s", exc)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as exc:
            logger.exception("Farm builder error for user %s", user_id)
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
        logger.exception("Unexpected error in farm builder WS for user %s", user_id)
    finally:
        operation_gate.release(user_id, op_type)
        exec_session_manager.mark_disconnected(exec_session.id)
