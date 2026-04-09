"""WebSocket handler for auto-scout -- scan map and send scouts with live progress.

Optimized flow:
- Rally point navigation happens ONCE (not per target)
- During stealth delays, countdown messages are sent so the user sees progress
- ETA is computed after the first target and updated each cycle
"""

from __future__ import annotations

import asyncio
import logging
import time
import re
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.web.sessions import session_manager, TravianSession
from travian_api.web.ws.manager import ws_manager
from travian_api.web.routes.military import _resolve_scout_unit
from travian_api.stealth.timing import HumanTiming
from travian_api.stealth.human_delay import ActionType
from travian_api.parsers.html_parser import parse_troop_confirm_page

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "scout_auto"


async def _send(ws: WebSocket, data: dict) -> bool:
    try:
        await ws.send_json(data)
        return True
    except Exception:
        return False


async def _stealth_delay_with_countdown(ws: WebSocket, seconds: float, label: str) -> bool:
    """Sleep for `seconds` while streaming countdown messages every second."""
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, 1.0)
        if not await _send(ws, {
            "type": "waiting",
            "message": f"{label} ({remaining:.0f}s remaining)",
            "remaining": round(remaining, 1),
        }):
            return False
        await asyncio.sleep(chunk)
        remaining -= chunk
    return True


async def _send_scout_fast(
    session: TravianSession,
    x: int, y: int,
    amount: int,
    scout_type: str,
    village_id: int,
    is_first: bool,
) -> dict:
    """Send scouts to a single target with optimized stealth.

    When is_first=True, navigates to rally point. Otherwise skips navigation
    (we're already on the rally point page from the previous confirm).
    """
    http = session.http_client
    delay = http.human_delay
    scout_unit = _resolve_scout_unit(session.tribe_id)
    troops = {scout_unit: amount}
    scout_target_value = "1" if scout_type == "resources" else "2"

    if village_id:
        rally_url = f"/build.php?newdid={village_id}&gid=16&tt=2"
    else:
        rally_url = "/build.php?gid=16&tt=2"

    # Step 0: Navigate (only on first target)
    if is_first:
        await http.navigator.navigate_to_rally_point(village_id)

    # Step 1: Submit troop form (minimal delay — we just "filled" the form)
    await delay.wait(ActionType.RAPID, "selecting troops")

    form_data = {}
    for i in range(1, 11):
        form_data[f'troop[t{i}]'] = str(troops.get(f't{i}', 0))
    form_data['villagename'] = ''
    form_data['x'] = str(x)
    form_data['y'] = str(y)
    form_data['eventType'] = '4'  # raid
    form_data['ok'] = 'ok'

    confirm_html = await http.post_form(rally_url, form_data)

    has_confirm = 'troopSendForm' in confirm_html or 'confirmSendTroops' in confirm_html
    if not has_confirm:
        error_msg = _extract_error(confirm_html)
        return {"success": False, "error": error_msg or "No confirmation form", "travel_time": None}

    # Step 2: Parse and confirm (short delay — "reading" the confirmation)
    await delay.wait(ActionType.RAPID, "reading confirmation")

    confirm_fields = parse_troop_confirm_page(confirm_html)
    checksum = confirm_fields.pop('checksum', '')
    if not checksum:
        cs_match = re.search(r"input\[name=checksum\]'\)\.value\s*=\s*'([a-f0-9]+)'", confirm_html)
        if cs_match:
            checksum = cs_match.group(1)

    if not checksum:
        return {"success": False, "error": "No checksum in confirmation", "travel_time": None}

    final_data = dict(confirm_fields)
    final_data['checksum'] = checksum
    if scout_target_value:
        final_data['troops[0][scoutTarget]'] = scout_target_value

    await delay.wait(ActionType.CLICK, "clicking send")

    result_html = await http.post_form(rally_url, final_data)

    # Detect success
    action_token = final_data.get('action', '')
    form_reappeared = action_token and f'value="{action_token}"' in result_html
    has_error = bool(re.search(r'class="error[^"]*"', result_html))
    has_movement = 'troopMovement' in result_html
    success = (not form_reappeared and not has_error) or has_movement

    # Extract travel time
    travel_time = None
    time_match = re.search(r'class="in"[^>]*>.*?(\d+:\d+:\d+)', confirm_html, re.DOTALL)
    if time_match:
        travel_time = time_match.group(1)

    # Update navigator state — we're now on the rally point page
    if hasattr(http.navigator, '_current_page'):
        http.navigator._current_page = rally_url

    if not success:
        error_msg = _extract_error(result_html)
        return {"success": False, "error": error_msg or "Send failed", "travel_time": travel_time}

    return {"success": True, "error": None, "travel_time": travel_time}


def _extract_error(html: str) -> str:
    """Extract error message from Travian HTML response."""
    m = re.search(r'class="error[^"]*"[^>]*>(.*?)</[^>]+>', html, re.DOTALL)
    if m:
        return re.sub(r'<[^>]+>', '', m.group(1)).strip()
    if 'No troops' in html or 'no troops' in html.lower():
        return 'No troops available'
    return ''


@router.websocket("/ws/scout/auto")
async def auto_scout_ws(websocket: WebSocket):
    """Auto-scout WS with optimized stealth: navigate once, countdown during delays, ETA display."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    await ws_manager.connect(websocket, user_id, CHANNEL)

    try:
        while True:
            # Wait for config
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

            radius = config.get("radius")
            if not radius or not isinstance(radius, int) or radius < 1:
                await _send(websocket, {"type": "error", "message": "Invalid radius"})
                continue

            amount = config.get("amount", 1)
            scout_type = config.get("type", "resources")
            user_delay = config.get("delay", 3.0)
            village_id = config.get("village_id") or session.active_village_id
            exclude_raw = config.get("exclude_coords") or []

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

            center_village = next(
                (v for v in session.auth_state.villages if v.id == village_id), None
            )
            if not center_village:
                await _send(websocket, {"type": "error", "message": f"Village {village_id} not found"})
                continue

            cx, cy = center_village.x, center_village.y
            svc = session.scout_service

            try:
                # ── Phase 1: Scan ────────────────────────────────────
                if not await _send(websocket, {"type": "scanning", "message": f"Scanning ({cx},{cy}) r={radius}..."}):
                    break

                tiles = await svc.scan_map(cx, cy, radius)
                own_ids = {v.id for v in session.auth_state.villages}
                tiles = [t for t in tiles if t.village_id > 0 and t.village_id not in own_ids and not t.is_oasis]

                if tiles:
                    if not await _send(websocket, {"type": "scanning", "message": f"Enriching {len(tiles)} tiles..."}):
                        break
                    tiles = await svc.enrich_tiles(tiles)

                tiles = svc.filter_targets(tiles, exclude_coords=exclude_coords, exclude_oases=True)

                if not tiles:
                    await _send(websocket, {"type": "scan_complete", "targets": 0})
                    await _send(websocket, {"type": "complete", "total_sent": 0, "successful": 0})
                    continue

                total = len(tiles)
                if not await _send(websocket, {"type": "scan_complete", "targets": total}):
                    break

                # Send pre-computed target list so frontend can show them all immediately
                await _send(websocket, {
                    "type": "target_list",
                    "targets": [{"x": t.x, "y": t.y, "name": t.village_name, "pop": t.population, "dist": round(t.distance, 1)} for t in tiles],
                })

                # ── Phase 2: Send scouts ─────────────────────────────
                results = []
                times_per_target = []
                t_start_total = time.monotonic()

                for i, target in enumerate(tiles):
                    t_start = time.monotonic()

                    # ETA calculation
                    eta_str = ""
                    if times_per_target:
                        avg = sum(times_per_target) / len(times_per_target)
                        remaining_targets = total - i
                        eta_secs = avg * remaining_targets
                        eta_min = int(eta_secs // 60)
                        eta_sec = int(eta_secs % 60)
                        eta_str = f" | ETA: {eta_min}m{eta_sec:02d}s"

                    if not await _send(websocket, {
                        "type": "scouting",
                        "target": {"x": target.x, "y": target.y, "name": target.village_name or "?"},
                        "index": i + 1,
                        "total": total,
                        "eta": eta_str.strip(" |") if eta_str else None,
                    }):
                        break

                    try:
                        result = await _send_scout_fast(
                            session, target.x, target.y, amount, scout_type, village_id,
                            is_first=(i == 0),
                        )
                        results.append(result)

                        if not await _send(websocket, {
                            "type": "scout_result",
                            "target": {"x": target.x, "y": target.y},
                            "success": result["success"],
                            "error": result.get("error"),
                            "travel_time": result.get("travel_time"),
                            "index": i + 1,
                            "total": total,
                        }):
                            break

                    except Exception as e:
                        logger.warning("Scout error for (%s,%s): %s", target.x, target.y, e)
                        results.append({"success": False, "error": str(e), "travel_time": None})
                        if not await _send(websocket, {
                            "type": "scout_result",
                            "target": {"x": target.x, "y": target.y},
                            "success": False,
                            "error": str(e),
                            "travel_time": None,
                            "index": i + 1,
                            "total": total,
                        }):
                            break

                    t_elapsed = time.monotonic() - t_start
                    times_per_target.append(t_elapsed)

                    # Inter-target stealth delay with countdown (skip after last)
                    if i < total - 1:
                        stealth_secs = HumanTiming.delay(user_delay)
                        if not await _stealth_delay_with_countdown(
                            websocket, stealth_secs,
                            f"Stealth cooldown before target {i + 2}/{total}"
                        ):
                            break

                # ── Phase 3: Summary ─────────────────────────────────
                total_sent = len(results)
                successful = sum(1 for r in results if r.get("success"))
                total_time = time.monotonic() - t_start_total
                avg_time = total_time / total_sent if total_sent else 0
                await _send(websocket, {
                    "type": "complete",
                    "total_sent": total_sent,
                    "successful": successful,
                    "total_time_seconds": round(total_time, 1),
                    "avg_time_per_target": round(avg_time, 1),
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
