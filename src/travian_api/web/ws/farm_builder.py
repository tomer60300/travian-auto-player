"""WebSocket handler for Farm Builder — preview or full-build jobs.

Endpoints
---------
WS /ws/farm-builder/run?token=<JWT>

The build job runs as a managed background operation, so backgrounding
Safari (or any WS drop) does NOT abort it. Reconnect via
``/ws/sessions/{id}/stream`` to resume the live feed.

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
  {"type": "operation_complete", "status": ...}  # terminal pushed by manager
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import update

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.operation_manager import OperationContext, operation_manager
from travian_api.services.farm_builder_service import FarmBuilderService
from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.models.db import async_session_factory
from travian_api.web.models.farm_builder import FarmBuilderRunHistory
from travian_api.web.operation_gate import active_ops
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws._resumable import subscribe_and_tail
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "farm_builder"

OP_LABEL = "farm-builder"


def _build_farm_builder_coro(action: str, cfg: dict, survivors: list):
    """Returns the OperationManager coroutine for a preview or run job."""

    async def coro(ctx: OperationContext) -> None:
        session: TravianSession = ctx.session
        svc = FarmBuilderService(session)

        async def send_log(category: str, emoji: str, message: str, level: str = "info") -> None:
            ts = time.time()
            ctx.push(
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
                    "user_id": ctx.user_id,
                }
            )

        async def check_stop() -> bool:
            return ctx.should_stop()

        # Activity budget check before starting
        try:
            session.http_client.check_activity_budget()
        except ActivityBudgetExhausted as exc:
            ctx.push({"type": "error", "message": str(exc), "fatal": True})
            ctx.push({"type": "status", "data": {"state": "stopped"}})
            return

        ctx.push({"type": "status", "data": {"state": "running"}})

        history_id: int | None = None

        if action == "preview":
            result = await svc.preview(cfg, send_log, check_stop)
            if result.get("stopped"):
                ctx.push({"type": "status", "data": {"state": "stopped"}})
            else:
                ctx.push({"type": "preview_result", "data": result})
                ctx.push({"type": "status", "data": {"state": "completed"}})
            return

        # action == "run"
        if not survivors:
            ctx.push(
                {"type": "error", "message": "No survivors list provided for run", "fatal": True}
            )
            ctx.push({"type": "status", "data": {"state": "stopped"}})
            return

        # Record history row up-front.
        try:
            async with async_session_factory() as db:
                row = FarmBuilderRunHistory(
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
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
        ctx.push({"type": "complete", "report": report})
        final_state = "stopped" if report.get("stopped") else "completed"
        ctx.push({"type": "status", "data": {"state": final_state}})

        if history_id is not None:
            try:
                async with async_session_factory() as db:
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

    return coro


@router.websocket("/ws/farm-builder/run")
async def farm_builder_ws(websocket: WebSocket) -> None:
    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    if OP_LABEL in active_ops.get_active(user_id):
        existing = next(
            (op for op in operation_manager.list_for_user(user_id) if op.label == OP_LABEL),
            None,
        )
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "A farm builder is already running for this account",
            }
        )
        await websocket.close(code=4009, reason="Farm builder already running")
        return

    await websocket.accept()

    try:
        raw = await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return

    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await websocket.send_json({"type": "error", "message": "Invalid JSON", "fatal": True})
        await websocket.close(code=4002)
        return

    action = msg.get("action") if isinstance(msg, dict) else None
    if action not in ("preview", "run"):
        await websocket.send_json(
            {"type": "error", "message": "Expected 'preview' or 'run' action", "fatal": True}
        )
        await websocket.close(code=4002)
        return

    cfg = msg.get("config") or {}
    if not cfg.get("home_villages"):
        await websocket.send_json(
            {"type": "error", "message": "No home_villages in config", "fatal": True}
        )
        await websocket.close(code=4002)
        return

    survivors = msg.get("survivors") or [] if action == "run" else []

    op = operation_manager.start(
        user_id=user_id,
        label=OP_LABEL,
        session_type="farm-builder",
        session_label="Farm Builder",
        session=session,
        coro=_build_farm_builder_coro(action, cfg, survivors),
        require_unique_label=True,
    )
    if op is None:
        existing = operation_manager.find_by_label(user_id, OP_LABEL)
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "A farm builder is already running for this account",
            }
        )
        await websocket.close(code=4009, reason="Farm builder already running")
        return

    await subscribe_and_tail(websocket, user_id, CHANNEL, op.session_id)
