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

    await ws_manager.connect(websocket, user_id, SCAN_CHANNEL)

    try:
        # Wait for config
        try:
            raw = await websocket.receive_text()
        except WebSocketDisconnect:
            return

        import json as _json
        try:
            config = _json.loads(raw)
        except (ValueError, TypeError):
            await _send(websocket, {"type": "error", "message": "Invalid JSON"})
            return

        radius = config.get("radius", 10)
        village_id = config.get("village_id") or session.active_village_id
        min_pop = config.get("min_pop")
        max_pop = config.get("max_pop")
        max_player_pop = config.get("max_player_pop")
        show_oases = config.get("show_oases", False)
        limit = config.get("limit", 100)
        exclude_alliance_ids = config.get("exclude_alliance_ids", [])
        exclude_alliance_names = config.get("exclude_alliance_names", [])
        exclude_player_names = config.get("exclude_player_names", [])

        center_village = next(
            (v for v in session.auth_state.villages if v.id == village_id), None
        )
        if not center_village:
            await _send(websocket, {"type": "error", "message": f"Village {village_id} not found"})
            return

        cx, cy = center_village.x, center_village.y
        svc = session.scout_service

        import math
        t_total_start = time.monotonic()

        # ── Phase 1: Map scan ───────────────────────────────────────
        step = 15
        scan_centers = []
        for scx in range(cx - radius, cx + radius + 1, step * 2):
            for scy in range(cy - radius, cy + radius + 1, step * 2):
                scan_centers.append((scx, scy))

        num_regions = len(scan_centers)
        if not await _send(websocket, {
            "type": "phase",
            "phase": "map_scan",
            "message": f"Scanning {num_regions} map region(s) around ({cx},{cy}) r={radius}...",
        }):
            return

        all_tile_data: dict[tuple[int, int], dict] = {}
        for idx, (scx, scy) in enumerate(scan_centers):
            if not await _send(websocket, {
                "type": "scan_region",
                "index": idx + 1,
                "total": num_regions,
                "center": {"x": scx, "y": scy},
            }):
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

            raw_tiles.append(MapTileInfo(
                x=x, y=y,
                village_id=did if did > 0 else 0,
                player_id=uid if uid else None,
                alliance_id=aid if aid else None,
                village_name=village_name,
                distance=round(dist, 2),
                is_oasis=is_oasis,
                is_abandoned=did == -1 and uid is None,
            ))

        if not await _send(websocket, {
            "type": "phase",
            "phase": "map_scan_done",
            "message": f"Map scan complete: {len(all_tile_data)} raw tiles, {len(raw_tiles)} with villages/oases",
        }):
            return

        # ── Phase 2: Pre-enrichment filtering ───────────────────────
        own_ids = {v.id for v in session.auth_state.villages}
        before_count = len(raw_tiles)
        tiles = [t for t in raw_tiles if t.village_id > 0 and t.village_id not in own_ids]
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

        if not await _send(websocket, {
            "type": "phase",
            "phase": "pre_filter",
            "message": f"Pre-filter: {before_count} → {len(tiles)} tiles (removed: {filter_summary})",
        }):
            return

        if not tiles:
            await _send(websocket, {"type": "complete", "tiles": [], "total": 0, "stats": {
                "raw_tiles": len(all_tile_data), "after_prefilter": 0,
                "time_seconds": round(time.monotonic() - t_total_start, 1),
            }})
            return

        # ── Phase 3: Enrichment (the slow part) ────────────────────
        enrich_total = len(tiles)
        if not await _send(websocket, {
            "type": "phase",
            "phase": "enriching",
            "message": f"Enriching {enrich_total} tiles with population, player, tribe data...",
            "detail": f"Each tile requires an API call (~2-4s with stealth throttling). "
                      f"Estimated time: {enrich_total * 3}–{enrich_total * 5}s",
        }):
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

            if not await _send(websocket, {
                "type": "enrich_progress",
                "index": i + 1,
                "total": enrich_total,
                "tile": {"x": tile.x, "y": tile.y, "name": tile.village_name or "?"},
                "eta": eta_str or None,
                "message": f"[{i + 1}/{enrich_total}] Fetching ({tile.x},{tile.y}) {tile.village_name or '?'}"
                           f"{' | ETA: ' + eta_str if eta_str else ''}",
            }):
                return

            try:
                detail = await svc.get_tile_details(tile.x, tile.y)
                detail.distance = tile.distance
                detail.is_oasis = tile.is_oasis
                detail.is_abandoned = tile.is_abandoned
                if not detail.village_name and tile.village_name:
                    detail.village_name = tile.village_name
                enriched.append(detail)

                if not await _send(websocket, {
                    "type": "enrich_detail",
                    "index": i + 1,
                    "total": enrich_total,
                    "tile": {
                        "x": detail.x, "y": detail.y,
                        "name": detail.village_name, "pop": detail.population,
                        "player": detail.player_name or None,
                        "alliance": detail.alliance_name or None,
                        "tribe": detail.tribe or None,
                        "distance": detail.distance,
                    },
                }):
                    return

            except Exception as e:
                logger.warning("Enrich failed for (%s,%s): %s", tile.x, tile.y, e)
                enriched.append(tile)
                await _send(websocket, {
                    "type": "enrich_detail",
                    "index": i + 1,
                    "total": enrich_total,
                    "tile": {"x": tile.x, "y": tile.y, "name": tile.village_name, "error": str(e)},
                })

            enrich_times.append(time.monotonic() - t_enrich_start)

        tiles = enriched
        if not await _send(websocket, {
            "type": "phase",
            "phase": "enrich_done",
            "message": f"Enrichment complete: {len(tiles)} tiles enriched "
                       f"({sum(enrich_times):.1f}s total, {sum(enrich_times)/len(enrich_times):.1f}s avg)",
        }):
            return

        # ── Phase 3b: Compute player populations ──────────────────
        # GQL player.population returns 0 for other players, so we sum
        # all visible village populations per player from the enriched tiles.
        player_pops: dict[int, int] = {}
        if max_player_pop is not None:
            player_pops = _sum_visible_player_pops(tiles)

            # Log what we found
            seen: set[int] = set()
            deduped = []
            for t in tiles:
                if t.player_id and t.player_id not in seen and t.player_id in player_pops:
                    seen.add(t.player_id)
                    deduped.append(f"{t.player_name or '?'}={player_pops[t.player_id]}")
            deduped.sort(key=lambda s: -int(s.split('=')[1]))
            if not await _send(websocket, {
                "type": "phase",
                "phase": "player_pop",
                "message": f"Player populations (visible sum): {', '.join(deduped[:25])}"
                           f"{'...' if len(deduped) > 25 else ''}",
            }):
                return

        # ── Phase 4: Post-enrichment filtering ──────────────────────
        post_filter_msgs = []

        # Alliance name filter
        if exclude_alliance_names:
            excluded_names_lower = {n.lower() for n in exclude_alliance_names}
            before = len(tiles)
            tiles = [t for t in tiles if not t.alliance_name or t.alliance_name.lower() not in excluded_names_lower]
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
            post_filter_msgs.append(f"Filters ({', '.join(parts) if parts else 'combined'}): -{removed}")

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
                post_filter_msgs.append(f"Player pop >{max_player_pop}: -{removed} ({player_detail})")

        # Limit
        if len(tiles) > limit:
            tiles = tiles[:limit]
            post_filter_msgs.append(f"Capped at limit={limit}")

        filter_detail = "; ".join(post_filter_msgs) if post_filter_msgs else "no additional filtering needed"
        if not await _send(websocket, {
            "type": "phase",
            "phase": "post_filter",
            "message": f"Post-filter: {len(tiles)} targets remaining ({filter_detail})",
        }):
            return

        # ── Phase 5: Send results ───────────────────────────────────
        total_time = time.monotonic() - t_total_start
        tile_dicts = [
            {
                "x": t.x, "y": t.y,
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

        await _send(websocket, {
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
                "avg_enrich_time": round(sum(enrich_times) / len(enrich_times), 1) if enrich_times else 0,
            },
        })

    except WebSocketDisconnect:
        logger.info("Scout scan WS disconnected: user=%s", user_id)
    except Exception:
        logger.exception("Unexpected error in scout scan WS for user %s", user_id)
        await _send(websocket, {"type": "error", "message": "Internal scan error"})
    finally:
        await ws_manager.disconnect(user_id, SCAN_CHANNEL)
