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

import yaml
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from travian_api.services.build_queue_service import BuildPlan, BuildPlanItem
from travian_api.web.sessions import session_manager, TravianSession
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

    village_id = data.get("village", data.get("village_id", 0))
    plan_entries = data.get("plan", [])

    if not isinstance(plan_entries, list) or not plan_entries:
        raise ValueError("YAML must contain a non-empty 'plan' list.")

    items: list[BuildPlanItem] = []
    for entry in plan_entries:
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
    """Send JSON to the WebSocket, silently ignoring closed connections."""
    try:
        await ws.send_json(data)
    except Exception:
        logger.debug("Failed to send JSON over WS in queue handler", exc_info=True)


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

    await ws_manager.connect(websocket, user_id, CHANNEL)

    # ── Event used to signal cancellation from a client "stop" message ──
    stop_event = asyncio.Event()

    try:
        # ── Wait for the config message ───────────────────────────────
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
            config = json.loads(raw)
        except asyncio.TimeoutError:
            await _send(websocket, {"type": "error", "message": "Timed out waiting for config message"})
            return
        except (json.JSONDecodeError, WebSocketDisconnect) as exc:
            await _send(websocket, {"type": "error", "message": f"Invalid config: {exc}"})
            return

        yaml_content: str = config.get("yaml_content", "")
        poll_interval: int = config.get("poll_interval", 30)
        use_video: bool = config.get("use_video", False)
        verbose: bool = config.get("verbose", False)

        if not yaml_content:
            await _send(websocket, {"type": "error", "message": "yaml_content is required"})
            return

        # ── Parse the YAML plan ───────────────────────────────────────
        try:
            plan = _parse_yaml_to_plan(yaml_content)
        except (yaml.YAMLError, ValueError) as exc:
            await _send(websocket, {"type": "error", "message": f"Invalid build plan: {exc}"})
            return

        await _send(websocket, {
            "type": "status",
            "message": f"Parsed plan: village {plan.village_id}, {len(plan.items)} items",
        })

        # ── Wire up the on_status callback ────────────────────────────
        service = session.build_queue_service
        prev_callback = service._on_status

        async def _stream_status(msg: str) -> None:
            await _send(websocket, {"type": "status", "message": msg})

        # The service callback is synchronous (Callable[[str], None]),
        # so we schedule the async send onto the running event loop.
        loop = asyncio.get_running_loop()

        def _sync_status_callback(msg: str) -> None:
            asyncio.run_coroutine_threadsafe(_stream_status(msg), loop)

        service.on_status(_sync_status_callback)

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
                        await _send(websocket, {
                            "type": "status",
                            "message": "Stop requested -- aborting after current step",
                        })
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
            done, pending = await asyncio.wait(
                {exec_task, asyncio.create_task(stop_event.wait())},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # If stop was triggered, cancel the execution
            if stop_event.is_set() and not exec_task.done():
                exec_task.cancel()
                try:
                    await exec_task
                except asyncio.CancelledError:
                    pass
                await _send(websocket, {
                    "type": "status",
                    "message": "Execution stopped by client",
                })
                # Collect partial results from completed items
                results = []
                for item in plan.items:
                    if item.status != "pending":
                        results.append({
                            "building": item.building,
                            "slot_id": item.slot_id,
                            "level": f"{item.current_level}/{item.target}",
                            "status": item.status,
                        })
                await _send(websocket, {"type": "complete", "results": results})
            else:
                # Normal completion
                results = exec_task.result()

                # Send step_complete for each result
                for r in results:
                    await _send(websocket, {
                        "type": "step_complete",
                        "building": r.get("building", ""),
                        "level": r.get("level", ""),
                        "success": r.get("status") == "started",
                    })

                await _send(websocket, {"type": "complete", "results": results})

            # Cancel any pending wait tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as exc:
            logger.exception("Build queue execution failed for user %s", user_id)
            await _send(websocket, {"type": "error", "message": str(exc)})
        finally:
            # Restore the previous status callback
            service._on_status = prev_callback
            listener_task.cancel()
            try:
                await listener_task
            except asyncio.CancelledError:
                pass

    except WebSocketDisconnect:
        logger.info("Queue WS disconnected: user=%s", user_id)
    except Exception as exc:
        logger.exception("Unexpected error in queue WS for user %s", user_id)
        await _send(websocket, {"type": "error", "message": f"Internal error: {exc}"})
    finally:
        await ws_manager.disconnect(user_id, CHANNEL)
