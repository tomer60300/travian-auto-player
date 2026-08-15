"""WebSocket handler for build queue execution.

Endpoint: WS /ws/queue/run?token=<JWT>

The plan execution runs as a managed background operation, decoupled from
the WebSocket lifetime — backgrounding Safari (or any WS drop) does NOT
abort the queue. Reconnect via ``/ws/sessions/{id}/stream`` to resume the
live status feed with full message history.

Protocol:
    1. Client connects with JWT in query param.
    2. Client sends config message:
       {"yaml_content": "...", "poll_interval": 30, "use_video": false, "verbose": false}
    3. Server streams JSON messages:
       - {"type": "status", "message": "..."}        -- progress updates
       - {"type": "step_complete", "building": "...", "level": N, "success": true/false}
       - {"type": "complete", "results": [...]}       -- plan finished
       - {"type": "error", "message": "..."}          -- fatal error
       - {"type": "operation_complete", "status": ...} -- terminal, pushed by manager
    4. Client can send {"action": "stop"} to request graceful abort.
"""

from __future__ import annotations

import asyncio
import json
import logging

import yaml
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.operation_manager import OperationContext, operation_manager
from travian_api.services.build_queue_service import BuildPlan, BuildPlanItem
from travian_api.web.log_broadcast import log_stream_manager
from travian_api.web.operation_gate import active_ops
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws._resumable import subscribe_and_tail
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


def _build_queue_coro(
    plan: BuildPlan,
    village_label: str,
    yaml_content: str,
    poll_interval: int,
    use_video: bool,
    verbose: bool,
):
    """Returns the OperationManager coroutine that executes a build plan."""

    async def coro(ctx: OperationContext) -> None:
        session: TravianSession = ctx.session
        service = session.build_queue_service

        # Resync exec_session label now that the village is known.
        ctx.exec_session.label = f"Build Queue - {village_label}"

        cli_parts = [
            f"travian queue run plan.yaml --village {village_label} --poll {poll_interval}"
        ]
        if use_video:
            cli_parts.append("--use-video")
        if verbose:
            cli_parts.append("--verbose")

        ctx.push(
            {
                "type": "trigger_info",
                "command": " ".join(cli_parts),
                "plan_yaml": yaml_content,
            }
        )
        ctx.push(
            {
                "type": "status",
                "message": f"Parsed plan: village {village_label}, {len(plan.items)} items",
            }
        )
        log_stream_manager.push(
            {
                "level": "info",
                "source": "build_queue",
                "message": (f"Build queue started: {village_label}, {len(plan.items)} items"),
                "user_id": ctx.user_id,
            }
        )

        # Up-front guards.
        try:
            session.http_client.check_activity_budget()
        except ActivityBudgetExhausted as exc:
            ctx.push({"type": "error", "message": str(exc), "fatal": True})
            return

        # Service status callbacks come from a worker thread; bridge them to
        # the loop via a thread-safe queue + drainer task.
        status_queue: asyncio.Queue[str | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def _sync_status_callback(msg: str) -> None:
            loop.call_soon_threadsafe(status_queue.put_nowait, msg)

        async def _drain_status_queue() -> None:
            while True:
                msg = await status_queue.get()
                if msg is None:
                    return
                ctx.push({"type": "status", "message": msg})

        service.add_status_callback(_sync_status_callback)
        drainer_task = asyncio.create_task(_drain_status_queue())

        # Watch for stop while the plan runs and cancel the exec task if so.
        async def _wait_for_stop() -> None:
            while not ctx.should_stop():
                if await ctx.wait_or_stop(0.5):
                    return

        exec_task: asyncio.Task | None = None
        stop_watcher: asyncio.Task | None = None
        try:
            exec_task = asyncio.create_task(
                service.execute_plan_continuous(
                    plan,
                    poll_interval_s=poll_interval,
                    use_video=use_video,
                    verbose=verbose,
                )
            )
            stop_watcher = asyncio.create_task(_wait_for_stop())

            done, _pending = await asyncio.wait(
                {exec_task, stop_watcher},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if ctx.should_stop() and exec_task and not exec_task.done():
                exec_task.cancel()
                try:
                    await exec_task
                except asyncio.CancelledError:
                    pass
                ctx.push({"type": "status", "message": "Execution stopped by client"})
                results: list[dict] = []
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
                ctx.push({"type": "complete", "results": results})
                log_stream_manager.push(
                    {
                        "level": "warning",
                        "source": "build_queue",
                        "message": "Build queue stopped by user",
                        "user_id": ctx.user_id,
                    }
                )
                return

            # Normal completion path.
            results = exec_task.result() if exec_task in done else []
            for r in results:
                ok = r.get("status") == "started"
                log_stream_manager.push(
                    {
                        "level": "info",
                        "source": "build_queue",
                        "message": (
                            f"{r.get('building', '?')} -> Lv{r.get('level', '?')}: "
                            f"{'OK' if ok else 'FAIL'}"
                        ),
                        "user_id": ctx.user_id,
                    }
                )
                ctx.push(
                    {
                        "type": "step_complete",
                        "building": r.get("building", ""),
                        "level": r.get("level", ""),
                        "success": ok,
                    }
                )
            ctx.push({"type": "complete", "results": results})
            log_stream_manager.push(
                {
                    "level": "success",
                    "source": "build_queue",
                    "message": f"Build queue completed ({len(results)} steps)",
                    "user_id": ctx.user_id,
                }
            )
        finally:
            service.remove_status_callback(_sync_status_callback)
            status_queue.put_nowait(None)
            for t in (drainer_task, stop_watcher):
                if t is not None and not t.done():
                    t.cancel()
            for t in (drainer_task, stop_watcher):
                if t is None:
                    continue
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    return coro


@router.websocket("/ws/queue/run")
async def queue_run_ws(websocket: WebSocket) -> None:
    """Execute a build plan over WebSocket with real-time status streaming."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: TravianSession | None = session_manager.get(user_id)
    if session is None:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="No active Travian session",
        )
        return

    await websocket.accept()

    # Receive config first so we can do the per-village policy check before
    # spawning anything (and avoid creating an exec_session for a rejected run).
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=30)
        config = json.loads(raw)
    except TimeoutError:
        await websocket.send_json(
            {"type": "error", "message": "Timed out waiting for config message", "fatal": True}
        )
        await websocket.close(code=4002)
        return
    except (json.JSONDecodeError, WebSocketDisconnect, RuntimeError) as exc:
        try:
            await websocket.send_json(
                {"type": "error", "message": f"Invalid config: {exc}", "fatal": True}
            )
        except Exception:
            pass
        return

    # Accept both bare-config and {action:"start", config:{...}} shapes so
    # the resumable-hook frontend and any older clients both work.
    if (
        isinstance(config, dict)
        and config.get("action") == "start"
        and isinstance(config.get("config"), dict)
    ):
        config = config["config"]

    yaml_content: str = config.get("yaml_content", "") if isinstance(config, dict) else ""
    raw_poll: int = config.get("poll_interval", 30) if isinstance(config, dict) else 30
    # Stealth floor on poll cadence: a configured 1s poll forces bot-like
    # retry timing regardless of what the inner service does. Clamp to a
    # 30s floor and 1h ceiling — humans don't reload faster than that, and
    # don't wait longer than that for a queue check either.
    poll_interval: int = max(30, min(int(raw_poll), 3600))
    use_video: bool = bool(config.get("use_video", False)) if isinstance(config, dict) else False
    verbose: bool = bool(config.get("verbose", False)) if isinstance(config, dict) else False

    if not yaml_content:
        await websocket.send_json(
            {"type": "error", "message": "yaml_content is required", "fatal": True}
        )
        await websocket.close(code=4002)
        return

    try:
        plan = _parse_yaml_to_plan(yaml_content)
    except (yaml.YAMLError, ValueError) as exc:
        await websocket.send_json(
            {"type": "error", "message": f"Invalid build plan: {exc}", "fatal": True}
        )
        await websocket.close(code=4002)
        return

    # The plan must name its village explicitly. Falling back to the session
    # default (pinned to the login village since selection became tab-local)
    # would run the build queue against the wrong village — a wasted, wrong
    # sequence of Travian requests. Fail fast instead.
    if not plan.village_id:
        await websocket.send_json(
            {
                "type": "error",
                "message": "Build plan is missing village_id.",
                "fatal": True,
            }
        )
        await websocket.close(code=4002)
        return

    op_label = f"queue:{plan.village_id}"
    if op_label in active_ops.get_active(user_id):
        existing = next(
            (op for op in operation_manager.list_for_user(user_id) if op.label == op_label),
            None,
        )
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "A queue is already running for this village",
            }
        )
        await websocket.close(code=4009)
        return

    village_label = str(plan.village_id)
    if session.auth_state:
        for v in session.auth_state.villages:
            if v.id == plan.village_id:
                village_label = f"{v.name} ({v.id})"
                break

    op = operation_manager.start(
        user_id=user_id,
        label=op_label,
        session_type="queue",
        session_label=f"Build Queue - {village_label}",
        session=session,
        coro=_build_queue_coro(
            plan=plan,
            village_label=village_label,
            yaml_content=yaml_content,
            poll_interval=poll_interval,
            use_video=use_video,
            verbose=verbose,
        ),
        require_unique_label=True,
    )
    if op is None:
        existing = operation_manager.find_by_label(user_id, op_label)
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "A queue is already running for this village",
            }
        )
        await websocket.close(code=4009)
        return

    await subscribe_and_tail(websocket, user_id, CHANNEL, op.session_id)
