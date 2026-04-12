"""WebSocket handler for live raid analysis with progress streaming."""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from travian_api.web.sessions import session_manager
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws/reports/analyze")
async def ws_analyze_reports(websocket: WebSocket):
    """Stream raid analysis progress and partial results over WebSocket."""

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

    channel = "analyze_reports"
    await ws_manager.connect(websocket, user_id, channel)

    try:
        # Wait for config message
        try:
            msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
        except asyncio.TimeoutError:
            await websocket.send_json({"type": "error", "message": "Timeout waiting for config"})
            return

        from travian_api.models.raid_analyzer import AnalyzerSettings

        settings = AnalyzerSettings(
            village_id=msg.get("village_id") or session.active_village_id,
            min_resources=msg.get("min_resources", 200),
            max_report_age_hours=msg.get("max_report_age_hours", 24),
            max_pages=msg.get("max_pages", 3),
            exclude_alliances=msg.get("exclude_alliances", []),
            exclude_players=msg.get("exclude_players", []),
            smithy_level=msg.get("smithy_level", 0),
            hero_offense=msg.get("hero_offense", 0),
            hero_strength=msg.get("hero_strength", 0),
            radius=msg.get("radius"),
            output_json=True,
        )

        analyzer = session.raid_analyzer

        # Progress queue for async delivery
        progress_queue: asyncio.Queue = asyncio.Queue()

        def on_progress(phase: str, message: str, detail: dict):
            try:
                progress_queue.put_nowait({
                    "type": "progress",
                    "phase": phase,
                    "message": message,
                    **detail,
                })
            except asyncio.QueueFull:
                pass

        analyzer.on_progress(on_progress)

        # Send progress messages as they arrive
        async def send_progress():
            while True:
                try:
                    msg = await asyncio.wait_for(progress_queue.get(), timeout=0.5)
                    await websocket.send_json(msg)
                except asyncio.TimeoutError:
                    continue
                except (WebSocketDisconnect, Exception):
                    break

        progress_task = asyncio.create_task(send_progress())

        try:
            await websocket.send_json({"type": "progress", "phase": "start", "message": "Analysis starting..."})

            result = await analyzer.analyze(settings)

            # Cancel progress sender
            progress_task.cancel()
            try:
                await progress_task
            except asyncio.CancelledError:
                pass

            # Drain remaining progress messages
            while not progress_queue.empty():
                try:
                    msg = progress_queue.get_nowait()
                    await websocket.send_json(msg)
                except Exception:
                    break

            # Build final results
            targets = []
            for target_state, rec in result.targets:
                targets.append({
                    "state": target_state.model_dump(mode="json"),
                    "recommendation": rec.model_dump(mode="json"),
                })

            await websocket.send_json({
                "type": "complete",
                "source_village": result.source_village_name,
                "source_coords": f"({result.source_x}, {result.source_y})",
                "total_targets": len(targets),
                "targets": targets,
                "diagnostics": {
                    "total_reports_listed": getattr(result, "total_reports_listed", None),
                    "reports_skipped_type": getattr(result, "reports_skipped_type", None),
                    "reports_fetched_ok": getattr(result, "reports_fetched_ok", None),
                    "reports_fetched_fail": getattr(result, "reports_fetched_fail", None),
                    "pages_fetched": getattr(result, "pages_fetched", None),
                    "pages_failed": getattr(result, "pages_failed", None),
                    "analysis_duration_seconds": round(getattr(result, "analysis_duration_seconds", 0), 1),
                    "warnings": getattr(result, "warnings", []),
                },
            })

        except Exception as exc:
            progress_task.cancel()
            logger.exception("Analysis failed in WS handler")
            await websocket.send_json({"type": "error", "message": f"Analysis failed: {exc}"})

        finally:
            analyzer.on_progress(None)

    except WebSocketDisconnect:
        logger.info("Analyzer WS disconnected: user=%s", user_id)
    except Exception as exc:
        logger.exception("Unexpected error in analyzer WS: user=%s", user_id)
        try:
            await websocket.send_json({"type": "error", "message": f"Unexpected error: {exc}"})
        except Exception:
            pass
    finally:
        await ws_manager.disconnect(user_id, channel, websocket)
