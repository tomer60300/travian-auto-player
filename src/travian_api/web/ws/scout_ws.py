"""WebSocket handler for auto-scout -- scan map and send scouts with live progress."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.web.sessions import session_manager, TravianSession
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "scout_auto"


async def _send(ws: WebSocket, data: dict) -> bool:
    """Send JSON to the WebSocket. Returns False if the connection is gone."""
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


@router.websocket("/ws/scout/auto")
async def auto_scout_ws(websocket: WebSocket):
    """WebSocket endpoint for auto-scout with live progress streaming.

    Protocol:
      1. Client connects with ?token=<JWT>
      2. Client sends config JSON:
         {radius, amount, type?, delay?, exclude_coords?, village_id?}
      3. Server streams progress messages (see message types below)
      4. Server sends "complete" or "error" and keeps connection open for
         another run (client can send a new config).

    Message types sent by server:
      - {"type": "scanning", "message": "..."}
      - {"type": "scan_complete", "targets": N}
      - {"type": "scouting", "target": {x, y, name}, "index": N, "total": M}
      - {"type": "scout_result", "target": {x, y}, "success": bool, "travel_time": str|null}
      - {"type": "complete", "total_sent": N, "successful": M}
      - {"type": "error", "message": "..."}
    """
    # ── Authenticate ────────────────────────────────────────────────
    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return  # socket already closed by authenticate()

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    await ws_manager.connect(websocket, user_id, CHANNEL)

    try:
        # Keep connection open for multiple runs
        while True:
            # ── Wait for config from client ──────────────────────────
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                break

            try:
                import json
                config = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await _send(websocket, {"type": "error", "message": "Invalid JSON config"})
                continue

            # ── Parse config ─────────────────────────────────────────
            radius = config.get("radius")
            if not radius or not isinstance(radius, int) or radius < 1:
                await _send(websocket, {"type": "error", "message": "Missing or invalid 'radius' (positive integer required)"})
                continue

            amount = config.get("amount", 1)
            scout_type = config.get("type", "resources")
            delay = config.get("delay", 1.0)
            village_id = config.get("village_id") or session.active_village_id
            exclude_raw = config.get("exclude_coords") or []

            # Parse exclude_coords: list of [x, y] or "x,y" strings
            exclude_coords: set[tuple[int, int]] = set()
            for item in exclude_raw:
                try:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        exclude_coords.add((int(item[0]), int(item[1])))
                    elif isinstance(item, str):
                        parts = item.replace("|", ",").split(",")
                        if len(parts) == 2:
                            exclude_coords.add((int(parts[0].strip()), int(parts[1].strip())))
                except (ValueError, TypeError):
                    pass

            # ── Resolve center village ───────────────────────────────
            center_village = next(
                (v for v in session.auth_state.villages if v.id == village_id), None
            )
            if not center_village:
                await _send(websocket, {"type": "error", "message": f"Village {village_id} not found"})
                continue

            cx, cy = center_village.x, center_village.y
            svc = session.scout_service

            # ── Run auto-scout ───────────────────────────────────────
            try:
                # Phase 1: Scanning
                if not await _send(websocket, {"type": "scanning", "message": f"Scanning map around ({cx},{cy}) r={radius}..."}):
                    break

                tiles = await svc.scan_map(cx, cy, radius)

                # Filter out own villages
                own_ids = {v.id for v in session.auth_state.villages}
                tiles = [t for t in tiles if t.village_id not in own_ids]
                tiles = [t for t in tiles if not t.is_oasis]
                tiles = [t for t in tiles if t.village_id > 0]

                # Enrich tiles with population data
                if tiles:
                    if not await _send(websocket, {"type": "scanning", "message": f"Enriching {len(tiles)} tiles with details..."}):
                        break
                    tiles = await svc.enrich_tiles(tiles)

                # Apply filters
                tiles = svc.filter_targets(
                    tiles,
                    exclude_coords=exclude_coords,
                    exclude_oases=True,
                )

                if not tiles:
                    await _send(websocket, {"type": "scan_complete", "targets": 0})
                    await _send(websocket, {"type": "complete", "total_sent": 0, "successful": 0})
                    continue

                if not await _send(websocket, {"type": "scan_complete", "targets": len(tiles)}):
                    break

                # Phase 2: Send scouts one by one with status callback
                total = len(tiles)
                results: list[dict] = []

                for i, target in enumerate(tiles):
                    # Notify client about the current target
                    if not await _send(websocket, {
                        "type": "scouting",
                        "target": {"x": target.x, "y": target.y, "name": target.village_name or "?"},
                        "index": i + 1,
                        "total": total,
                    }):
                        break

                    try:
                        result = await session.military_service.send_scouts(
                            x=target.x,
                            y=target.y,
                            amount=amount,
                            scout_type=scout_type,
                            village_id=village_id,
                        )

                        # Retry once on transient failure
                        if not result.success and "No confirmation form" in (result.raw_response or ""):
                            await asyncio.sleep(3)
                            result = await session.military_service.send_scouts(
                                x=target.x,
                                y=target.y,
                                amount=amount,
                                scout_type=scout_type,
                                village_id=village_id,
                            )

                        entry = {
                            "x": target.x,
                            "y": target.y,
                            "name": target.village_name,
                            "success": result.success,
                            "travel_time": result.travel_time,
                        }
                        results.append(entry)

                        if not await _send(websocket, {
                            "type": "scout_result",
                            "target": {"x": target.x, "y": target.y},
                            "success": result.success,
                            "travel_time": result.travel_time,
                        }):
                            break

                    except Exception as e:
                        logger.warning("Scout send error for (%s,%s): %s", target.x, target.y, e)
                        results.append({
                            "x": target.x,
                            "y": target.y,
                            "name": target.village_name,
                            "success": False,
                            "travel_time": None,
                        })
                        if not await _send(websocket, {
                            "type": "scout_result",
                            "target": {"x": target.x, "y": target.y},
                            "success": False,
                            "travel_time": None,
                        }):
                            break

                    # Delay between sends (except after the last one)
                    if i < total - 1:
                        from travian_api.stealth.timing import HumanTiming
                        await asyncio.sleep(HumanTiming.delay(delay))

                # Phase 3: Summary
                total_sent = len(results)
                successful = sum(1 for r in results if r["success"])
                await _send(websocket, {
                    "type": "complete",
                    "total_sent": total_sent,
                    "successful": successful,
                })

            except WebSocketDisconnect:
                break
            except Exception as exc:
                logger.exception("Auto-scout error for user %s", user_id)
                await _send(websocket, {"type": "error", "message": str(exc)})

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unexpected error in scout WS for user %s", user_id)
    finally:
        await ws_manager.disconnect(user_id, CHANNEL)
