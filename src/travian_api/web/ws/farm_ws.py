"""WebSocket handlers for long-running farm loop-send operations.

Endpoints
---------
WS /ws/farm/run/{list_id}?interval=300&duration=0&verbose=false&token=<JWT>
WS /ws/farm/run-all?interval=300&duration=0&list_ids=1,2,3&token=<JWT>

Protocol
--------
After connecting, the server waits for a ``{"action": "start"}`` message from
the client before entering the send loop.  The client can send
``{"action": "stop"}`` at any time to gracefully terminate.

Messages streamed back to the client:

- ``{"type": "info", ...}``          initial config acknowledgement
- ``{"type": "cycle_start", ...}``   beginning of a send cycle
- ``{"type": "result", ...}``        per-list (or per-slot when verbose) result
- ``{"type": "cycle_end", ...}``     end of a cycle with aggregated stats
- ``{"type": "error", ...}``         non-fatal error (loop continues)
- ``{"type": "complete", ...}``      loop finished (duration elapsed / stopped)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.sessions import session_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _send(ws: WebSocket, data: dict) -> None:
    """Send JSON to the client; silently swallow errors (caller handles disconnect)."""
    try:
        await ws.send_json(data)
    except Exception:
        raise WebSocketDisconnect()


async def _wait_for_start_or_stop(ws: WebSocket) -> bool:
    """Block until the client sends ``{"action": "start"}``.

    Returns ``True`` if start received, ``False`` if stop/disconnect.
    """
    while True:
        try:
            msg = await ws.receive_json()
        except (WebSocketDisconnect, Exception):
            return False
        action = msg.get("action", "").lower() if isinstance(msg, dict) else ""
        if action == "start":
            return True
        if action == "stop":
            return False


async def _check_stop(ws: WebSocket) -> bool:
    """Non-blocking check whether the client sent ``{"action": "stop"}``.

    Returns ``True`` if a stop message was received.
    """
    try:
        # Use wait_for with a tiny timeout so we don't block the loop
        msg = await asyncio.wait_for(ws.receive_json(), timeout=0.05)
        if isinstance(msg, dict) and msg.get("action", "").lower() == "stop":
            return True
    except asyncio.TimeoutError:
        pass
    except (WebSocketDisconnect, Exception):
        return True  # treat disconnect as stop
    return False


# ---------------------------------------------------------------------------
# WS /ws/farm/run/{list_id}
# ---------------------------------------------------------------------------


@router.websocket("/ws/farm/run/{list_id}")
async def ws_farm_run(websocket: WebSocket, list_id: int):
    """Loop-send a single farm list at a fixed interval."""

    # ── Authenticate ─────────────────────────────────────────────────
    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return  # socket already closed by authenticate()

    session = session_manager.get(user_id)
    if session is None:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="No active Travian session",
        )
        return

    # ── Parse query params ───────────────────────────────────────────
    params = websocket.query_params
    try:
        interval = max(10, int(params.get("interval", "300")))
        duration = max(0, int(params.get("duration", "0")))
    except (ValueError, TypeError):
        interval, duration = 300, 0
    verbose = params.get("verbose", "false").lower() in ("true", "1", "yes")

    channel = f"farm_run_{list_id}"
    await ws_manager.connect(websocket, user_id, channel)

    exec_session = exec_session_manager.create(user_id, "farm-run", f"Farm Run - #{list_id}")

    async def _tracked_send(ws, data):
        await _send(ws, data)
        exec_session_manager.push(exec_session.id, data)

    try:
        # ── Fetch list info and send initial message ─────────────────
        try:
            fl = await session.farm_service.get_farm_list(list_id)
        except Exception as exc:
            await _tracked_send(websocket, {
                "type": "error",
                "message": f"Failed to fetch farm list {list_id}: {exc}",
            })
            return

        exec_session.label = f"Farm Run - {fl.name}"
        await _tracked_send(websocket, {"type": "session_init", "session_id": exec_session.id})
        await _tracked_send(websocket, {
            "type": "info",
            "list_id": fl.id,
            "list_name": fl.name,
            "active_slots": len(fl.active_slots),
            "interval": interval,
            "duration": duration,
            "verbose": verbose,
            "message": "Waiting for start command",
        })

        # ── Wait for client to send {"action": "start"} ─────────────
        if not await _wait_for_start_or_stop(websocket):
            await _tracked_send(websocket, {"type": "complete", "reason": "cancelled"})
            return

        # ── Main loop ────────────────────────────────────────────────
        end_time = time.time() + duration * 60 if duration else None
        total_success = 0
        total_fail = 0
        cycle = 0

        while True:
            # Check duration
            if end_time and time.time() >= end_time:
                break

            # Check for stop from client
            if await _check_stop(websocket):
                break

            cycle += 1
            await _tracked_send(websocket, {
                "type": "cycle_start",
                "cycle": cycle,
                "timestamp": _now_iso(),
            })

            try:
                result = await session.farm_service.send_farm_list(list_id)

                # Check Gold Club error
                if result.targets and result.targets[0].error == "plus.error_goldclub":
                    await _tracked_send(websocket, {
                        "type": "error",
                        "message": "Gold Club not active - cannot send farm lists",
                        "fatal": True,
                    })
                    break

                cycle_success = result.success_count
                cycle_fail = result.fail_count
                total_success += cycle_success
                total_fail += cycle_fail

                # Always send per-target results for failures; all results when verbose
                for t in result.targets:
                    is_ok = t.error == ""
                    if verbose or not is_ok:
                        await _tracked_send(websocket, {
                            "type": "result",
                            "cycle": cycle,
                            "slot_id": t.id,
                            "success": is_ok,
                            "status": t.status,
                            "error": t.error or None,
                        })

                # Compute next send time: None if the loop will end before then
                next_time = time.time() + interval
                if end_time and next_time >= end_time:
                    next_send = None
                else:
                    next_send = datetime.fromtimestamp(next_time).isoformat(timespec="seconds")

                await _tracked_send(websocket, {
                    "type": "cycle_end",
                    "cycle": cycle,
                    "sent": cycle_success,
                    "failed": cycle_fail,
                    "total": cycle_success + cycle_fail,
                    "cumulative_success": total_success,
                    "cumulative_fail": total_fail,
                    "timestamp": _now_iso(),
                    "next_send_at": next_send,
                })

            except WebSocketDisconnect:
                raise
            except Exception as exc:
                await _tracked_send(websocket, {
                    "type": "error",
                    "cycle": cycle,
                    "message": str(exc),
                    "fatal": False,
                    "timestamp": _now_iso(),
                })

            # Sleep between cycles with jitter (±15%) to avoid detection
            jitter = interval * random.uniform(-0.15, 0.15)
            wait_time = interval + jitter
            remaining = wait_time
            while remaining > 0:
                chunk = min(remaining, 2)
                await asyncio.sleep(chunk)
                remaining -= chunk
                if await _check_stop(websocket):
                    remaining = -1  # signal break
            if remaining == -1:
                break

        # ── Send completion summary ──────────────────────────────────
        await _tracked_send(websocket, {
            "type": "complete",
            "reason": "duration_elapsed" if (end_time and time.time() >= end_time) else "stopped",
            "total_cycles": cycle,
            "total_success": total_success,
            "total_fail": total_fail,
            "timestamp": _now_iso(),
        })

    except WebSocketDisconnect:
        logger.info("Farm run WS disconnected: user=%s list=%s", user_id, list_id)
    except Exception as exc:
        logger.exception("Unexpected error in farm run WS: user=%s list=%s", user_id, list_id)
        try:
            await _tracked_send(websocket, {
                "type": "error",
                "message": f"Unexpected server error: {exc}",
                "fatal": True,
            })
        except Exception:
            logger.debug("Failed to send error message to farm run WS: user=%s list=%s", user_id, list_id, exc_info=True)
    finally:
        exec_session_manager.mark_disconnected(exec_session.id)
        await ws_manager.disconnect(user_id, channel, websocket)


# ---------------------------------------------------------------------------
# WS /ws/farm/run-all
# ---------------------------------------------------------------------------


@router.websocket("/ws/farm/run-all")
async def ws_farm_run_all(websocket: WebSocket):
    """Loop-send all (or specified) farm lists at a fixed interval."""

    # ── Authenticate ─────────────────────────────────────────────────
    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session = session_manager.get(user_id)
    if session is None:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="No active Travian session",
        )
        return

    # ── Parse query params ───────────────────────────────────────────
    params = websocket.query_params
    try:
        interval = max(10, int(params.get("interval", "300")))
        duration = max(0, int(params.get("duration", "0")))
    except (ValueError, TypeError):
        interval, duration = 300, 0
    verbose = params.get("verbose", "false").lower() in ("true", "1", "yes")

    list_ids_param = params.get("list_ids", "")
    requested_ids: list[int] | None = None
    if list_ids_param:
        requested_ids = [int(x.strip()) for x in list_ids_param.split(",") if x.strip()]

    channel = "farm_run_all"
    await ws_manager.connect(websocket, user_id, channel)

    exec_session = exec_session_manager.create(user_id, "farm-run-all", "Farm Run All")

    async def _tracked_send_all(ws, data):
        await _send(ws, data)
        exec_session_manager.push(exec_session.id, data)

    try:
        # ── Fetch lists and resolve IDs ──────────────────────────────
        try:
            all_lists = await session.farm_service.get_all_farm_lists()
        except Exception as exc:
            await _tracked_send_all(websocket, {
                "type": "error",
                "message": f"Failed to fetch farm lists: {exc}",
            })
            return

        if requested_ids:
            all_lists = [fl for fl in all_lists if fl.id in requested_ids]

        if not all_lists:
            await _tracked_send_all(websocket, {
                "type": "error",
                "message": "No farm lists found",
                "fatal": True,
            })
            return

        send_ids = [fl.id for fl in all_lists]
        total_active = sum(len(fl.active_slots) for fl in all_lists)

        list_names = ", ".join(fl.name for fl in all_lists)
        exec_session.label = f"Farm Run All ({len(all_lists)} lists)"
        await _tracked_send_all(websocket, {"type": "session_init", "session_id": exec_session.id})
        await _tracked_send_all(websocket, {
            "type": "info",
            "lists": [
                {"id": fl.id, "name": fl.name, "active_slots": len(fl.active_slots)}
                for fl in all_lists
            ],
            "total_active_slots": total_active,
            "interval": interval,
            "duration": duration,
            "verbose": verbose,
            "message": "Waiting for start command",
        })

        # ── Wait for start ───────────────────────────────────────────
        if not await _wait_for_start_or_stop(websocket):
            await _tracked_send_all(websocket, {"type": "complete", "reason": "cancelled"})
            return

        # ── Main loop ────────────────────────────────────────────────
        end_time = time.time() + duration * 60 if duration else None
        total_success = 0
        total_fail = 0
        cycle = 0

        while True:
            if end_time and time.time() >= end_time:
                break

            if await _check_stop(websocket):
                break

            cycle += 1
            await _tracked_send_all(websocket, {
                "type": "cycle_start",
                "cycle": cycle,
                "timestamp": _now_iso(),
            })

            try:
                results = await session.farm_service.send_all_farm_lists(send_ids)

                gold_club_error = False
                cycle_success = 0
                cycle_fail = 0

                for lid, result in results.items():
                    if result.targets and result.targets[0].error == "plus.error_goldclub":
                        gold_club_error = True
                        break

                    list_success = result.success_count
                    list_fail = result.fail_count
                    cycle_success += list_success
                    cycle_fail += list_fail

                    # Always send individual failure details; full list when verbose
                    failed_targets = [t for t in result.targets if t.error != ""]
                    if verbose or failed_targets:
                        targets_to_report = result.targets if verbose else failed_targets
                        await _tracked_send_all(websocket, {
                            "type": "result",
                            "cycle": cycle,
                            "list_id": lid,
                            "success": list_success,
                            "fail": list_fail,
                            "targets": [
                                {
                                    "slot_id": t.id,
                                    "success": t.error == "",
                                    "status": t.status,
                                    "error": t.error or None,
                                }
                                for t in targets_to_report
                            ],
                        })

                if gold_club_error:
                    await _tracked_send_all(websocket, {
                        "type": "error",
                        "message": "Gold Club not active - cannot send farm lists",
                        "fatal": True,
                    })
                    break

                total_success += cycle_success
                total_fail += cycle_fail

                # Compute next send time for run-all
                next_time = time.time() + interval
                if end_time and next_time >= end_time:
                    next_send = None
                else:
                    next_send = datetime.fromtimestamp(next_time).isoformat(timespec="seconds")

                await _tracked_send_all(websocket, {
                    "type": "cycle_end",
                    "cycle": cycle,
                    "sent": cycle_success,
                    "failed": cycle_fail,
                    "total": cycle_success + cycle_fail,
                    "cumulative_success": total_success,
                    "cumulative_fail": total_fail,
                    "timestamp": _now_iso(),
                    "next_send_at": next_send,
                })

            except WebSocketDisconnect:
                raise
            except Exception as exc:
                await _tracked_send_all(websocket, {
                    "type": "error",
                    "cycle": cycle,
                    "message": str(exc),
                    "fatal": False,
                    "timestamp": _now_iso(),
                })

            # Sleep between cycles with jitter (±15%) to avoid detection
            jitter = interval * random.uniform(-0.15, 0.15)
            wait_time = interval + jitter
            remaining = wait_time
            while remaining > 0:
                chunk = min(remaining, 2)
                await asyncio.sleep(chunk)
                remaining -= chunk
                if await _check_stop(websocket):
                    remaining = -1
            if remaining == -1:
                break

        # ── Completion ───────────────────────────────────────────────
        await _tracked_send_all(websocket, {
            "type": "complete",
            "reason": "duration_elapsed" if (end_time and time.time() >= end_time) else "stopped",
            "total_cycles": cycle,
            "total_success": total_success,
            "total_fail": total_fail,
            "timestamp": _now_iso(),
        })

    except WebSocketDisconnect:
        logger.info("Farm run-all WS disconnected: user=%s", user_id)
    except Exception as exc:
        logger.exception("Unexpected error in farm run-all WS: user=%s", user_id)
        try:
            await _tracked_send_all(websocket, {
                "type": "error",
                "message": f"Unexpected server error: {exc}",
                "fatal": True,
            })
        except Exception:
            logger.debug("Failed to send error message to farm run-all WS: user=%s", user_id, exc_info=True)
    finally:
        exec_session_manager.mark_disconnected(exec_session.id)
        await ws_manager.disconnect(user_id, channel, websocket)
