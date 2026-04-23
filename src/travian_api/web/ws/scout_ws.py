"""WebSocket handler for auto-scout -- scan map and send scouts with live progress.

Optimized flow:
- Rally point navigation happens ONCE (not per target)
- During stealth delays, countdown messages are sent so the user sees progress
- ETA is computed after the first target and updated each cycle
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.exceptions import ActivityBudgetExhausted
from travian_api.parsers.html_parser import parse_troop_confirm_page
from travian_api.stealth.human_delay import ActionType
from travian_api.stealth.timing import HumanTiming
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.operation_gate import operation_gate
from travian_api.web.routes.military import _resolve_scout_unit
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws.manager import ws_manager

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
        if not await _send(
            ws,
            {
                "type": "waiting",
                "message": f"{label} ({remaining:.0f}s remaining)",
                "remaining": round(remaining, 1),
            },
        ):
            return False
        await asyncio.sleep(chunk)
        remaining -= chunk
    return True


async def _send_scout_fast(
    session: TravianSession,
    x: int,
    y: int,
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
        form_data[f"troop[t{i}]"] = str(troops.get(f"t{i}", 0))
    form_data["villagename"] = ""
    form_data["x"] = str(x)
    form_data["y"] = str(y)
    form_data["eventType"] = "4"  # raid
    form_data["ok"] = "ok"

    confirm_html = await http.post_form(rally_url, form_data, safe_to_retry=False)

    has_confirm = "troopSendForm" in confirm_html or "confirmSendTroops" in confirm_html
    if not has_confirm:
        error_msg = _extract_error(confirm_html)
        return {"success": False, "error": error_msg or "No confirmation form", "travel_time": None}

    # Step 2: Parse and confirm (short delay — "reading" the confirmation)
    await delay.wait(ActionType.RAPID, "reading confirmation")

    confirm_fields = parse_troop_confirm_page(confirm_html)
    checksum = confirm_fields.pop("checksum", "")
    if not checksum:
        cs_match = re.search(r"input\[name=checksum\]'\)\.value\s*=\s*'([a-f0-9]+)'", confirm_html)
        if cs_match:
            checksum = cs_match.group(1)

    if not checksum:
        return {"success": False, "error": "No checksum in confirmation", "travel_time": None}

    final_data = dict(confirm_fields)
    final_data["checksum"] = checksum
    if scout_target_value:
        final_data["troops[0][scoutTarget]"] = scout_target_value

    await delay.wait(ActionType.CLICK, "clicking send")

    result_html = await http.post_form(rally_url, final_data, safe_to_retry=False)

    # Detect success
    action_token = final_data.get("action", "")
    form_reappeared = action_token and f'value="{action_token}"' in result_html
    has_error = bool(re.search(r'class="error[^"]*"', result_html))
    has_movement = "troopMovement" in result_html
    success = (not form_reappeared and not has_error) or has_movement

    # Extract travel time
    travel_time = None
    time_match = re.search(r'class="in"[^>]*>.*?(\d+:\d+:\d+)', confirm_html, re.DOTALL)
    if time_match:
        travel_time = time_match.group(1)

    # Update navigator state — we're now on the rally point page
    if hasattr(http.navigator, "_current_page"):
        http.navigator._current_page = rally_url

    if not success:
        error_msg = _extract_error(result_html)
        return {"success": False, "error": error_msg or "Send failed", "travel_time": travel_time}

    return {"success": True, "error": None, "travel_time": travel_time}


def _extract_error(html: str) -> str:
    """Extract error message from Travian HTML response."""
    m = re.search(r'class="error[^"]*"[^>]*>(.*?)</[^>]+>', html, re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    if "No troops" in html or "no troops" in html.lower():
        return "No troops available"
    return ""


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

    op_type = "scout"

    if not operation_gate.acquire(user_id, op_type):
        await websocket.close(code=4009, reason="A scout operation is already running")
        return

    try:
        op_started_at = time.monotonic()

        await ws_manager.connect(websocket, user_id, CHANNEL)

        exec_session = exec_session_manager.create(user_id, "scout-auto", "Auto Scout")

        async def _tracked_send(ws, data):
            ok = await _send(ws, data)
            if ok:
                exec_session_manager.push(exec_session.id, data)
            return ok

        await _tracked_send(websocket, {"type": "session_init", "session_id": exec_session.id})

        while True:
            # Wait for config
            try:
                raw = await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                break

            try:
                import json

                config = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                await _tracked_send(websocket, {"type": "error", "message": "Invalid JSON config"})
                continue

            radius = config.get("radius")
            if not radius or not isinstance(radius, int) or radius < 1:
                await _tracked_send(websocket, {"type": "error", "message": "Invalid radius"})
                continue

            amount = config.get("amount", 1)
            scout_type = config.get("type", "resources")
            delay_min = config.get("delay_min", config.get("delay", 2.0))
            delay_max = config.get("delay_max", delay_min + 2.0)
            start_index = config.get("start_index", 0)
            village_id = config.get("village_id") or session.active_village_id

            # "targets": enriched target list from scan UI [{x, y, name, pop, player}, ...]
            # When provided, skip re-scan — scout exactly these targets.
            targets_from_ui = config.get("targets")

            center_village = next(
                (v for v in session.auth_state.villages if v.id == village_id), None
            )
            if not center_village:
                await _tracked_send(
                    websocket, {"type": "error", "message": f"Village {village_id} not found"}
                )
                continue

            cx, cy = center_village.x, center_village.y
            svc = session.scout_service

            # Build equivalent CLI command for display
            cli_parts = [f"travian scout auto --radius {radius} --village-id {village_id}"]
            cli_parts.append(f"--amount {amount}")
            cli_parts.append(f"--type {scout_type}")
            cli_parts.append(f"--delay {delay_min}")
            if start_index:
                cli_parts.append(f"--start-index {start_index}")
            if targets_from_ui:
                cli_parts.append(f"--targets {len(targets_from_ui)}")
            await _tracked_send(
                websocket,
                {
                    "type": "trigger_info",
                    "command": " ".join(cli_parts),
                },
            )

            try:
                # ── Phase 1: Resolve targets ─────────────────────────
                if targets_from_ui is not None:
                    # Pre-filtered targets from scan UI — no re-scan
                    import math

                    from travian_api.models.farm_list import MapTileInfo

                    if not targets_from_ui:
                        await _tracked_send(
                            websocket,
                            {
                                "type": "complete",
                                "total_sent": 0,
                                "successful": 0,
                                "next_start_index": 0,
                            },
                        )
                        continue

                    tiles = []
                    for item in targets_from_ui:
                        try:
                            tx = int(item.get("x") if isinstance(item, dict) else item[0])
                            ty = int(item.get("y") if isinstance(item, dict) else item[1])
                            name = item.get("name", "") if isinstance(item, dict) else ""
                            pop = item.get("pop", 0) if isinstance(item, dict) else 0
                            player = item.get("player", "") if isinstance(item, dict) else ""
                            dist = math.sqrt((tx - cx) ** 2 + (ty - cy) ** 2)
                            t = MapTileInfo(x=tx, y=ty, distance=round(dist, 2))
                            t.village_name = name
                            t.population = pop
                            t.player_name = player
                            tiles.append(t)
                        except (ValueError, TypeError, KeyError):
                            pass

                    if not tiles:
                        await _tracked_send(
                            websocket,
                            {
                                "type": "complete",
                                "total_sent": 0,
                                "successful": 0,
                                "next_start_index": 0,
                            },
                        )
                        continue

                    if not await _tracked_send(
                        websocket, {"type": "scan_complete", "targets": len(tiles)}
                    ):
                        break

                    await _tracked_send(
                        websocket,
                        {
                            "type": "target_list",
                            "targets": [
                                {
                                    "x": t.x,
                                    "y": t.y,
                                    "name": t.village_name,
                                    "pop": t.population,
                                    "dist": t.distance,
                                }
                                for t in tiles
                            ],
                        },
                    )
                else:
                    # Legacy fallback: no targets provided, do a full scan
                    if not await _tracked_send(
                        websocket,
                        {"type": "scanning", "message": f"Scanning ({cx},{cy}) r={radius}..."},
                    ):
                        break

                    tiles = await svc.scan_map(cx, cy, radius)
                    own_ids = {v.id for v in session.auth_state.villages}
                    tiles = [
                        t
                        for t in tiles
                        if t.village_id > 0 and t.village_id not in own_ids and not t.is_oasis
                    ]

                    if tiles:
                        if not await _tracked_send(
                            websocket,
                            {"type": "scanning", "message": f"Enriching {len(tiles)} tiles..."},
                        ):
                            break
                        tiles = await svc.enrich_tiles(tiles)

                    tiles = svc.filter_targets(tiles, exclude_oases=True)

                if not tiles:
                    await _tracked_send(websocket, {"type": "scan_complete", "targets": 0})
                    await _tracked_send(
                        websocket,
                        {
                            "type": "complete",
                            "total_sent": 0,
                            "successful": 0,
                            "next_start_index": 0,
                        },
                    )
                    continue

                total = len(tiles)
                if not await _tracked_send(websocket, {"type": "scan_complete", "targets": total}):
                    break

                # Send pre-computed target list so frontend can show them all immediately
                await _tracked_send(
                    websocket,
                    {
                        "type": "target_list",
                        "targets": [
                            {
                                "x": t.x,
                                "y": t.y,
                                "name": t.village_name,
                                "pop": t.population,
                                "dist": round(t.distance, 1),
                            }
                            for t in tiles
                        ],
                    },
                )

                # ── Player population breakdown (debug aid) ──────────
                player_villages: dict[str, list[tuple[str, int, int, int]]] = {}
                for t in tiles:
                    pname = t.player_name or ""
                    if pname:
                        vname = t.village_name or f"({t.x},{t.y})"
                        player_villages.setdefault(pname, []).append(
                            (vname, t.population, t.x, t.y)
                        )

                if player_villages:
                    await _tracked_send(
                        websocket,
                        {
                            "type": "player_pops",
                            "players": [
                                {
                                    "name": pname,
                                    "total": sum(vp for _, vp, *_ in vils),
                                    "villages": [
                                        {"name": vn, "pop": vp, "x": vx, "y": vy}
                                        for vn, vp, vx, vy in vils
                                    ],
                                }
                                for pname, vils in sorted(
                                    player_villages.items(),
                                    key=lambda kv: -sum(vp for _, vp, *_ in kv[1]),
                                )
                            ],
                        },
                    )

                original_total = len(tiles)

                # Rotate targets for round-robin resume
                if start_index > 0 and start_index < len(tiles):
                    tiles = tiles[start_index:] + tiles[:start_index]

                # ── Pre-flight: check available scouts ──────────────
                scout_unit = _resolve_scout_unit(session.tribe_id)
                try:
                    preflight_troops = await session.military_service.get_available_troops(
                        village_id
                    )
                    available_scouts = preflight_troops.get(scout_unit, 0)
                except Exception:
                    available_scouts = -1  # unknown, proceed anyway

                scouts_per_target = amount * (2 if scout_type == "both" else 1)

                if available_scouts >= 0:
                    max_targets = (
                        available_scouts // scouts_per_target if scouts_per_target > 0 else 0
                    )
                    await _tracked_send(
                        websocket,
                        {
                            "type": "scout_preflight",
                            "available": available_scouts,
                            "needed_per_target": scouts_per_target,
                            "can_send_to": max_targets,
                            "total_targets": original_total,
                        },
                    )

                    if max_targets == 0:
                        await _tracked_send(
                            websocket,
                            {
                                "type": "scouts_exhausted",
                                "available": 0,
                                "message": f"No scouts available (0 {scout_unit} idle)",
                                "sent_so_far": 0,
                                "successful": 0,
                            },
                        )
                        await _tracked_send(
                            websocket,
                            {
                                "type": "complete",
                                "total_sent": 0,
                                "successful": 0,
                                "next_start_index": start_index,
                            },
                        )
                        continue
                    if max_targets < len(tiles):
                        tiles = tiles[:max_targets]
                        await _tracked_send(
                            websocket,
                            {
                                "type": "scouts_capped",
                                "available": available_scouts,
                                "can_send_to": max_targets,
                                "total_targets": original_total,
                                "message": f"Only {available_scouts} scouts idle — capped to {max_targets} targets",
                            },
                        )

                total = len(tiles)

                # ── Phase 2: Send scouts (stealth mode) ─────────────
                # Check if captcha was just resolved
                if operation_gate.check_should_stop(user_id, op_started_at):
                    await _tracked_send(
                        websocket,
                        {
                            "type": "error",
                            "message": "Stopped after captcha resolution — restart manually",
                        },
                    )
                    await _tracked_send(
                        websocket,
                        {
                            "type": "complete",
                            "total_sent": 0,
                            "successful": 0,
                            "next_start_index": start_index,
                        },
                    )
                    continue

                # Activity budget check before starting the send loop
                try:
                    session.http_client.check_activity_budget()
                except ActivityBudgetExhausted as exc:
                    await _tracked_send(websocket, {"type": "error", "message": str(exc)})
                    await _tracked_send(
                        websocket,
                        {
                            "type": "complete",
                            "total_sent": 0,
                            "successful": 0,
                            "next_start_index": start_index,
                        },
                    )
                    continue

                results = []
                times_per_target = []
                t_start_total = time.monotonic()
                consecutive_troop_failures = 0
                force_navigate = False
                renav_interval = random.randint(8, 15)
                mean_delay = (delay_min + delay_max) / 2
                delay_obj = session.http_client.human_delay

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

                    if not await _tracked_send(
                        websocket,
                        {
                            "type": "scouting",
                            "target": {
                                "x": target.x,
                                "y": target.y,
                                "name": target.village_name or "?",
                            },
                            "index": i + 1,
                            "total": total,
                            "eta": eta_str.strip(" |") if eta_str else None,
                        },
                    ):
                        break

                    # ── Stealth: noise injection (15% chance) ───
                    noise = getattr(session.http_client, "noise_injector", None)
                    if noise and hasattr(noise, "maybe_inject_noise") and i > 0:
                        try:
                            injected = await noise.maybe_inject_noise()
                            if injected:
                                await _tracked_send(
                                    websocket,
                                    {
                                        "type": "noise_action",
                                        "message": "Idle browsing (stealth)...",
                                    },
                                )
                                force_navigate = True
                        except Exception:
                            pass

                    # ── Stealth: periodic re-navigation ─────────
                    if i > 0 and i % renav_interval == 0:
                        renav_interval = random.randint(8, 15)
                        try:
                            await delay_obj.wait(ActionType.PAGE_LOAD, "browsing")
                            nav = getattr(session.http_client, "navigator", None)
                            if nav and hasattr(nav, "idle_browse"):
                                await nav.idle_browse()
                                force_navigate = True
                                await _tracked_send(
                                    websocket,
                                    {
                                        "type": "re_navigate",
                                        "message": "Breaking request pattern (stealth)...",
                                    },
                                )
                        except Exception:
                            pass

                    # Determine if we need to re-navigate to rally point
                    use_is_first = (i == 0) or force_navigate
                    if force_navigate:
                        force_navigate = False

                    try:
                        if scout_type == "both":
                            # Send resources scout
                            result_res = await _send_scout_fast(
                                session,
                                target.x,
                                target.y,
                                amount,
                                "resources",
                                village_id,
                                is_first=use_is_first,
                            )
                            # Realistic decision delay (switching scout type)
                            await delay_obj.wait(ActionType.DECISION, "switching scout type")
                            # Send defenses scout
                            result_def = await _send_scout_fast(
                                session,
                                target.x,
                                target.y,
                                amount,
                                "defenses",
                                village_id,
                                is_first=False,
                            )
                            # Combine results
                            both_success = result_res["success"] and result_def["success"]
                            errors = []
                            if not result_res["success"]:
                                errors.append(f"resources: {result_res.get('error', '?')}")
                            if not result_def["success"]:
                                errors.append(f"defenses: {result_def.get('error', '?')}")
                            result = {
                                "success": both_success,
                                "error": "; ".join(errors) if errors else None,
                                "travel_time": result_res.get("travel_time"),
                            }
                            results.append(result)
                        else:
                            result = await _send_scout_fast(
                                session,
                                target.x,
                                target.y,
                                amount,
                                scout_type,
                                village_id,
                                is_first=use_is_first,
                            )
                            results.append(result)

                        if not await _tracked_send(
                            websocket,
                            {
                                "type": "scout_result",
                                "target": {"x": target.x, "y": target.y},
                                "success": result["success"],
                                "error": result.get("error"),
                                "travel_time": result.get("travel_time"),
                                "index": i + 1,
                                "total": total,
                            },
                        ):
                            break

                    except Exception as e:
                        logger.warning("Scout error for (%s,%s): %s", target.x, target.y, e)
                        result = {"success": False, "error": str(e), "travel_time": None}
                        results.append(result)
                        if not await _tracked_send(
                            websocket,
                            {
                                "type": "scout_result",
                                "target": {"x": target.x, "y": target.y},
                                "success": False,
                                "error": str(e),
                                "travel_time": None,
                                "index": i + 1,
                                "total": total,
                            },
                        ):
                            break

                    t_elapsed = time.monotonic() - t_start
                    times_per_target.append(t_elapsed)

                    # ── Mid-loop: charge activity budget ──────
                    session.http_client.activity_scheduler.log_activity(t_elapsed)
                    if (i + 1) % 5 == 0:
                        try:
                            session.http_client.check_activity_budget()
                        except ActivityBudgetExhausted as exc:
                            await _tracked_send(websocket, {"type": "error", "message": str(exc)})
                            break

                    # ── Mid-loop: detect troop exhaustion ───────
                    if (
                        not result.get("success")
                        and "troops" in (result.get("error") or "").lower()
                    ):
                        consecutive_troop_failures += 1
                    else:
                        consecutive_troop_failures = 0

                    if consecutive_troop_failures >= 2:
                        await _tracked_send(
                            websocket,
                            {
                                "type": "scouts_exhausted",
                                "sent_so_far": i + 1,
                                "successful": sum(1 for r in results if r.get("success")),
                                "message": "Scouts ran out — stopping batch",
                            },
                        )
                        break

                    # ── Inter-target delay: heavy-tailed + fatigue ─
                    if i < total - 1:
                        fatigue_factor = 1.0 + (i / max(total, 1)) * 0.3
                        stealth_secs = HumanTiming.delay(mean_delay) * fatigue_factor
                        stealth_secs = max(1.0, min(stealth_secs, mean_delay * 15))
                        if not await _stealth_delay_with_countdown(
                            websocket,
                            stealth_secs,
                            f"Stealth cooldown before target {i + 2}/{total}",
                        ):
                            break

                # ── Phase 3: Summary ─────────────────────────────────
                total_sent = len(results)
                successful = sum(1 for r in results if r.get("success"))
                total_time = time.monotonic() - t_start_total
                avg_time = total_time / total_sent if total_sent else 0
                await _tracked_send(
                    websocket,
                    {
                        "type": "complete",
                        "total_sent": total_sent,
                        "successful": successful,
                        "total_time_seconds": round(total_time, 1),
                        "avg_time_per_target": round(avg_time, 1),
                        "next_start_index": (start_index + total_sent) % original_total
                        if original_total > 0
                        else 0,
                    },
                )

            except (WebSocketDisconnect, RuntimeError):
                break
            except Exception as exc:
                logger.exception("Auto-scout error for user %s", user_id)
                await _tracked_send(websocket, {"type": "error", "message": str(exc)})

    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        logger.exception("Unexpected error in scout WS for user %s", user_id)
    finally:
        operation_gate.release(user_id, op_type)
        exec_session_manager.mark_disconnected(exec_session.id)
        await ws_manager.disconnect(user_id, CHANNEL, websocket)


# ---------------------------------------------------------------------------
# Shared helper: batch-query real player populations via GraphQL
# ---------------------------------------------------------------------------


def _sum_visible_player_pops(tiles: list) -> dict[int, int]:
    """Sum village populations per player from the enriched tiles in the scan.

    This is the best approximation available — the GQL ``player.population``
    field returns 0 for other players (only works for your own account).
    """
    pops: dict[int, int] = {}
    for t in tiles:
        if t.player_id:
            pops[t.player_id] = pops.get(t.player_id, 0) + t.population
    return pops


# ---------------------------------------------------------------------------
# Map Scan WebSocket — streams detailed progress for the scan phase
# ---------------------------------------------------------------------------

SCAN_CHANNEL = "scout_scan"


@router.websocket("/ws/scout/scan")
async def scout_scan_ws(websocket: WebSocket):
    """Map scan with streaming progress: map regions, enrichment per-tile, filtering stats, ETA."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    scan_op_type = "scout-scan"

    if not operation_gate.acquire(user_id, scan_op_type):
        await websocket.close(code=4009, reason="A map scan operation is already running")
        return

    try:
        await ws_manager.connect(websocket, user_id, SCAN_CHANNEL)

        exec_session = exec_session_manager.create(user_id, "scout-scan", "Map Scan")

        async def _tracked_send(ws, data):
            ok = await _send(ws, data)
            if ok:
                exec_session_manager.push(exec_session.id, data)
            return ok

        await _tracked_send(websocket, {"type": "session_init", "session_id": exec_session.id})

        # Wait for config
        try:
            raw = await websocket.receive_text()
        except (WebSocketDisconnect, RuntimeError):
            return

        import json as _json

        try:
            config = _json.loads(raw)
        except (ValueError, TypeError):
            await _tracked_send(websocket, {"type": "error", "message": "Invalid JSON"})
            return

        radius = config.get("radius", 10)
        village_id = config.get("village_id") or session.active_village_id
        min_pop = config.get("min_pop")
        max_pop = config.get("max_pop")
        max_player_pop = config.get("max_player_pop")
        show_oases = config.get("show_oases", False)
        limit = config.get("limit", 99999)
        exclude_alliance_ids = config.get("exclude_alliance_ids", [])
        exclude_alliance_names = config.get("exclude_alliance_names", [])
        exclude_player_names = config.get("exclude_player_names", [])

        center_village = next((v for v in session.auth_state.villages if v.id == village_id), None)
        if not center_village:
            await _tracked_send(
                websocket, {"type": "error", "message": f"Village {village_id} not found"}
            )
            return

        cx, cy = center_village.x, center_village.y
        svc = session.scout_service

        # Build equivalent CLI command for display
        cli_parts = [f"travian scout scan --radius {radius} --village-id {village_id}"]
        if min_pop is not None:
            cli_parts.append(f"--min-pop {min_pop}")
        if max_pop is not None:
            cli_parts.append(f"--max-pop {max_pop}")
        if max_player_pop is not None:
            cli_parts.append(f"--max-player-pop {max_player_pop}")
        if show_oases:
            cli_parts.append("--show-oases")
        if limit != 99999:
            cli_parts.append(f"--limit {limit}")
        if exclude_alliance_names:
            cli_parts.append(f'--exclude-alliances "{",".join(exclude_alliance_names)}"')
        if exclude_player_names:
            cli_parts.append(f'--exclude-players "{",".join(exclude_player_names)}"')
        await _tracked_send(
            websocket,
            {
                "type": "trigger_info",
                "command": " ".join(cli_parts),
            },
        )

        import math

        t_total_start = time.monotonic()

        # ── Phase 1: Map scan ───────────────────────────────────────
        step = 15
        scan_centers = []
        for scx in range(cx - radius, cx + radius + 1, step * 2):
            for scy in range(cy - radius, cy + radius + 1, step * 2):
                scan_centers.append((scx, scy))

        num_regions = len(scan_centers)
        if not await _tracked_send(
            websocket,
            {
                "type": "phase",
                "phase": "map_scan",
                "message": f"Scanning {num_regions} map region(s) around ({cx},{cy}) r={radius}...",
            },
        ):
            return

        all_tile_data: dict[tuple[int, int], dict] = {}
        for idx, (scx, scy) in enumerate(scan_centers):
            if not await _tracked_send(
                websocket,
                {
                    "type": "scan_region",
                    "index": idx + 1,
                    "total": num_regions,
                    "center": {"x": scx, "y": scy},
                },
            ):
                return

            resp = await svc.http_client.post_json(
                "/api/v1/map/position",
                {"data": {"x": scx, "y": scy, "zoomLevel": 3, "ignorePositions": []}},
            )
            for t in resp.get("tiles", []):
                pos = t.get("position", {})
                x, y = pos.get("x", 0), pos.get("y", 0)
                if (x, y) not in all_tile_data:
                    all_tile_data[(x, y)] = t

        # Parse raw tiles
        from travian_api.models.farm_list import MapTileInfo

        raw_tiles: list[MapTileInfo] = []
        for (x, y), t in all_tile_data.items():
            dist = math.sqrt((x - cx) ** 2 + (y - cy) ** 2)
            if dist > radius:
                continue
            did = t.get("did")
            if did is None:
                continue
            uid = t.get("uid")
            aid = t.get("aid")
            title = t.get("title", "")
            village_name = ""
            name_match = re.search(r"\{k\.dt\}\s*(.+)", title)
            if name_match:
                village_name = name_match.group(1).strip()
            is_oasis = "{k.fo}" in title or "{k.bt}" in title

            raw_tiles.append(
                MapTileInfo(
                    x=x,
                    y=y,
                    village_id=did if did > 0 else 0,
                    player_id=uid if uid else None,
                    alliance_id=aid if aid else None,
                    village_name=village_name,
                    distance=round(dist, 2),
                    is_oasis=is_oasis,
                    is_abandoned=did == -1 and uid is None,
                )
            )

        if not await _tracked_send(
            websocket,
            {
                "type": "phase",
                "phase": "map_scan_done",
                "message": f"Map scan complete: {len(all_tile_data)} raw tiles, {len(raw_tiles)} with villages/oases",
            },
        ):
            return

        # ── Phase 2: Pre-enrichment filtering ───────────────────────
        own_ids = {v.id for v in session.auth_state.villages}
        before_count = len(raw_tiles)
        # Keep player villages (village_id > 0, not own) + oases if requested
        # BUG FIX: unoccupied oases have village_id=0, so the old filter
        # `village_id > 0` dropped them before show_oases was checked.
        tiles = [
            t
            for t in raw_tiles
            if (t.village_id > 0 and t.village_id not in own_ids) or (t.is_oasis and show_oases)
        ]
        if not show_oases:
            tiles = [t for t in tiles if not t.is_oasis]
        relevant = [t for t in tiles if t.player_id or (t.is_oasis and show_oases)]
        removed_own = before_count - len(tiles)
        removed_other = len(tiles) - len(relevant)
        tiles = relevant

        removed_alliance_id = 0
        if exclude_alliance_ids:
            excluded_set = set(exclude_alliance_ids)
            before = len(tiles)
            tiles = [t for t in tiles if not t.alliance_id or t.alliance_id not in excluded_set]
            removed_alliance_id = before - len(tiles)

        filter_parts = []
        if removed_own > 0:
            filter_parts.append(f"{removed_own} own villages")
        if removed_other > 0:
            filter_parts.append(f"{removed_other} empty/irrelevant")
        if removed_alliance_id > 0:
            filter_parts.append(f"{removed_alliance_id} excluded alliances")
        filter_summary = ", ".join(filter_parts) if filter_parts else "none removed"

        if not await _tracked_send(
            websocket,
            {
                "type": "phase",
                "phase": "pre_filter",
                "message": f"Pre-filter: {before_count} → {len(tiles)} tiles (removed: {filter_summary})",
            },
        ):
            return

        if not tiles:
            await _tracked_send(
                websocket,
                {
                    "type": "complete",
                    "tiles": [],
                    "total": 0,
                    "stats": {
                        "raw_tiles": len(all_tile_data),
                        "after_prefilter": 0,
                        "time_seconds": round(time.monotonic() - t_total_start, 1),
                    },
                },
            )
            return

        # ── Phase 3: Enrichment (the slow part) ────────────────────
        enrich_total = len(tiles)
        if not await _tracked_send(
            websocket,
            {
                "type": "phase",
                "phase": "enriching",
                "message": f"Enriching {enrich_total} tiles with population, player, tribe data...",
                "detail": f"Each tile requires an API call (~2-4s with stealth throttling). "
                f"Estimated time: {enrich_total * 3}–{enrich_total * 5}s",
            },
        ):
            return

        enriched: list[MapTileInfo] = []
        enrich_times: list[float] = []

        for i, tile in enumerate(tiles):
            t_enrich_start = time.monotonic()

            eta_str = ""
            if enrich_times:
                avg_t = sum(enrich_times) / len(enrich_times)
                remaining = enrich_total - i
                eta_secs = avg_t * remaining
                eta_m = int(eta_secs // 60)
                eta_s = int(eta_secs % 60)
                eta_str = f"{eta_m}m{eta_s:02d}s"

            if not await _tracked_send(
                websocket,
                {
                    "type": "enrich_progress",
                    "index": i + 1,
                    "total": enrich_total,
                    "tile": {"x": tile.x, "y": tile.y, "name": tile.village_name or "?"},
                    "eta": eta_str or None,
                    "message": f"[{i + 1}/{enrich_total}] Fetching ({tile.x},{tile.y}) {tile.village_name or '?'}"
                    f"{' | ETA: ' + eta_str if eta_str else ''}",
                },
            ):
                return

            try:
                detail = await svc.get_tile_details(tile.x, tile.y)
                detail.distance = tile.distance
                detail.is_oasis = tile.is_oasis
                detail.is_abandoned = tile.is_abandoned
                if not detail.village_name and tile.village_name:
                    detail.village_name = tile.village_name
                # Preserve player/alliance from map scan when tile-details
                # didn't extract them (occupied oases may use different HTML)
                if not detail.player_id and tile.player_id:
                    detail.player_id = tile.player_id
                if not detail.player_name and tile.player_name:
                    detail.player_name = tile.player_name
                if not detail.alliance_id and tile.alliance_id:
                    detail.alliance_id = tile.alliance_id
                if not detail.alliance_name and tile.alliance_name:
                    detail.alliance_name = tile.alliance_name
                enriched.append(detail)

                if not await _tracked_send(
                    websocket,
                    {
                        "type": "enrich_detail",
                        "index": i + 1,
                        "total": enrich_total,
                        "tile": {
                            "x": detail.x,
                            "y": detail.y,
                            "name": detail.village_name,
                            "pop": detail.population,
                            "player": detail.player_name or None,
                            "alliance": detail.alliance_name or None,
                            "tribe": detail.tribe or None,
                            "distance": detail.distance,
                        },
                    },
                ):
                    return

            except Exception as e:
                logger.warning("Enrich failed for (%s,%s): %s", tile.x, tile.y, e)
                enriched.append(tile)
                await _tracked_send(
                    websocket,
                    {
                        "type": "enrich_detail",
                        "index": i + 1,
                        "total": enrich_total,
                        "tile": {
                            "x": tile.x,
                            "y": tile.y,
                            "name": tile.village_name,
                            "error": str(e),
                        },
                    },
                )

            enrich_times.append(time.monotonic() - t_enrich_start)

        tiles = enriched
        if not await _tracked_send(
            websocket,
            {
                "type": "phase",
                "phase": "enrich_done",
                "message": f"Enrichment complete: {len(tiles)} tiles enriched "
                f"({sum(enrich_times):.1f}s total, {sum(enrich_times) / len(enrich_times):.1f}s avg)",
            },
        ):
            return

        # ── Phase 3b: Compute player populations ──────────────────
        # Visible village sums (always computed for the debug breakdown)
        visible_pops = _sum_visible_player_pops(tiles)

        # When max_player_pop filter is active, fetch REAL total
        # populations from each player's profile page so villages
        # outside the scan radius are counted.
        if max_player_pop is not None and visible_pops:
            unique_pids = set(visible_pops.keys())
            if not await _tracked_send(
                websocket,
                {
                    "type": "phase",
                    "phase": "player_profiles",
                    "message": f"Fetching {len(unique_pids)} player profile(s) for accurate population…",
                },
            ):
                return
            profile_pops = await svc.fetch_player_populations(unique_pids)
            # Use profile pop when available; fall back to visible sum
            player_pops = {
                pid: profile_pops.get(pid) or visible_pops.get(pid, 0) for pid in unique_pids
            }
            pop_source = "profile"

            # Inherit: occupied oases have population=0 but belong to a
            # player.  Set their population to the owner's total so both
            # village-level and player-level filters apply correctly and
            # the scan results show a meaningful number.
            for t in tiles:
                if t.is_oasis and t.player_id and t.population == 0:
                    owner_pop = player_pops.get(t.player_id, 0)
                    if owner_pop > 0:
                        t.population = owner_pop
        else:
            player_pops = visible_pops
            pop_source = "visible"

        if player_pops:
            # Build per-village breakdown keyed by player_id
            pv_map: dict[int, list[dict]] = {}
            pn_map: dict[int, str] = {}
            for t in tiles:
                if t.player_id and t.player_id in player_pops:
                    pn_map.setdefault(t.player_id, t.player_name or "?")
                    pv_map.setdefault(t.player_id, []).append(
                        {
                            "name": t.village_name or f"({t.x},{t.y})",
                            "pop": t.population,
                            "x": t.x,
                            "y": t.y,
                        }
                    )

            if not await _tracked_send(
                websocket,
                {
                    "type": "player_pops",
                    "source": pop_source,
                    "players": [
                        {
                            "name": pn_map[pid],
                            "total": player_pops[pid],
                            "visible_total": visible_pops.get(pid, 0),
                            "villages": pv_map[pid],
                            "source": pop_source,
                        }
                        for pid in sorted(player_pops, key=lambda p: -player_pops[p])
                    ],
                },
            ):
                return

        # ── Phase 4: Post-enrichment filtering ──────────────────────
        post_filter_msgs = []

        # Alliance name filter
        if exclude_alliance_names:
            excluded_names_lower = {n.lower() for n in exclude_alliance_names}
            before = len(tiles)
            tiles = [
                t
                for t in tiles
                if not t.alliance_name or t.alliance_name.lower() not in excluded_names_lower
            ]
            removed = before - len(tiles)
            if removed > 0:
                post_filter_msgs.append(f"Alliance names: -{removed}")

        # Player name filter
        exclude_player_ids: set[int] = set()
        if exclude_player_names:
            name_lower_set = {n.lower() for n in exclude_player_names}
            for t in tiles:
                if t.player_name and t.player_name.lower() in name_lower_set and t.player_id:
                    exclude_player_ids.add(t.player_id)

        # Population + other filters
        before = len(tiles)
        tiles = svc.filter_targets(
            tiles,
            max_population=max_pop,
            min_population=min_pop,
            only_no_player=False,
            exclude_oases=not show_oases,
            exclude_player_ids=exclude_player_ids or None,
        )
        removed = before - len(tiles)
        if removed > 0:
            parts = []
            if min_pop is not None or max_pop is not None:
                parts.append(f"pop {min_pop or 0}–{max_pop or '∞'}")
            if exclude_player_ids:
                parts.append(f"{len(exclude_player_ids)} excluded players")
            post_filter_msgs.append(
                f"Filters ({', '.join(parts) if parts else 'combined'}): -{removed}"
            )

        # Player total pop filter
        if max_player_pop is not None:
            before = len(tiles)
            removed_players = set()
            filtered = []
            for t in tiles:
                if not t.player_id:
                    filtered.append(t)
                    continue
                ppop = player_pops.get(t.player_id, 0)
                if ppop <= max_player_pop:
                    filtered.append(t)
                else:
                    removed_players.add((t.player_name or "?", ppop))
            tiles = filtered
            removed = before - len(tiles)
            if removed > 0:
                player_detail = ", ".join(
                    f"{name}({pop})" for name, pop in sorted(removed_players, key=lambda x: -x[1])
                )
                post_filter_msgs.append(
                    f"Player pop >{max_player_pop}: -{removed} ({player_detail})"
                )

        # Limit
        if len(tiles) > limit:
            tiles = tiles[:limit]
            post_filter_msgs.append(f"Capped at limit={limit}")

        filter_detail = (
            "; ".join(post_filter_msgs) if post_filter_msgs else "no additional filtering needed"
        )
        if not await _tracked_send(
            websocket,
            {
                "type": "phase",
                "phase": "post_filter",
                "message": f"Post-filter: {len(tiles)} targets remaining ({filter_detail})",
            },
        ):
            return

        # ── Phase 5: Send results ───────────────────────────────────
        total_time = time.monotonic() - t_total_start
        tile_dicts = [
            {
                "x": t.x,
                "y": t.y,
                "village_id": t.village_id,
                "player_id": t.player_id,
                "alliance_id": t.alliance_id,
                "alliance_name": t.alliance_name,
                "village_name": t.village_name,
                "player_name": t.player_name,
                "tribe": t.tribe,
                "population": t.population,
                "distance": t.distance,
                "is_oasis": t.is_oasis,
                "is_abandoned": t.is_abandoned,
            }
            for t in tiles
        ]

        await _tracked_send(
            websocket,
            {
                "type": "complete",
                "tiles": tile_dicts,
                "total": len(tile_dicts),
                "stats": {
                    "raw_tiles": len(all_tile_data),
                    "after_prefilter": enrich_total,
                    "enriched": len(enriched),
                    "final": len(tile_dicts),
                    "time_seconds": round(total_time, 1),
                    "enrich_time_seconds": round(sum(enrich_times), 1) if enrich_times else 0,
                    "avg_enrich_time": round(sum(enrich_times) / len(enrich_times), 1)
                    if enrich_times
                    else 0,
                },
            },
        )

    except (WebSocketDisconnect, RuntimeError):
        logger.info("Scout scan WS disconnected: user=%s", user_id)
    except Exception:
        logger.exception("Unexpected error in scout scan WS for user %s", user_id)
        await _tracked_send(websocket, {"type": "error", "message": "Internal scan error"})
    finally:
        operation_gate.release(user_id, scan_op_type)
        exec_session_manager.mark_disconnected(exec_session.id)
        await ws_manager.disconnect(user_id, SCAN_CHANNEL, websocket)
