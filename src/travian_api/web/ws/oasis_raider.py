"""WebSocket handler for Oasis Raider.

Thin wrapper around :class:`OperationManager`: this WS only

* parses the start config and spawns the managed background task,
* forwards client ``{"action": "stop"}`` messages to that task,
* tails the task's :class:`ExecutionSession` so the initiating client sees
  live progress.

Crucially, dropping the WS (Safari background, page reload, network blip)
does **not** stop the sweep — the op coroutine keeps running to completion,
and the client can reconnect via ``/ws/sessions/{id}/stream`` to resume the
live tail with full message history.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.operation_manager import OperationContext, operation_manager
from travian_api.services.oasis_raider_service import OasisRaiderConfig, OasisRaiderService
from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws._loop_stealth import (
    interruptible_sleep,
    night_rest_pause,
    recurring_wait,
)
from travian_api.web.ws._resumable import subscribe_and_tail
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "oasis_raider"

OP_LABEL = "oasis-raider"


async def _send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


def _build_oasis_coro(config: OasisRaiderConfig):
    """Return the coroutine OperationManager will run in the background."""

    async def coro(ctx: OperationContext) -> None:
        service = OasisRaiderService(ctx.session)

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
                    "source": "oasis_raider",
                    "message": f"[{category}] {emoji} {message}",
                    "user_id": ctx.user_id,
                }
            )

        async def check_stop() -> bool:
            return ctx.should_stop()

        iteration = 0

        # Announce a night pause in the oasis UI's own vocabulary: send_log is
        # both visible in the UI and mirrored to the log stream (its message
        # switch has no "info" case), and the sleeping/running status keeps the
        # indicator honest rather than stuck on "running" through the night.
        async def _announce_rest(hours: float) -> None:
            await send_log(
                "RECURRING", "🌙", f"Night rest — pausing ~{hours:.1f}h, resuming in the morning."
            )
            ctx.push({"type": "status", "data": {"state": "sleeping"}})

        recurring = config.repeat_interval_seconds > 0
        while not ctx.should_stop():
            # Go quiet overnight and resume in the morning — a graceful, VISIBLE
            # pause (night_rest_pause announces it and flips the status), not the
            # fatal budget path below. ONLY for a recurring raid: a one-shot
            # (repeat_interval=0, the default) is a single sweep the operator
            # wants now, not something to delay a whole night before firing.
            if recurring and await night_rest_pause(ctx, announce=_announce_rest):
                break

            try:
                ctx.session.http_client.check_activity_budget()
            except ActivityBudgetExhausted as exc:
                # fatal=True so OperationManager terminal-status detection
                # marks this as FAILED rather than COMPLETED.
                ctx.push({"type": "error", "message": str(exc), "fatal": True})
                ctx.push({"type": "status", "data": {"state": "stopped"}})
                return

            iteration += 1
            if iteration > 1:
                await send_log(
                    "RECURRING",
                    "🔁",
                    f"Starting iteration #{iteration} "
                    f"(repeat_interval={config.repeat_interval_seconds}s)",
                    "info",
                )
            # One running-status push per iteration, right before the sweep — it
            # also covers resuming from a night pause, which left it "sleeping".
            ctx.push({"type": "status", "data": {"state": "running"}})

            stats = await service.run_sweep(config, send_log, check_stop)
            ctx.push({"type": "summary", "data": stats})

            if ctx.should_stop() or config.repeat_interval_seconds <= 0:
                break

            # Heavy-tailed, tempo-scaled inter-sweep wait (shared with the farm
            # loop): ±10% micro-jitter on a fixed interval still leaves a sharp
            # periodogram peak at repeat_interval, so the whole cadence is
            # replaced by a bursty draw whose expected value is the interval.
            # Round once so the announced countdown matches the actual sleep.
            wait_secs = max(1, round(recurring_wait(ctx, float(config.repeat_interval_seconds))))
            await send_log(
                "RECURRING",
                "⏱️",
                f"Iteration #{iteration} complete — next run in {wait_secs}s",
                "info",
            )
            ctx.push({"type": "status", "data": {"state": "sleeping"}})

            # Captcha-aware chunked sleep (shared with the farm loop): a captcha
            # resolved mid-wait is honored within seconds, not after the full
            # (up to 4x interval) wait.
            if await interruptible_sleep(ctx, wait_secs):
                break

        terminal = "stopped" if ctx.should_stop() else "completed"
        ctx.push({"type": "status", "data": {"state": terminal}})

    return coro


@router.websocket("/ws/oasis-raider")
async def oasis_raider_ws(websocket: WebSocket) -> None:
    """Oasis Raider WS: receive start config, spawn detached op, tail its stream."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    # Already running? Hand the client the existing session_id so it can
    # subscribe via /ws/sessions/{id}/stream instead of starting a duplicate.
    existing = operation_manager.find_by_label(user_id, OP_LABEL)
    if existing is not None:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id,
                "message": (
                    "An oasis raider is already running for this account. "
                    "Subscribe to /ws/sessions/{id}/stream to view it."
                ),
            }
        )
        await websocket.close(code=4009, reason="Oasis raider already running")
        return

    await websocket.accept()

    # ── Receive start config ─────────────────────────────────────────────
    try:
        raw = await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return

    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await _send(websocket, {"type": "error", "message": "Invalid JSON"})
        await websocket.close(code=4002)
        return

    if not isinstance(msg, dict) or msg.get("action") != "start":
        await _send(websocket, {"type": "error", "message": "Expected start action"})
        await websocket.close(code=4002)
        return

    cfg = msg.get("config", {}) or {}
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
        await _send(websocket, {"type": "error", "message": "No troops configured"})
        await websocket.close(code=4002)
        return

    # ── Spawn the detached op (atomic uniqueness) ───────────────────────
    op = operation_manager.start(
        user_id=user_id,
        label=OP_LABEL,
        session_type="oasis-raider",
        session_label="Oasis Raider",
        session=session,
        coro=_build_oasis_coro(config),
        require_unique_label=True,
    )
    if op is None:
        # Lost the race against another tab that started the same op while
        # we were waiting for the start payload. Surface the existing
        # session_id so the client can subscribe to it instead.
        existing = operation_manager.find_by_label(user_id, OP_LABEL)
        await _send(
            websocket,
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": (
                    "An oasis raider is already running for this account. "
                    "Subscribe to /ws/sessions/{id}/stream to view it."
                ),
            },
        )
        await websocket.close(code=4009, reason="Oasis raider already running")
        return

    # Tail the session — WS death is independent of op lifetime.
    await subscribe_and_tail(websocket, user_id, CHANNEL, op.session_id)
