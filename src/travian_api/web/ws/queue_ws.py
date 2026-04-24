"""WebSocket handler for build queue execution.

Endpoint: WS /ws/queue/run?token=<JWT>

Protocol:
    1. Client connects with JWT in query param.
    2. Client sends config message:
       {"yaml_content": "...", "poll_interval": 30, "use_video": false, "verbose": false}
    3. Server streams JSON messages:
       - {"type": "status", "message": "..."}        -- progress updates
       - {"type": "step_complete", "building": "...", "level": N, "success": true/false}
       - {"type": "complete", "results": [...]}       -- plan finished
       - {"type": "error", "message": "..."}          -- fatal error
    4. Client can send {"action": "stop"} to abort execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import yaml
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.services.build_queue_service import BuildPlan, BuildPlanItem
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.operation_gate import active_ops, captcha_stop
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "queue"


def _parse_yaml_to_plan(yaml_content: str) -> BuildPlan:
    """Parse a YAML string into a BuildPlan (same logic as the REST route)."""
    raw = yaml_content.replace("\t", "  ")
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise ValueError("YAML must be a mapping with 'village' and 'plan' keys.")

    raw_vid = data.get("village", data.get("village_id", 0))
    # Sanitise: YAML may parse "auto" as a string or 0 as falsy.
    # Ensure we always end up with an int (0 means "use session default").
    try:
        village_id = int(raw_vid)
    except (TypeError, ValueError):
        village_id = 0
    plan_entries = data.get("plan", [])

    if not isinstance(plan_entries, list) or not plan_entries:
        raise ValueError("YAML must contain a non-empty 'plan' list.")

    items: list[BuildPlanItem] = []
    for entry in plan_entries:
        if not isinstance(entry, dict):
            raise ValueError(
                f"Each plan entry must be a mapping, got {type(entry).__name__}: {entry!r}"
            )
        items.append(
            BuildPlanItem(
                building=entry.get("building", ""),
                target=entry.get("target", entry.get("level", 1)),
                priority=entry.get("priority", 5),
                slot=entry.get("slot", 0),
                expect=entry.get("expect", ""),
            )
        )

    return BuildPlan(village_id=village_id, items=items)


async def _send(ws: WebSocket, data: dict) -> None:
    """Send JSON to the WebSocket; raise on failure so callers can handle disconnect."""
    try:
        await ws.send_json(data)
    except (RuntimeError, WebSocketDisconnect):
        raise WebSocketDisconnect()


async def _try_send(ws: WebSocket, data: dict) -> bool:
    """Best-effort send — returns False (no exception) if the WS is already closed."""
    try:
        await ws.send_json(data)
        return True
    except (RuntimeError, WebSocketDisconnect, Exception):
        return False


@router.websocket("/ws/queue/run")
async def queue_run_ws(websocket: WebSocket):
    """Execute a build plan over WebSocket with real-time status streaming."""

    # ── Authenticate ──────────────────────────────────────────────────
    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return  # authenticate() already closed the socket

    session: TravianSession | None = session_manager.get(user_id)
    if session is None:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="No active Travian session",
        )
        return

    op_type = None  # Set after village_id is known (per-village gate)
    gate_acquired = False

    try:
        op_started_at = time.monotonic()

        await ws_manager.connect(websocket, user_id, CHANNEL)

        exec_session = exec_session_manager.create(user_id, "queue", "Build Queue")

        async def _tracked_send(ws: WebSocket, data: dict) -> None:
            await _send(ws, data)
            exec_session_manager.push(exec_session.id, data)

        async def _tracked_try_send(ws: WebSocket, data: dict) -> bool:
            ok = await _try_send(ws, data)
            exec_session_manager.push(exec_session.id, data)
            return ok

        def _broadcast_log(message: str, level: str = "info") -> None:
            """Push a log entry to the shared log stream for the Logs page."""
            log_stream_manager.push(
                {
                    "timestamp": time.time(),
                    "level": level,
                    "source": "build_queue",
                    "message": message,
                    "user_id": user_id,
                }
            )

        # ── Event used to signal cancellation from a client "stop" message ──
        stop_event = asyncio.Event()

        # ── Wait for the config message ───────────────────────────────
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            config = json.loads(raw)
        except TimeoutError:
            await _tracked_send(
                websocket, {"type": "error", "message": "Timed out waiting for config message"}
            )
            return
        except (json.JSONDecodeError, WebSocketDisconnect) as exc:
            await _tracked_send(websocket, {"type": "error", "message": f"Invalid config: {exc}"})
            return

        yaml_content: str = config.get("yaml_content", "")
        poll_interval: int = config.get("poll_interval", 30)
        use_video: bool = config.get("use_video", False)
        verbose: bool = config.get("verbose", False)

        if not yaml_content:
            await _tracked_send(websocket, {"type": "error", "message": "yaml_content is required"})
            return

        # ── Parse the YAML plan ───────────────────────────────────────
        try:
            plan = _parse_yaml_to_plan(yaml_content)
        except (yaml.YAMLError, ValueError) as exc:
            await _tracked_send(
                websocket, {"type": "error", "message": f"Invalid build plan: {exc}"}
            )
            return

        # Fall back to session active village when YAML has no village_id
        if not plan.village_id and session.active_village_id:
            plan.village_id = session.active_village_id

        # Policy (not a mutex): a second queue loop on the same village would
        # just duplicate polling work — the service-layer slot lock already
        # prevents double-upgrades. Refuse cleanly so the user sees feedback.
        op_type = f"queue:{plan.village_id}"
        if op_type in active_ops.get_active(user_id):
            await _tracked_send(
                websocket,
                {
                    "type": "error",
                    "message": "A queue is already running for this village",
                },
            )
            return
        active_ops.register(user_id, op_type)
        gate_acquired = True

        # Resolve village name for logging
        village_label = str(plan.village_id)
        if session.auth_state:
            for v in session.auth_state.villages:
                if v.id == plan.village_id:
                    village_label = f"{v.name} ({v.id})"
                    break

        exec_session.label = f"Build Queue - {village_label}"
        await _tracked_send(websocket, {"type": "session_init", "session_id": exec_session.id})
        # Build equivalent CLI command for display
        cli_parts = [
            f"travian queue run plan.yaml --village {village_label} --poll {poll_interval}"
        ]
        if use_video:
            cli_parts.append("--use-video")
        if verbose:
            cli_parts.append("--verbose")
        await _tracked_send(
            websocket,
            {
                "type": "trigger_info",
                "command": " ".join(cli_parts),
                "plan_yaml": yaml_content,
            },
        )
        await _tracked_send(
            websocket,
            {
                "type": "status",
                "message": f"Parsed plan: village {village_label}, {len(plan.items)} items",
            },
        )
        _broadcast_log(f"Build queue started: {village_label}, {len(plan.items)} items")

        # ── Wire up the on_status callback ────────────────────────────
        # Use add/remove to register a per-connection callback instead of
        # overwriting the shared _on_status — safe for concurrent WS/REST calls.
        service = session.build_queue_service

        # Use a thread-safe queue so the sync callback never blocks and
        # messages are drained in order by an async task.
        status_queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _sync_status_callback(msg: str) -> None:
            loop.call_soon_threadsafe(status_queue.put_nowait, msg)

        async def _drain_status_queue() -> None:
            """Continuously drain status messages and send them over WS."""
            try:
                while True:
                    msg = await status_queue.get()
                    if msg is None:
                        break
                    await _tracked_send(websocket, {"type": "status", "message": msg})
            except WebSocketDisconnect:
                pass

        # Check if captcha was just resolved
        if captcha_stop.should_stop(user_id, op_started_at):
            await _tracked_send(
                websocket,
                {"type": "error", "message": "Stopped after captcha resolution — restart manually"},
            )
            return

        # Activity budget check before starting execution
        try:
            session.http_client.check_activity_budget()
        except ActivityBudgetExhausted as exc:
            await _tracked_send(websocket, {"type": "error", "message": str(exc)})
            return

        service.add_status_callback(_sync_status_callback)
        drainer_task = asyncio.create_task(_drain_status_queue())

        # ── Listener task: watch for client "stop" messages ───────────
        async def _listen_for_stop() -> None:
            try:
                while True:
                    raw_msg = await websocket.receive_text()
                    try:
                        msg = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("action") == "stop":
                        stop_event.set()
                        await _tracked_send(
                            websocket,
                            {
                                "type": "status",
                                "message": "Stop requested -- aborting after current step",
                            },
                        )
                        return
            except WebSocketDisconnect:
                stop_event.set()

        listener_task = asyncio.create_task(_listen_for_stop())

        # ── Execute the plan ──────────────────────────────────────────
        try:
            # Wrap execute_plan_continuous so we can cancel it on stop
            exec_task = asyncio.create_task(
                service.execute_plan_continuous(
                    plan,
                    poll_interval_s=poll_interval,
                    use_video=use_video,
                    verbose=verbose,
                )
            )

            # Wait for either execution to finish or stop signal
            stop_task = asyncio.create_task(stop_event.wait())
            done, pending = await asyncio.wait(
                {exec_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # If stop was triggered, cancel the execution
            if stop_event.is_set() and not exec_task.done():
                exec_task.cancel()
                try:
                    await exec_task
                except asyncio.CancelledError:
                    pass
                await _tracked_try_send(
                    websocket,
                    {
                        "type": "status",
                        "message": "Execution stopped by client",
                    },
                )
                results = []
                for item in plan.items:
                    if item.status != "pending":
                        results.append(
                            {
                                "building": item.building,
                                "slot_id": item.slot_id,
                                "level": f"{item.current_level}/{item.target}",
                                "status": item.status,
                            }
                        )
                await _tracked_try_send(websocket, {"type": "complete", "results": results})
                _broadcast_log("Build queue stopped by user", "warning")
            else:
                # Normal completion
                results = exec_task.result()

                for r in results:
                    status_str = "OK" if r.get("status") == "started" else "FAIL"
                    _broadcast_log(
                        f"{r.get('building', '?')} -> Lv{r.get('level', '?')}: {status_str}"
                    )
                    await _tracked_try_send(
                        websocket,
                        {
                            "type": "step_complete",
                            "building": r.get("building", ""),
                            "level": r.get("level", ""),
                            "success": r.get("status") == "started",
                        },
                    )

                await _tracked_try_send(websocket, {"type": "complete", "results": results})
                _broadcast_log(f"Build queue completed ({len(results)} steps)", "success")

            # Cancel any pending wait tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except WebSocketDisconnect:
            logger.info("Queue WS disconnected mid-execution: user=%s", user_id)
        except Exception as exc:
            logger.exception("Build queue execution failed for user %s", user_id)
            await _tracked_try_send(websocket, {"type": "error", "message": str(exc)})
            _broadcast_log(f"Build queue error: {exc}", "error")
        finally:
            # Unregister this connection's callback and stop the drainer
            service.remove_status_callback(_sync_status_callback)
            status_queue.put_nowait(None)  # signal drainer to exit
            drainer_task.cancel()
            listener_task.cancel()
            for t in (drainer_task, listener_task):
                try:
                    await t
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        logger.info("Queue WS disconnected: user=%s", user_id)
    except Exception as exc:
        logger.exception("Unexpected error in queue WS for user %s", user_id)
        await _tracked_try_send(websocket, {"type": "error", "message": f"Internal error: {exc}"})
    finally:
        if gate_acquired and op_type:
            active_ops.unregister(user_id, op_type)
        exec_session_manager.mark_disconnected(exec_session.id)
        await ws_manager.disconnect(user_id, CHANNEL, websocket)
