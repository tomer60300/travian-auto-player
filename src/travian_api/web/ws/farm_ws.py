"""WebSocket handlers for long-running farm loop-send operations.

Endpoints
---------
WS /ws/farm/run/{list_id}?interval=300&duration=0&verbose=false&token=<JWT>
WS /ws/farm/run-all?interval=300&duration=0&list_ids=1,2,3&token=<JWT>

Both wrap a managed background operation so the loop survives WS death
(Safari background, page reload, network blip). The op continues until
duration expires, the user sends ``{"action": "stop"}``, or a captcha
resolution signal halts it. Reconnect via ``/ws/sessions/{id}/stream``.

Protocol summary
----------------
1. Client connects (config in query string).
2. Client sends ``{"action": "start"}`` to actually kick off the loop.
3. Op streams ``cycle_start`` / ``result`` / ``cycle_end`` messages plus a
   final ``operation_complete`` terminal pushed by ``OperationManager``.
4. Either side may send ``{"action": "stop"}`` to request halt.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.operation_manager import OperationContext, operation_manager
from travian_api.web.operation_gate import active_ops
from travian_api.web.sessions import session_manager
from travian_api.web.ws._loop_stealth import (
    interruptible_sleep,
    night_rest_pause,
    recurring_wait,
)
from travian_api.web.ws._resumable import subscribe_and_tail
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# A bounded run whose remaining time exceeds this genuinely spans a night, so
# it night-rests like an unbounded run. A shorter bounded run is a deliberate
# operator session that runs through to its deadline rather than absorbing a
# multi-hour sleep (which would also overrun the requested duration).
_NIGHT_REST_MIN_RUN_S = 12 * 3600


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


async def _send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def _wait_for_start(ws: WebSocket) -> bool:
    """Block until the client sends ``{"action": "start"}``. False on disconnect/stop."""
    try:
        raw = await ws.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return False
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(msg, dict):
        return False
    return msg.get("action", "").lower() == "start"


# ---------------------------------------------------------------------------
# Single-list loop coroutine
# ---------------------------------------------------------------------------


def _build_farm_run_coro(
    list_id: int,
    interval: int,
    duration: int,
    verbose: bool,
):
    """Returns the coroutine OperationManager will run in the background."""

    async def coro(ctx: OperationContext) -> None:
        farm_service = ctx.session.farm_service

        # Fetch list info up-front so the client gets a useful initial frame.
        try:
            fl = await farm_service.get_farm_list(list_id)
        except Exception as exc:
            ctx.push(
                {
                    "type": "error",
                    "message": f"Failed to fetch farm list {list_id}: {exc}",
                    "fatal": True,
                }
            )
            return

        ctx.exec_session.label = f"Farm Run - {fl.name}"

        cli_parts = [f"travian farm run {fl.id} --interval {interval} --duration {duration}"]
        if verbose:
            cli_parts.append("--verbose")
        ctx.push({"type": "trigger_info", "command": " ".join(cli_parts)})
        ctx.push(
            {
                "type": "info",
                "list_id": fl.id,
                "list_name": fl.name,
                "active_slots": len(fl.active_slots),
                "interval": interval,
                "duration": duration,
                "verbose": verbose,
            }
        )

        end_time = time.time() + duration * 60 if duration else None
        total_success = 0
        total_fail = 0
        cycle = 0

        while True:
            if end_time and time.time() >= end_time:
                break
            if ctx.should_stop():
                break

            # Go quiet overnight and resume in the morning — a graceful, VISIBLE
            # pause (see night_rest_pause), not the fatal budget path below.
            # Gated on TOTAL run length (fixed, not the shrinking remaining
            # time): unbounded runs and long bounded runs that genuinely span a
            # night rest; a short bounded run is a deliberate fixed session and
            # runs through so its cadence isn't shredded by a mid-session sleep.
            # The deadline caps the sleep so even a long run ending mid-night
            # stops on time instead of lingering asleep past its finish.
            if (duration == 0 or duration * 60 > _NIGHT_REST_MIN_RUN_S) and await night_rest_pause(
                ctx, deadline=end_time
            ):
                break
            # A deadline-capped pause can return right at end_time; re-check here
            # so a bounded run doesn't fire one more cycle past its deadline (the
            # loop-top check alone runs only after the cycle below).
            if end_time and time.time() >= end_time:
                break

            try:
                ctx.session.http_client.check_activity_budget()
            except ActivityBudgetExhausted as exc:
                ctx.push({"type": "error", "message": str(exc), "fatal": True})
                break

            cycle += 1
            ctx.push({"type": "cycle_start", "cycle": cycle, "timestamp": _now_iso()})
            # Draw the (heavy-tailed) inter-cycle wait now so the reported
            # next-send time below matches the sleep actually taken at the end
            # of the cycle, rather than the raw interval.
            wait_time = recurring_wait(ctx, interval)

            try:
                result = await farm_service.send_farm_list(list_id)

                if result.targets and result.targets[0].error == "plus.error_goldclub":
                    ctx.push(
                        {
                            "type": "error",
                            "message": "Gold Club not active - cannot send farm lists",
                            "fatal": True,
                        }
                    )
                    break

                cycle_success = result.success_count
                cycle_fail = result.fail_count
                total_success += cycle_success
                total_fail += cycle_fail

                for t in result.targets:
                    is_ok = t.error == ""
                    if verbose or not is_ok:
                        ctx.push(
                            {
                                "type": "result",
                                "cycle": cycle,
                                "slot_id": t.id,
                                "success": is_ok,
                                "status": t.status,
                                "error": t.error or None,
                            }
                        )

                next_time = time.time() + wait_time
                next_send = (
                    None
                    if end_time and next_time >= end_time
                    else datetime.fromtimestamp(next_time).isoformat(timespec="seconds")
                )
                ctx.push(
                    {
                        "type": "cycle_end",
                        "cycle": cycle,
                        "sent": cycle_success,
                        "failed": cycle_fail,
                        "total": cycle_success + cycle_fail,
                        "cumulative_success": total_success,
                        "cumulative_fail": total_fail,
                        "timestamp": _now_iso(),
                        "next_send_at": next_send,
                    }
                )
            except Exception as exc:
                ctx.push(
                    {
                        "type": "error",
                        "cycle": cycle,
                        "message": str(exc),
                        "fatal": False,
                        "timestamp": _now_iso(),
                    }
                )

            if await interruptible_sleep(ctx, wait_time):
                break

        ctx.push(
            {
                "type": "complete",
                "reason": (
                    "duration_elapsed" if (end_time and time.time() >= end_time) else "stopped"
                ),
                "total_cycles": cycle,
                "total_success": total_success,
                "total_fail": total_fail,
                "timestamp": _now_iso(),
            }
        )

    return coro


# ---------------------------------------------------------------------------
# Run-all loop coroutine
# ---------------------------------------------------------------------------


def _build_farm_run_all_coro(
    send_ids: list[int],
    list_names_csv: str,
    total_active: int,
    interval: int,
    duration: int,
    verbose: bool,
    list_ids_param: str,
):
    """Returns the coroutine OperationManager will run for the run-all op."""

    async def coro(ctx: OperationContext) -> None:
        farm_service = ctx.session.farm_service

        cli_parts = [f"travian farm run-all --interval {interval} --duration {duration}"]
        if list_ids_param:
            cli_parts.append(f"--lists {list_ids_param}")
        if verbose:
            cli_parts.append("--verbose")
        ctx.push({"type": "trigger_info", "command": " ".join(cli_parts)})

        ctx.push(
            {
                "type": "info",
                "lists": list_names_csv,
                "list_ids": send_ids,
                "total_active_slots": total_active,
                "interval": interval,
                "duration": duration,
                "verbose": verbose,
            }
        )

        end_time = time.time() + duration * 60 if duration else None
        total_success = 0
        total_fail = 0
        cycle = 0

        while True:
            if end_time and time.time() >= end_time:
                break
            if ctx.should_stop():
                break

            # Go quiet overnight and resume in the morning — a graceful, VISIBLE
            # pause (see night_rest_pause), not the fatal budget path below.
            # Gated on TOTAL run length (fixed, not the shrinking remaining
            # time): unbounded runs and long bounded runs that genuinely span a
            # night rest; a short bounded run is a deliberate fixed session and
            # runs through so its cadence isn't shredded by a mid-session sleep.
            # The deadline caps the sleep so even a long run ending mid-night
            # stops on time instead of lingering asleep past its finish.
            if (duration == 0 or duration * 60 > _NIGHT_REST_MIN_RUN_S) and await night_rest_pause(
                ctx, deadline=end_time
            ):
                break
            # A deadline-capped pause can return right at end_time; re-check here
            # so a bounded run doesn't fire one more cycle past its deadline (the
            # loop-top check alone runs only after the cycle below).
            if end_time and time.time() >= end_time:
                break

            try:
                ctx.session.http_client.check_activity_budget()
            except ActivityBudgetExhausted as exc:
                ctx.push({"type": "error", "message": str(exc), "fatal": True})
                break

            cycle += 1
            ctx.push({"type": "cycle_start", "cycle": cycle, "timestamp": _now_iso()})
            # Draw the (heavy-tailed) inter-cycle wait now so the reported
            # next-send time below matches the sleep actually taken at the end
            # of the cycle, rather than the raw interval.
            wait_time = recurring_wait(ctx, interval)

            try:
                results = await farm_service.send_all_farm_lists(send_ids)

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

                    failed_targets = [t for t in result.targets if t.error != ""]
                    if verbose or failed_targets:
                        targets_to_report = result.targets if verbose else failed_targets
                        ctx.push(
                            {
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
                            }
                        )

                if gold_club_error:
                    ctx.push(
                        {
                            "type": "error",
                            "message": "Gold Club not active - cannot send farm lists",
                            "fatal": True,
                        }
                    )
                    break

                total_success += cycle_success
                total_fail += cycle_fail

                next_time = time.time() + wait_time
                next_send = (
                    None
                    if end_time and next_time >= end_time
                    else datetime.fromtimestamp(next_time).isoformat(timespec="seconds")
                )
                ctx.push(
                    {
                        "type": "cycle_end",
                        "cycle": cycle,
                        "sent": cycle_success,
                        "failed": cycle_fail,
                        "total": cycle_success + cycle_fail,
                        "cumulative_success": total_success,
                        "cumulative_fail": total_fail,
                        "timestamp": _now_iso(),
                        "next_send_at": next_send,
                    }
                )
            except Exception as exc:
                ctx.push(
                    {
                        "type": "error",
                        "cycle": cycle,
                        "message": str(exc),
                        "fatal": False,
                        "timestamp": _now_iso(),
                    }
                )

            if await interruptible_sleep(ctx, wait_time):
                break

        ctx.push(
            {
                "type": "complete",
                "reason": (
                    "duration_elapsed" if (end_time and time.time() >= end_time) else "stopped"
                ),
                "total_cycles": cycle,
                "total_success": total_success,
                "total_fail": total_fail,
                "timestamp": _now_iso(),
            }
        )

    return coro


# ---------------------------------------------------------------------------
# WS /ws/farm/run/{list_id}
# ---------------------------------------------------------------------------


@router.websocket("/ws/farm/run/{list_id}")
async def ws_farm_run(websocket: WebSocket, list_id: int) -> None:
    """Loop-send a single farm list at a fixed interval."""

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

    params = websocket.query_params
    # Stealth floor: a 10s farm-list cadence is impossible to mistake for
    # a human. When stealth is on, refuse to run faster than the floor —
    # bots can be configured short, players can't click that fast.
    stealth_enabled = bool(getattr(session.http_client, "stealth_enabled", False))
    floor = 60 if stealth_enabled else 10
    try:
        interval = max(floor, int(params.get("interval", "300")))
        duration = max(0, int(params.get("duration", "0")))
    except (ValueError, TypeError):
        interval, duration = 300, 0
    verbose = params.get("verbose", "false").lower() in ("true", "1", "yes")

    op_label = f"farm:{list_id}"

    # Policy: shared FarmListService._cursors[list_id] state means two loops
    # on the same list would alternate cursor advances and double the send
    # rate. Reject the second cleanly so the client sees the existing op.
    if op_label in active_ops.get_active(user_id):
        await websocket.accept()
        existing = next(
            (op for op in operation_manager.list_for_user(user_id) if op.label == op_label),
            None,
        )
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": f"A farm loop is already running for list {list_id}",
            }
        )
        await websocket.close(code=4009, reason="Farm loop already running for this list")
        return

    await websocket.accept()

    if not await _wait_for_start(websocket):
        await websocket.close(code=1000, reason="Cancelled before start")
        return

    op = operation_manager.start(
        user_id=user_id,
        label=op_label,
        session_type="farm-run",
        session_label=f"Farm Run - #{list_id}",
        session=session,
        coro=_build_farm_run_coro(list_id, interval, duration, verbose),
        require_unique_label=True,
    )
    if op is None:
        existing = next(
            (o for o in operation_manager.list_for_user(user_id) if o.label == op_label),
            None,
        )
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": f"A farm loop is already running for list {list_id}",
            }
        )
        await websocket.close(code=4009, reason="Farm loop already running for this list")
        return

    await subscribe_and_tail(websocket, user_id, f"farm_run_{list_id}", op.session_id)


# ---------------------------------------------------------------------------
# WS /ws/farm/run-all
# ---------------------------------------------------------------------------


@router.websocket("/ws/farm/run-all")
async def ws_farm_run_all(websocket: WebSocket) -> None:
    """Loop-send all (or specified) farm lists at a fixed interval."""

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

    params = websocket.query_params
    # Stealth floor: a 10s farm-list cadence is impossible to mistake for
    # a human. When stealth is on, refuse to run faster than the floor —
    # bots can be configured short, players can't click that fast.
    stealth_enabled = bool(getattr(session.http_client, "stealth_enabled", False))
    floor = 60 if stealth_enabled else 10
    try:
        interval = max(floor, int(params.get("interval", "300")))
        duration = max(0, int(params.get("duration", "0")))
    except (ValueError, TypeError):
        interval, duration = 300, 0
    verbose = params.get("verbose", "false").lower() in ("true", "1", "yes")

    list_ids_param = params.get("list_ids", "")
    requested_ids: list[int] | None = None
    if list_ids_param:
        try:
            requested_ids = [int(x.strip()) for x in list_ids_param.split(",") if x.strip()]
        except ValueError:
            await websocket.accept()
            await websocket.send_json(
                {"type": "error", "message": "Invalid list_ids parameter", "fatal": True}
            )
            await websocket.close(code=4002)
            return

    await websocket.accept()

    # Resolve the actual list set up-front so we can do per-list policy
    # check before spawning the op (and before creating an exec_session).
    try:
        all_lists = await session.farm_service.get_all_farm_lists()
    except Exception as exc:
        await websocket.send_json(
            {"type": "error", "message": f"Failed to fetch farm lists: {exc}", "fatal": True}
        )
        await websocket.close(code=1011)
        return

    if requested_ids:
        all_lists = [fl for fl in all_lists if fl.id in requested_ids]

    if not all_lists:
        await websocket.send_json(
            {"type": "error", "message": "No farm lists found", "fatal": True}
        )
        await websocket.close(code=1000)
        return

    send_ids = [fl.id for fl in all_lists]
    total_active = sum(len(fl.active_slots) for fl in all_lists)
    list_names_csv = ", ".join(fl.name for fl in all_lists)

    # Per-list policy: refuse if any requested list already has a loop.
    already_active = active_ops.get_active(user_id)
    conflicts = [lid for lid in send_ids if f"farm:{lid}" in already_active]
    if conflicts:
        await websocket.send_json(
            {
                "type": "error",
                "message": (
                    f"Farm loop already running for list(s): {conflicts}. "
                    "Stop them first or choose disjoint lists."
                ),
                "fatal": True,
            }
        )
        await websocket.close(code=4009)
        return

    if not await _wait_for_start(websocket):
        await websocket.close(code=1000, reason="Cancelled before start")
        return

    op = operation_manager.start(
        user_id=user_id,
        label="farm-all",
        extra_labels=[f"farm:{lid}" for lid in send_ids],
        session_type="farm-run-all",
        session_label=f"Farm Run All ({len(all_lists)} lists)",
        session=session,
        coro=_build_farm_run_all_coro(
            send_ids=send_ids,
            list_names_csv=list_names_csv,
            total_active=total_active,
            interval=interval,
            duration=duration,
            verbose=verbose,
            list_ids_param=list_ids_param,
        ),
        # Disjoint run-alls are intentionally allowed to coexist — only
        # the per-list ``farm:{lid}`` extras need to be unique. Setting
        # require_unique_label here would block a second run-all over a
        # completely disjoint list set, which is a feature, not a bug.
        require_unique_extras=True,
    )
    if op is None:
        # A second tab beat us to a list in the same set. Re-surface which.
        already = set(active_ops.get_active(user_id))
        conflicts = [lid for lid in send_ids if f"farm:{lid}" in already]
        await websocket.send_json(
            {
                "type": "error",
                "message": (
                    f"Farm loop already running for list(s): {conflicts}. "
                    "Stop them first or choose disjoint lists."
                ),
                "fatal": True,
            }
        )
        await websocket.close(code=4009)
        return

    await subscribe_and_tail(websocket, user_id, "farm_run_all", op.session_id)
