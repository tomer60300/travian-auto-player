"""WebSocket handlers for auto-scout and map-scan operations.

Both endpoints (``/ws/scout/auto``, ``/ws/scout/scan``) wrap the actual sweep
logic in a managed background operation. Backgrounding Safari (or any WS
drop) does NOT halt the sweep — the op continues server-side and clients
can reconnect via ``/ws/sessions/{id}/stream`` to resume the live tail
with full message history.

Optimised stealth flow preserved from the original:
- Rally point navigation happens ONCE per sweep, not per target.
- During stealth delays, countdown messages are pushed so the user sees
  progress instead of an apparent freeze.
- ETA is computed after the first target and updated each cycle.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from travian_api.exceptions import ActivityBudgetExhausted, ReconStrictViolation
from travian_api.models.farm_list import MapTileInfo
from travian_api.operation_manager import OperationContext, operation_manager
from travian_api.parsers.html_parser import parse_troop_confirm_page
from travian_api.services.auto_scout_service import _format_bonus_breakdown
from travian_api.stealth.human_delay import ActionType
from travian_api.stealth.timing import HumanTiming
from travian_api.web.operation_gate import active_ops
from travian_api.web.routes.military import _resolve_scout_unit
from travian_api.web.sessions import TravianSession, session_manager
from travian_api.web.ws._resumable import subscribe_and_tail
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()

CHANNEL = "scout_auto"
SCAN_CHANNEL = "scout_scan"


def _resolve_recon_flags(use_recon: bool, recon_strict: bool) -> tuple[bool, bool]:
    """Normalize the two client-supplied recon flags into the backend's
    effective decision: ``(acquire_recon, strict)``.

    ``strict`` ("Require background account") IMPLIES recon usage. A
    request that requires the background account but also unchecks "use
    background account" is contradictory; strict wins. Without this,
    ``use_recon=False`` + ``recon_strict=True`` would skip recon
    acquisition, sail past the entry guard (which keys off recon being
    required), and route every read onto the PRIMARY account — the exact
    leak strict mode exists to forbid. So strict mode can never be
    disabled by the opt-out flag.
    """
    return (use_recon or recon_strict, recon_strict)


# ---------------------------------------------------------------------------
# Low-level scout dispatch — unchanged from the original
# ---------------------------------------------------------------------------


async def _stealth_delay_with_countdown_ctx(
    ctx: OperationContext, seconds: float, label: str
) -> bool:
    """Sleep for *seconds* while pushing one ``waiting`` message per second.

    Returns True if the operation was stopped (explicit stop or captcha
    resolution) during the wait, False if the sleep completed naturally.
    """
    remaining = seconds
    while remaining > 0:
        chunk = min(remaining, 1.0)
        ctx.push(
            {
                "type": "waiting",
                "message": f"{label} ({remaining:.0f}s remaining)",
                "remaining": round(remaining, 1),
            }
        )
        if await ctx.wait_or_stop(chunk):
            return True
        if ctx.should_stop():  # captcha-stop poll
            return True
        remaining -= chunk
    return False


async def _send_scout_fast(
    session: TravianSession,
    x: int,
    y: int,
    amount: int,
    scout_type: str,
    village_id: int,
    is_first: bool,
) -> dict:
    """Send scouts to a single target with optimised stealth.

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

    if is_first:
        await http.navigator.navigate_to_rally_point(village_id)

    await delay.wait(ActionType.RAPID, "selecting troops")

    form_data: dict[str, str] = {}
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
        return {
            "success": False,
            "error": error_msg or "No confirmation form",
            "travel_time": None,
        }

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

    action_token = final_data.get("action", "")
    form_reappeared = action_token and f'value="{action_token}"' in result_html
    has_error = bool(re.search(r'class="error[^"]*"', result_html))
    has_movement = "troopMovement" in result_html
    success = (not form_reappeared and not has_error) or has_movement

    travel_time = None
    time_match = re.search(r'class="in"[^>]*>.*?(\d+:\d+:\d+)', confirm_html, re.DOTALL)
    if time_match:
        travel_time = time_match.group(1)

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


def _sum_visible_player_pops(tiles: list) -> dict[int, int]:
    """Sum village populations per player from enriched tiles."""
    pops: dict[int, int] = {}
    for t in tiles:
        if t.player_id:
            pops[t.player_id] = pops.get(t.player_id, 0) + t.population
    return pops


# ---------------------------------------------------------------------------
# Auto-scout operation coroutine
# ---------------------------------------------------------------------------


def _build_auto_scout_coro(config: dict):
    """Returns the OperationManager coroutine for an auto-scout sweep."""

    async def coro(ctx: OperationContext) -> None:
        session: TravianSession = ctx.session
        radius = config.get("radius")
        if not radius or not isinstance(radius, int) or radius < 1:
            ctx.push({"type": "error", "message": "Invalid radius", "fatal": True})
            return

        amount = config.get("amount", 1)
        scout_type = config.get("type", "resources")
        delay_min = config.get("delay_min", config.get("delay", 2.0))
        delay_max = config.get("delay_max", delay_min + 2.0)
        start_index = config.get("start_index", 0)
        village_id = config.get("village_id") or session.active_village_id
        targets_from_ui = config.get("targets")
        # Recon (background) account toggle — applies only to the
        # read portion of this sweep (the prep scan / target list
        # resolution). Scout dispatch downstream stays on primary
        # because the recon account has no troops to send.
        use_recon = bool(config.get("use_recon", True))
        recon_strict = bool(config.get("recon_strict", False))

        center_village = next((v for v in session.auth_state.villages if v.id == village_id), None)
        if not center_village:
            ctx.push({"type": "error", "message": f"Village {village_id} not found", "fatal": True})
            return

        cx, cy = center_village.x, center_village.y
        svc = session.scout_service

        # Recon HttpClient wiring — same pattern as the map-scan coro.
        # See _build_scout_scan_coro for full context. The recon proxy
        # only fronts the READ portion of the sweep (target list
        # resolution); the scout dispatch downstream stays on the
        # active user's primary because recon has no troops.
        from ...services.auto_scout_service import _recon_context, _recon_strict_context
        from ...services.recon_account import recon_account_manager

        # Strict mode implies recon usage — see _resolve_recon_flags.
        acquire_recon, strict_recon = _resolve_recon_flags(use_recon, recon_strict)
        recon_client = None
        if acquire_recon:
            try:
                recon_client = await recon_account_manager.get_or_create_client(
                    session.settings.base_url
                )
            except Exception:
                logger.exception("recon_account_manager.get_or_create_client failed")
                recon_client = None
        active_user_for_strict = (
            session.auth_state.player_name if session.auth_state else session.settings.username
        )
        # Abort whenever strict mode is on and no recon client is
        # available — unconfigured OR unauthenticated. NOT gated on
        # is_configured() (that gate was the silent-no-op bug). See the
        # equivalent block in _build_scout_scan_coro.
        if strict_recon and recon_client is None:
            configured = recon_account_manager.is_configured()
            reason = (
                "the background account couldn't authenticate"
                if configured
                else "no background account is configured on the server"
            )
            logger.error(
                "AutoScout-sweep STRICT-recon: recon proxy unavailable "
                "(configured=%s) AND strict mode is on. Aborting sweep "
                "for active_user=%s.",
                configured,
                active_user_for_strict,
            )
            ctx.push(
                {
                    "type": "error",
                    "fatal": True,
                    "message": (
                        f"Strict background-account mode is on, but {reason}. "
                        "The sweep was aborted to prevent leaking scout "
                        "traffic onto your active account."
                    ),
                }
            )
            return
        # ContextVar — race-safe per-coroutine routing. See the
        # equivalent block in _build_scout_scan_coro for why this is
        # preferred over the legacy `svc.recon_http_client` attribute.
        _recon_context.set(recon_client)
        # Arm choke-point enforcement for the read portion of the sweep.
        _recon_strict_context.set(strict_recon)
        svc.recon_http_client = recon_client  # belt-and-braces backward compat
        active_user = (
            session.auth_state.player_name if session.auth_state else session.settings.username
        )
        if recon_client is not None:
            proxy_user = recon_account_manager.get_proxy_username() or "(recon)"
            logger.info(
                "AutoScout-sweep proxy ACTIVE: read portion (target "
                "resolution) for active_user=%s dispatches through "
                "recon proxy username=%s. Scout DISPATCH stays on the "
                "active user's primary account (recon has no troops).",
                active_user,
                proxy_user,
            )

        cli_parts = [f"travian scout auto --radius {radius} --village-id {village_id}"]
        cli_parts.append(f"--amount {amount}")
        cli_parts.append(f"--type {scout_type}")
        cli_parts.append(f"--delay {delay_min}")
        if start_index:
            cli_parts.append(f"--start-index {start_index}")
        if targets_from_ui:
            cli_parts.append(f"--targets {len(targets_from_ui)}")
        ctx.push({"type": "trigger_info", "command": " ".join(cli_parts)})

        # ── Phase 1: Resolve targets ─────────────────────────────────────
        if targets_from_ui is not None:
            if not targets_from_ui:
                ctx.push(
                    {
                        "type": "complete",
                        "total_sent": 0,
                        "successful": 0,
                        "next_start_index": 0,
                    }
                )
                return

            tiles: list[MapTileInfo] = []
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
                ctx.push(
                    {
                        "type": "complete",
                        "total_sent": 0,
                        "successful": 0,
                        "next_start_index": 0,
                    }
                )
                return

            ctx.push({"type": "scan_complete", "targets": len(tiles)})
            ctx.push(
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
                }
            )
        else:
            ctx.push({"type": "scanning", "message": f"Scanning ({cx},{cy}) r={radius}..."})
            tiles = await svc.scan_map(cx, cy, radius)
            own_ids = {v.id for v in session.auth_state.villages}
            tiles = [
                t
                for t in tiles
                if t.village_id > 0 and t.village_id not in own_ids and not t.is_oasis
            ]

            if tiles:
                ctx.push({"type": "scanning", "message": f"Enriching {len(tiles)} tiles..."})
                tiles = await svc.enrich_tiles(tiles)

            tiles = svc.filter_targets(tiles, exclude_oases=True)

        if not tiles:
            ctx.push({"type": "scan_complete", "targets": 0})
            ctx.push(
                {
                    "type": "complete",
                    "total_sent": 0,
                    "successful": 0,
                    "next_start_index": 0,
                }
            )
            return

        total = len(tiles)
        ctx.push({"type": "scan_complete", "targets": total})

        ctx.push(
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
            }
        )

        # ── Player population breakdown (debug aid) ─────────────────────
        player_villages: dict[str, list[tuple[str, int, int, int]]] = {}
        for t in tiles:
            pname = t.player_name or ""
            if pname:
                vname = t.village_name or f"({t.x},{t.y})"
                player_villages.setdefault(pname, []).append((vname, t.population, t.x, t.y))

        if player_villages:
            ctx.push(
                {
                    "type": "player_pops",
                    "players": [
                        {
                            "name": pname,
                            "total": sum(vp for _, vp, *_ in vils),
                            "villages": [
                                {"name": vn, "pop": vp, "x": vx, "y": vy} for vn, vp, vx, vy in vils
                            ],
                        }
                        for pname, vils in sorted(
                            player_villages.items(),
                            key=lambda kv: -sum(vp for _, vp, *_ in kv[1]),
                        )
                    ],
                }
            )

        original_total = len(tiles)

        # Rotate targets for round-robin resume
        if start_index > 0 and start_index < len(tiles):
            tiles = tiles[start_index:] + tiles[:start_index]

        # ── Pre-flight: check available scouts ─────────────────────────
        scout_unit = _resolve_scout_unit(session.tribe_id)
        try:
            preflight_troops = await session.military_service.get_available_troops(village_id)
            available_scouts = preflight_troops.get(scout_unit, 0)
        except Exception:
            available_scouts = -1

        scouts_per_target = amount * (2 if scout_type == "both" else 1)

        if available_scouts >= 0:
            max_targets = available_scouts // scouts_per_target if scouts_per_target > 0 else 0
            ctx.push(
                {
                    "type": "scout_preflight",
                    "available": available_scouts,
                    "needed_per_target": scouts_per_target,
                    "can_send_to": max_targets,
                    "total_targets": original_total,
                }
            )

            if max_targets == 0:
                ctx.push(
                    {
                        "type": "scouts_exhausted",
                        "available": 0,
                        "message": f"No scouts available (0 {scout_unit} idle)",
                        "sent_so_far": 0,
                        "successful": 0,
                    }
                )
                ctx.push(
                    {
                        "type": "complete",
                        "total_sent": 0,
                        "successful": 0,
                        "next_start_index": start_index,
                    }
                )
                return
            if max_targets < len(tiles):
                tiles = tiles[:max_targets]
                ctx.push(
                    {
                        "type": "scouts_capped",
                        "available": available_scouts,
                        "can_send_to": max_targets,
                        "total_targets": original_total,
                        "message": (
                            f"Only {available_scouts} scouts idle — capped to {max_targets} targets"
                        ),
                    }
                )

        total = len(tiles)

        # ── Phase 2: Send scouts (stealth mode) ────────────────────────
        if ctx.should_stop():
            ctx.push(
                {
                    "type": "error",
                    "message": "Stopped after captcha resolution — restart manually",
                }
            )
            ctx.push(
                {
                    "type": "complete",
                    "total_sent": 0,
                    "successful": 0,
                    "next_start_index": start_index,
                }
            )
            return

        try:
            session.http_client.check_activity_budget()
        except ActivityBudgetExhausted as exc:
            ctx.push({"type": "error", "message": str(exc), "fatal": True})
            ctx.push(
                {
                    "type": "complete",
                    "total_sent": 0,
                    "successful": 0,
                    "next_start_index": start_index,
                }
            )
            return

        results: list[dict] = []
        times_per_target: list[float] = []
        t_start_total = time.monotonic()
        consecutive_troop_failures = 0
        force_navigate = False
        renav_interval = random.randint(8, 15)
        mean_delay = (delay_min + delay_max) / 2
        delay_obj = session.http_client.human_delay

        for i, target in enumerate(tiles):
            if ctx.should_stop():
                break

            t_start = time.monotonic()

            eta_str = ""
            if times_per_target:
                avg = sum(times_per_target) / len(times_per_target)
                remaining_targets = total - i
                eta_secs = avg * remaining_targets
                eta_min = int(eta_secs // 60)
                eta_sec = int(eta_secs % 60)
                eta_str = f" | ETA: {eta_min}m{eta_sec:02d}s"

            ctx.push(
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
                }
            )

            # ── Stealth: noise injection (15% chance) ─────────────
            noise = getattr(session.http_client, "noise_injector", None)
            if noise and hasattr(noise, "maybe_inject_noise") and i > 0:
                try:
                    injected = await noise.maybe_inject_noise()
                    if injected:
                        ctx.push(
                            {
                                "type": "noise_action",
                                "message": "Idle browsing (stealth)...",
                            }
                        )
                        force_navigate = True
                except Exception:
                    pass

            # ── Stealth: periodic re-navigation ───────────────────
            if i > 0 and i % renav_interval == 0:
                renav_interval = random.randint(8, 15)
                try:
                    await delay_obj.wait(ActionType.PAGE_LOAD, "browsing")
                    nav = getattr(session.http_client, "navigator", None)
                    if nav and hasattr(nav, "idle_browse"):
                        await nav.idle_browse()
                        force_navigate = True
                        ctx.push(
                            {
                                "type": "re_navigate",
                                "message": "Breaking request pattern (stealth)...",
                            }
                        )
                except Exception:
                    pass

            use_is_first = (i == 0) or force_navigate
            if force_navigate:
                force_navigate = False

            try:
                if scout_type == "both":
                    result_res = await _send_scout_fast(
                        session,
                        target.x,
                        target.y,
                        amount,
                        "resources",
                        village_id,
                        is_first=use_is_first,
                    )
                    await delay_obj.wait(ActionType.DECISION, "switching scout type")
                    result_def = await _send_scout_fast(
                        session,
                        target.x,
                        target.y,
                        amount,
                        "defenses",
                        village_id,
                        is_first=False,
                    )
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

                ctx.push(
                    {
                        "type": "scout_result",
                        "target": {"x": target.x, "y": target.y},
                        "success": result["success"],
                        "error": result.get("error"),
                        "travel_time": result.get("travel_time"),
                        "index": i + 1,
                        "total": total,
                    }
                )

            except Exception as e:
                logger.warning("Scout error for (%s,%s): %s", target.x, target.y, e)
                result = {"success": False, "error": str(e), "travel_time": None}
                results.append(result)
                ctx.push(
                    {
                        "type": "scout_result",
                        "target": {"x": target.x, "y": target.y},
                        "success": False,
                        "error": str(e),
                        "travel_time": None,
                        "index": i + 1,
                        "total": total,
                    }
                )

            t_elapsed = time.monotonic() - t_start
            times_per_target.append(t_elapsed)

            session.http_client.activity_scheduler.log_activity(t_elapsed)
            if (i + 1) % 5 == 0:
                try:
                    session.http_client.check_activity_budget()
                except ActivityBudgetExhausted as exc:
                    # fatal=True so OperationManager terminal detection
                    # marks this scout pass as FAILED.
                    ctx.push({"type": "error", "message": str(exc), "fatal": True})
                    break

            if not result.get("success") and "troops" in (result.get("error") or "").lower():
                consecutive_troop_failures += 1
            else:
                consecutive_troop_failures = 0

            if consecutive_troop_failures >= 2:
                ctx.push(
                    {
                        "type": "scouts_exhausted",
                        "sent_so_far": i + 1,
                        "successful": sum(1 for r in results if r.get("success")),
                        "message": "Scouts ran out — stopping batch",
                    }
                )
                break

            # ── Inter-target delay ─────────────────────────────────
            if i < total - 1:
                fatigue_factor = 1.0 + (i / max(total, 1)) * 0.3
                stealth_secs = HumanTiming.delay(mean_delay) * fatigue_factor
                stealth_secs = max(1.0, min(stealth_secs, mean_delay * 15))
                if await _stealth_delay_with_countdown_ctx(
                    ctx,
                    stealth_secs,
                    f"Stealth cooldown before target {i + 2}/{total}",
                ):
                    break

        # ── Phase 3: Summary ───────────────────────────────────────
        total_sent = len(results)
        successful = sum(1 for r in results if r.get("success"))
        total_time = time.monotonic() - t_start_total
        avg_time = total_time / total_sent if total_sent else 0
        ctx.push(
            {
                "type": "complete",
                "total_sent": total_sent,
                "successful": successful,
                "total_time_seconds": round(total_time, 1),
                "avg_time_per_target": round(avg_time, 1),
                "next_start_index": (start_index + total_sent) % original_total
                if original_total > 0
                else 0,
            }
        )

    return coro


# ---------------------------------------------------------------------------
# Map-scan operation coroutine
# ---------------------------------------------------------------------------


def _build_scout_scan_coro(config: dict):
    """Returns the OperationManager coroutine for a map scan with enrichment."""

    async def coro(ctx: OperationContext) -> None:
        session: TravianSession = ctx.session
        radius = config.get("radius", 10)
        village_id = config.get("village_id") or session.active_village_id
        min_pop = config.get("min_pop")
        max_pop = config.get("max_pop")
        max_player_pop = config.get("max_player_pop")
        show_oases = config.get("show_oases", False)
        # Mutually exclusive with show_oases: when oasis_only is True, the
        # result contains ONLY oases (occupied + unoccupied) and villages
        # are filtered out. Implies show_oases.
        oasis_only = bool(config.get("oasis_only", False))
        if oasis_only:
            show_oases = True
        limit = config.get("limit", 99999)
        exclude_alliance_ids = config.get("exclude_alliance_ids", [])
        exclude_alliance_names = config.get("exclude_alliance_names", [])
        exclude_player_names = config.get("exclude_player_names", [])

        # Oasis bonus filters (see Auto-Scout bonus-feature plan).
        # bonus_resource_mins maps canonical resource_id ("wood"/"clay"/
        # "iron"/"crop") to a minimum percentage (one of 25/50/75/100).
        # bonus_total_levels is the multi-select bucket set — an oasis
        # passes if its TOTAL bonus sum equals any selected level.
        # Both fail closed on malformed shapes: filter is a no-op rather
        # than an error.
        _ALLOWED_RESOURCES = {"wood", "clay", "iron", "crop"}
        _ALLOWED_LEVELS = {25, 50, 75, 100}
        raw_mins = config.get("bonus_resource_mins") or {}
        bonus_resource_mins: dict[str, int] = {}
        if isinstance(raw_mins, dict):
            for k, v in raw_mins.items():
                if (
                    isinstance(k, str)
                    and k in _ALLOWED_RESOURCES
                    and isinstance(v, (int, float))
                    and int(v) > 0
                ):
                    bonus_resource_mins[k] = int(v)
        raw_levels = config.get("bonus_total_levels") or []
        bonus_total_levels: set[int] = set()
        if isinstance(raw_levels, list):
            for v in raw_levels:
                if isinstance(v, (int, float)) and int(v) in _ALLOWED_LEVELS:
                    bonus_total_levels.add(int(v))

        # Target-type flag: "non-capitals" mode = player villages MINUS
        # capital villages. Backward-compat alias path: the legacy
        # `show_capitals` flag is intentionally NOT honored — capital
        # identification is now strictly tied to the non-capitals
        # filter, removing an entire profile-fetch phase for every
        # other scan mode.
        non_capitals = bool(config.get("non_capitals", False))

        # Target-type flag: "villages by oasis bonus" — find player villages
        # whose AGGREGATED occupied-oasis bonus meets the bonus filter. This
        # mode is village-only: oases are an aggregation input, never result
        # rows, so we force the oasis-display flags off to spend zero
        # enrichment requests on oasis tiles. The only oasis I/O is the
        # deduped, cached per-oasis tile-details fetched during aggregation.
        villages_by_oasis_bonus = bool(config.get("villages_by_oasis_bonus", False))
        if villages_by_oasis_bonus:
            show_oases = False
            oasis_only = False

        # Recon (background) account toggle — when True AND the recon
        # account is configured + authenticates successfully, all read
        # ops in this scan (map_position, tile-details, profile pages)
        # route through the recon HttpClient. Write ops stay on the
        # user's primary http_client. Default: ON when configured.
        use_recon = bool(config.get("use_recon", True))
        # Strict-recon mode — when True and use_recon is True, abort
        # the scan with a fatal error if the recon account is
        # configured but can't authenticate. Default OFF preserves
        # the existing visible-warning + fallback-to-primary path
        # for users who'd rather get results than no results. Strict
        # mode is for users who consider any scout traffic on their
        # primary unacceptable — they'd rather see the scan fail.
        recon_strict = bool(config.get("recon_strict", False))

        center_village = next((v for v in session.auth_state.villages if v.id == village_id), None)
        if not center_village:
            ctx.push({"type": "error", "message": f"Village {village_id} not found", "fatal": True})
            return

        cx, cy = center_village.x, center_village.y
        svc = session.scout_service

        # Wire the recon HttpClient via the per-coroutine ContextVar.
        # Lazy auth — first scan of the process eats ~5s; subsequent
        # scans reuse the cached session. Returns None when creds
        # aren't configured, the user opted out, or auth has failed
        # (the recon manager logs a warning).
        from ...services.auto_scout_service import _recon_context, _recon_strict_context
        from ...services.recon_account import recon_account_manager

        # Strict mode implies recon usage — see _resolve_recon_flags.
        acquire_recon, strict_recon = _resolve_recon_flags(use_recon, recon_strict)
        recon_client = None
        if acquire_recon:
            try:
                recon_client = await recon_account_manager.get_or_create_client(
                    session.settings.base_url
                )
            except Exception:
                logger.exception("recon_account_manager.get_or_create_client failed")
                recon_client = None
        # Active user's name — the primary account whose scan this is
        # and whose bot-detection / rate-limit risk we're protecting.
        active_user = (
            session.auth_state.player_name if session.auth_state else session.settings.username
        )
        # Strict-mode abort path: strict mode is on but no recon client is
        # available. Fail loudly — falling back to primary would silently
        # misrepresent the masking the user asked for. The abort fires
        # whether recon is unconfigured OR configured-but-unauthenticated:
        # BOTH leak reads onto the primary, which strict mode forbids.
        # (Gating this on is_configured() was the bug that made the toggle
        # a silent no-op when no recon creds were set.)
        if strict_recon and recon_client is None:
            configured = recon_account_manager.is_configured()
            reason = (
                "the background account couldn't authenticate"
                if configured
                else "no background account is configured on the server"
            )
            remedy = (
                "Rotate the recon credentials, then retry"
                if configured
                else "Configure a background account on the server, then retry"
            )
            logger.error(
                "AutoScout STRICT-recon: recon proxy unavailable "
                "(configured=%s) AND user opted into strict mode. "
                "Aborting scan for active_user=%s rather than leaking "
                "to primary.",
                configured,
                active_user,
            )
            ctx.push(
                {
                    "type": "error",
                    "fatal": True,
                    "message": (
                        f"Strict background-account mode is on, but {reason}. "
                        "The scan was aborted to prevent leaking scout traffic "
                        f"onto your active account. {remedy} — or uncheck "
                        "'Require background account' to fall back to your "
                        "primary."
                    ),
                }
            )
            return
        # Set the ContextVar scoped to this coroutine task. ContextVar
        # values are task-local in asyncio, so a concurrent scout-scan
        # vs auto-scout sweep on the same AutoScoutService instance
        # cannot race on this slot (each task has its own context).
        # No reset needed — the task ends when the coro returns and
        # its context is discarded.
        _recon_context.set(recon_client)
        # Arm the choke-point enforcement: if strict mode is on, any read
        # that reaches `_read_client()` with no recon available raises
        # rather than touching the primary. Defense-in-depth behind the
        # entry guard above.
        _recon_strict_context.set(strict_recon)
        # Keep the legacy attribute populated for any test paths /
        # external callers that still rely on it. ContextVar wins in
        # `_read_client` when both are set; this assignment is a
        # belt-and-braces safety net, not the load-bearing path.
        svc.recon_http_client = recon_client
        if recon_client is not None:
            proxy_user = recon_account_manager.get_proxy_username() or "(recon)"
            # Server log — operators reading :8002.out see exactly
            # which disposable account is fronting the recon traffic
            # for which active user. Hard requirement: this MUST be
            # present so a captcha event on the recon never gets
            # mistakenly attributed to the active user's primary.
            logger.info(
                "AutoScout proxy ACTIVE: read ops for active_user=%s "
                "will dispatch through recon proxy username=%s "
                "(server=%s). Primary account makes zero scout reqs.",
                active_user,
                proxy_user,
                session.settings.base_url,
            )
            ctx.push(
                {
                    "type": "phase",
                    "phase": "recon_active",
                    "message": (
                        f"Using background account [{proxy_user}] as a proxy "
                        f"to dispatch read ops (map scan, tile-details, "
                        f"profiles). Your active account [{active_user}] "
                        "will not appear in any of these requests — bot-"
                        "detection pressure stays on the proxy."
                    ),
                }
            )
        elif use_recon and recon_account_manager.is_configured():
            logger.warning(
                "AutoScout proxy UNAVAILABLE: recon credentials are set "
                "but the recon login isn't authenticated. Read ops for "
                "active_user=%s will fall back to the user's primary "
                "account, increasing bot-detection exposure.",
                active_user,
            )
            ctx.push(
                {
                    "type": "phase",
                    "phase": "recon_unavailable",
                    "message": (
                        "Recon proxy is configured but couldn't authenticate — "
                        f"falling back to your active account [{active_user}] "
                        "for this scan. Check server logs for the recon login "
                        "error and rotate credentials if needed. Enable "
                        "'Require background account' if you'd prefer the "
                        "scan to abort instead of fall back."
                    ),
                }
            )
        elif use_recon:
            # Configured-off path — silent. User asked for recon but
            # operator didn't set creds. Don't spam with a banner.
            logger.info(
                "AutoScout proxy DISABLED (no recon credentials in env): "
                "read ops for active_user=%s will dispatch through the "
                "user's primary account.",
                active_user,
            )

        cli_parts = [f"travian scout scan --radius {radius} --village-id {village_id}"]
        if min_pop is not None:
            cli_parts.append(f"--min-pop {min_pop}")
        if max_pop is not None:
            cli_parts.append(f"--max-pop {max_pop}")
        if max_player_pop is not None:
            cli_parts.append(f"--max-player-pop {max_player_pop}")
        if show_oases:
            cli_parts.append("--show-oases")
        if oasis_only:
            cli_parts.append("--oasis-only")
        if limit != 99999:
            cli_parts.append(f"--limit {limit}")
        if exclude_alliance_names:
            cli_parts.append(f'--exclude-alliances "{",".join(exclude_alliance_names)}"')
        if exclude_player_names:
            cli_parts.append(f'--exclude-players "{",".join(exclude_player_names)}"')
        ctx.push({"type": "trigger_info", "command": " ".join(cli_parts)})

        t_total_start = time.monotonic()

        # Note: the scout no longer maintains its own keepalive loop. The
        # operation manager (operation_manager._global_keepalive) pushes a
        # heartbeat every 10s for every op, so the WebSocket wire stays
        # warm during long silent phases and the client-side watchdog
        # (useResumableOperation.js) has a constant signal-of-life.

        # ── Phase 1: Map scan ───────────────────────────────────────
        # Each /api/v1/map/position call returns a 31x31 tile region
        # CENTERED on the requested point — so one call covers
        # center ± HALF tiles in each axis. To span the user-requested
        # radius around (cx, cy), we walk concentric rings of centers
        # spaced STRIDE apart, just enough that the outer regions
        # reach `radius` from (cx, cy). Critically: when radius <=
        # HALF we issue ONE call AT (cx, cy) — the old algorithm
        # placed the first center at (cx - radius, cy - radius)
        # which only covered the bottom-left quadrant of the
        # requested area for small radii.
        HALF = 15
        STRIDE = 30
        extras = max(0, (radius - HALF + STRIDE - 1) // STRIDE)
        scan_centers: list[tuple[int, int]] = []
        for dx in range(-extras, extras + 1):
            for dy in range(-extras, extras + 1):
                scan_centers.append((cx + dx * STRIDE, cy + dy * STRIDE))

        # Stealth: nearby clusters first, shuffle inside small buckets so
        # the visit order isn't a deterministic raster grid.
        scan_centers.sort(key=lambda p: (p[0] - cx) ** 2 + (p[1] - cy) ** 2)
        for i in range(0, len(scan_centers), 4):
            # Assign back: list slicing returns a copy, shuffling the
            # copy alone wouldn't change the original ordering.
            bucket = scan_centers[i : i + 4]
            random.shuffle(bucket)
            scan_centers[i : i + 4] = bucket

        num_regions = len(scan_centers)
        ctx.push(
            {
                "type": "phase",
                "phase": "map_scan",
                "message": (
                    f"Scanning {num_regions} map region(s) around ({cx},{cy}) r={radius}..."
                ),
            }
        )

        # Establish map-page Referer chain before tile XHRs, matching
        # the navigation a real browser would produce when scanning the
        # map. Crucially, drive the navigator on the SAME HttpClient
        # that fires the upcoming map_position POSTs — otherwise the
        # primary's navigator records "/karte.php" but the actual
        # XHRs go through the recon (or vice versa), breaking the
        # Referer chain Travian's anti-bot inspects.
        scan_client = svc._read_client()
        navigator = getattr(scan_client, "navigator", None)
        if navigator is not None and navigator.enabled:
            await navigator.navigate_to_map()

        all_tile_data: dict[tuple[int, int], dict] = {}
        for idx, (scx, scy) in enumerate(scan_centers):
            if ctx.should_stop():
                # User-initiated stop is not an error. Push a phase
                # frame (frontend renders it as an info line) so the user
                # sees WHY their scan ended without a misleading red toast.
                # operation_manager will still mark the terminal status as
                # STOPPED via ctx.should_stop() detection after we return.
                ctx.push(
                    {
                        "type": "phase",
                        "phase": "stopped",
                        "message": "Scan stopped by user",
                    }
                )
                return
            ctx.push(
                {
                    "type": "scan_region",
                    "index": idx + 1,
                    "total": num_regions,
                    "center": {"x": scx, "y": scy},
                }
            )
            # The map_position POSTs are the bulk of the scan traffic
            # (one per region, ~4 regions for radius 10). Route them
            # through the recon proxy when active — same data shape
            # regardless of which logged-in player asks.
            resp = await scan_client.post_json(
                "/api/v1/map/position",
                {"data": {"x": scx, "y": scy, "zoomLevel": 3, "ignorePositions": []}},
                request_type="xhr",
            )
            for t in resp.get("tiles", []):
                pos = t.get("position", {})
                x, y = pos.get("x", 0), pos.get("y", 0)
                if (x, y) not in all_tile_data:
                    all_tile_data[(x, y)] = t

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

        ctx.push(
            {
                "type": "phase",
                "phase": "map_scan_done",
                "message": (
                    f"Map scan complete: {len(all_tile_data)} raw tiles, "
                    f"{len(raw_tiles)} with villages/oases"
                ),
            }
        )

        # ── Phase 2: Pre-enrichment filtering ───────────────────────
        own_ids = {v.id for v in session.auth_state.villages}
        before_count = len(raw_tiles)
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

        ctx.push(
            {
                "type": "phase",
                "phase": "pre_filter",
                "message": (
                    f"Pre-filter: {before_count} → {len(tiles)} tiles (removed: {filter_summary})"
                ),
            }
        )

        if not tiles:
            ctx.push(
                {
                    "type": "complete",
                    "tiles": [],
                    "total": 0,
                    "stats": {
                        "raw_tiles": len(all_tile_data),
                        "after_prefilter": 0,
                        "time_seconds": round(time.monotonic() - t_total_start, 1),
                    },
                }
            )
            return

        # ── Phase 3: Enrichment ─────────────────────────────────────
        enrich_total = len(tiles)
        ctx.push(
            {
                "type": "phase",
                "phase": "enriching",
                "message": (
                    f"Enriching {enrich_total} tiles with population, player, tribe data..."
                ),
                "detail": (
                    "Each tile requires an API call (~2-4s with stealth throttling). "
                    f"Estimated time: {enrich_total * 3}–{enrich_total * 5}s"
                ),
            }
        )

        enriched: list[MapTileInfo] = []
        enrich_times: list[float] = []

        for i, tile in enumerate(tiles):
            if ctx.should_stop():
                # User-initiated stop is not an error. Push a phase
                # frame (frontend renders it as an info line) so the user
                # sees WHY their scan ended without a misleading red toast.
                # operation_manager will still mark the terminal status as
                # STOPPED via ctx.should_stop() detection after we return.
                ctx.push(
                    {
                        "type": "phase",
                        "phase": "stopped",
                        "message": "Scan stopped by user",
                    }
                )
                return

            t_enrich_start = time.monotonic()

            eta_str = ""
            if enrich_times:
                avg_t = sum(enrich_times) / len(enrich_times)
                remaining = enrich_total - i
                eta_secs = avg_t * remaining
                eta_m = int(eta_secs // 60)
                eta_s = int(eta_secs % 60)
                eta_str = f"{eta_m}m{eta_s:02d}s"

            ctx.push(
                {
                    "type": "enrich_progress",
                    "index": i + 1,
                    "total": enrich_total,
                    "tile": {"x": tile.x, "y": tile.y, "name": tile.village_name or "?"},
                    "eta": eta_str or None,
                    "message": (
                        f"[{i + 1}/{enrich_total}] Fetching ({tile.x},{tile.y}) "
                        f"{tile.village_name or '?'}"
                        f"{' | ETA: ' + eta_str if eta_str else ''}"
                    ),
                }
            )

            try:
                detail = await svc.get_tile_details(tile.x, tile.y)
                detail.distance = tile.distance
                detail.is_oasis = tile.is_oasis
                detail.is_abandoned = tile.is_abandoned
                if not detail.village_name and tile.village_name:
                    detail.village_name = tile.village_name
                if not detail.player_id and tile.player_id:
                    detail.player_id = tile.player_id
                if not detail.player_name and tile.player_name:
                    detail.player_name = tile.player_name
                if not detail.alliance_id and tile.alliance_id:
                    detail.alliance_id = tile.alliance_id
                if not detail.alliance_name and tile.alliance_name:
                    detail.alliance_name = tile.alliance_name
                enriched.append(detail)

                ctx.push(
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
                            "is_oasis": detail.is_oasis,
                            "bonus": detail.bonus or None,
                        },
                    }
                )

            except ReconStrictViolation:
                # Strict background-account breach — must be fatal, not a
                # per-tile "enrich failed" that the loop walks past. Re-raise
                # so the operation aborts instead of degrading silently.
                raise
            except Exception as e:
                logger.warning("Enrich failed for (%s,%s): %s", tile.x, tile.y, e)
                enriched.append(tile)
                ctx.push(
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
                    }
                )

            enrich_times.append(time.monotonic() - t_enrich_start)

        tiles = enriched
        ctx.push(
            {
                "type": "phase",
                "phase": "enrich_done",
                "message": (
                    f"Enrichment complete: {len(tiles)} tiles enriched "
                    f"({sum(enrich_times):.1f}s total, "
                    f"{(sum(enrich_times) / len(enrich_times)) if enrich_times else 0:.1f}s avg)"
                ),
            }
        )

        # ── Phase 3b: Compute player populations ───────────────────
        visible_pops = _sum_visible_player_pops(tiles)

        # Capital map (player_id → capital village_id). Built whenever
        # we fetch profile pages — required by both max_player_pop
        # (which needs the accurate total) and the non-capitals target
        # type (which needs to identify which villages to drop).
        capital_map: dict[int, int] = {}
        # Per-village occupied-oasis projections collected from the profile
        # pages (only when villages_by_oasis_bonus). Each entry is the dict
        # produced by AutoScoutService._extract_village_oases.
        profile_villages: list[dict] = []

        # Profile fetching is needed when ANY of:
        # - the user wants accurate per-player population (max_player_pop), OR
        # - the user picked the non-capitals target type and we need to
        #   identify capitals to filter them out, OR
        # - the user picked "villages by oasis bonus" and we need each
        #   village's occupiedOases coords (parsed from the same profile HTML).
        # Other modes (plain villages / with-oases / oasis-only) skip
        # the profile-fetch phase entirely, saving ~N HTTP requests
        # per scan where N is the unique-player count (~50–150 for a
        # radius-10 scan).
        want_capital_info = non_capitals
        need_profiles = (
            max_player_pop is not None or want_capital_info or villages_by_oasis_bonus
        ) and bool(visible_pops)

        if need_profiles:
            unique_pids = set(visible_pops.keys())
            ctx.push(
                {
                    "type": "phase",
                    "phase": "player_profiles",
                    "message": (
                        f"Fetching {len(unique_pids)} player profile(s) for accurate population…"
                    ),
                }
            )

            # Per-profile progress callback — fires AFTER every profile.
            # Each fetch already costs a stealth-throttled HTTP request
            # (~1.5-3s typical), so a per-profile push is the strongest
            # WS keepalive we can give: every gap between client-visible
            # frames is bounded by one profile fetch, well under any
            # reasonable proxy / Safari background timeout.
            def _on_profile(done: int, total: int) -> None:
                ctx.push(
                    {
                        "type": "phase",
                        "phase": "profile_progress",
                        "index": done,
                        "total": total,
                        "message": f"Profile fetch: {done}/{total}",
                    }
                )

            profiles = await svc.fetch_player_profiles(unique_pids, progress_cb=_on_profile)
            player_pops = {
                pid: (profiles.get(pid, {}).get("pop") or visible_pops.get(pid, 0))
                for pid in unique_pids
            }
            capital_misses = 0
            for pid, info in profiles.items():
                cap = info.get("capital_id")
                # Be explicit about both Nones and zeros. A capital_id of
                # 0 would otherwise pass the falsy check and silently
                # drop, AND if we accepted it it would match wilderness
                # tiles (village_id=0 sentinel from map scan) and mark
                # them as capitals. Travian village ids are positive in
                # practice; this guard is defensive.
                if cap is not None and isinstance(cap, int) and cap > 0:
                    capital_map[pid] = cap
                elif want_capital_info:
                    capital_misses += 1
                if villages_by_oasis_bonus:
                    vils = info.get("villages")
                    if isinstance(vils, list):
                        profile_villages.extend(vils)
            # Capital parse failures need to be loud — a silent zero
            # means every ★ in the UI is wrong without any error path.
            # Tolerable when the user opted out of capital markers; a
            # red flag when they're on. Push one summary frame instead
            # of N per-profile entries so noisy reproductions stay
            # legible.
            if want_capital_info and capital_misses:
                logger.warning(
                    "Capital parser returned None for %d/%d profiles; "
                    "the ★ column will be incomplete.",
                    capital_misses,
                    len(profiles),
                )
                ctx.push(
                    {
                        "type": "phase",
                        "phase": "capital_parse_warning",
                        "message": (
                            f"Capital marker missing for {capital_misses}/{len(profiles)} "
                            "profiles — Travian's profile layout may have changed."
                        ),
                    }
                )
            pop_source = "profile"
        else:
            player_pops = visible_pops
            pop_source = "visible"

        # ── Populate owner_population on EVERY owned tile ────────────
        # Hard semantic split:
        #   t.population        = the village's own population. Oases have
        #                         no village, so it's ALWAYS 0 for them
        #                         (occupied or not). Min/Max Village Pop
        #                         filters apply only to non-oasis tiles.
        #   t.owner_population  = the player who owns this tile's TOTAL
        #                         population summed across all their
        #                         villages. 0 for unoccupied oases (no
        #                         owner). Max Player Pop filters apply
        #                         to this field for any tile with an owner.
        # This replaces the prior code that overwrote t.population on
        # occupied oases — that conflated the two concepts and made
        # filters unpredictable.
        for t in tiles:
            if t.player_id:
                t.owner_population = player_pops.get(t.player_id, 0)
            else:
                t.owner_population = 0

        # ── Occupied-oasis V.Pop inheritance ────────────────────────
        # Oases have no population of their own. For OCCUPIED oases, the
        # tile-details parser captured the coords of the owning village
        # (oasis_owner_x/y); look that village up among the scanned tiles
        # and copy its `population` so the V.Pop column shows a real
        # number and the village-pop filter applies uniformly. Owner
        # villages outside the scan radius can't be resolved → pop stays
        # 0, which the user-supplied min-pop filter will drop.
        village_pop_by_xy = {(t.x, t.y): t.population for t in tiles if not t.is_oasis}
        for t in tiles:
            if (
                t.is_oasis
                and t.player_id
                and t.oasis_owner_x is not None
                and t.oasis_owner_y is not None
            ):
                t.population = village_pop_by_xy.get((t.oasis_owner_x, t.oasis_owner_y), 0)

        # Mark each tile owned by a known player whose capital we resolved.
        # The match relies on t.village_id (the ``did`` parsed from map
        # JSON) equalling the village ``id`` returned in the profile
        # payload. They ARE the same primary key in Travian's data model
        # — both are the village's database id, distinct from the
        # tile/map cell ``mapId``. Log the resolution rate so a future
        # divergence (e.g. server schema change, a profile field swap)
        # surfaces in :8001/:8002 logs rather than silently emitting an
        # empty ★ column.
        if capital_map:
            matched = 0
            scanned_player_ids = {t.player_id for t in tiles if t.player_id is not None}
            for t in tiles:
                if not t.is_oasis and t.player_id and capital_map.get(t.player_id) == t.village_id:
                    t.is_capital = True
                    matched += 1
            capitals_in_scan = sum(1 for pid in capital_map if pid in scanned_player_ids)
            logger.info(
                "Capital match: resolved=%d, matched_in_scan=%d, "
                "capitals_of_scanned_players=%d (capitals outside scan "
                "radius account for the gap).",
                len(capital_map),
                matched,
                capitals_in_scan,
            )

        if player_pops:
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

            ctx.push(
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
                }
            )

        # ── Village oasis-bonus stamping ────────────────────────────
        # For the "villages by oasis bonus" mode, stamp each in-radius
        # village's aggregated occupied-oasis bonus onto its tile. The bonus
        # is embedded in the owner's profile (already fetched above for
        # capital/population), so this costs ZERO extra requests — no
        # tile-details fetches. Villages outside the scan simply don't match
        # any tile.
        if villages_by_oasis_bonus:
            # village_id → (aggregated breakdown, occupied-oasis count)
            by_vid: dict[int, tuple[dict[str, int], int]] = {
                v["village_id"]: (v.get("breakdown", {}), v.get("oasis_count", 0))
                for v in profile_villages
                if v.get("village_id")
            }
            stamped = 0
            for t in tiles:
                if not t.is_oasis and t.village_id in by_vid:
                    breakdown, count = by_vid[t.village_id]
                    t.village_oasis_breakdown = breakdown
                    t.village_oasis_count = count
                    stamped += 1
            ctx.push(
                {
                    "type": "phase",
                    "phase": "oasis_aggregate",
                    "message": (
                        f"Resolved oasis bonus for {stamped} village(s) "
                        "from player profiles (no extra requests)."
                    ),
                }
            )

        # ── Phase 4: Post-enrichment filtering ──────────────────────
        post_filter_msgs = []

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

        exclude_player_ids: set[int] = set()
        if exclude_player_names:
            name_lower_set = {n.lower() for n in exclude_player_names}
            for t in tiles:
                if t.player_name and t.player_name.lower() in name_lower_set and t.player_id:
                    exclude_player_ids.add(t.player_id)

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

        if oasis_only:
            # Oasis-only mode: drop any tile that isn't an oasis (occupied or
            # unoccupied both stay). Applied AFTER population filters so a
            # max_pop on the oasis owner still works.
            before = len(tiles)
            tiles = [t for t in tiles if t.is_oasis]
            removed = before - len(tiles)
            if removed > 0:
                post_filter_msgs.append(f"Oasis-only: -{removed} villages removed")

        # ── Non-capitals filter ────────────────────────────────────
        # Drop every tile that the profile-fetch phase identified as a
        # player's capital village. Runs only when the user picked
        # "Non-capital villages only" — every other mode skipped the
        # profile-fetch phase, so t.is_capital is unset on all tiles
        # and this filter is a no-op even if accidentally triggered.
        if non_capitals:
            before = len(tiles)
            tiles = [t for t in tiles if not t.is_capital]
            removed = before - len(tiles)
            if removed > 0:
                post_filter_msgs.append(f"Non-capitals: -{removed} capital villages removed")

        # ── Bonus filter ────────────────────────────────────────────
        # Two independent axes that AND together: per-resource minimum
        # percentage (a tile must have ≥N% of every named resource) and
        # total-bucket match (sum of all resource percentages must equal
        # any selected bucket from {25, 50, 75, 100}). Applied only to
        # oasis tiles — villages have no bonus concept and pass through
        # unconditionally so a user with filterMode='with-oases' still
        # sees their villages.
        if bonus_resource_mins or bonus_total_levels:
            before = len(tiles)
            out: list = []
            misses_unparseable = 0
            for t in tiles:
                if not t.is_oasis:
                    out.append(t)
                    continue
                breakdown = t.bonus_breakdown or {}
                if not breakdown:
                    # Oasis with unparseable bonus AND a filter is set:
                    # drop. Count so the warning can fire if many.
                    misses_unparseable += 1
                    continue
                # Per-resource minimums — ALL must hold.
                if any(
                    breakdown.get(res, 0) < min_pct for res, min_pct in bonus_resource_mins.items()
                ):
                    continue
                # Total bucket — if any selected, the total must equal one.
                if bonus_total_levels and sum(breakdown.values()) not in bonus_total_levels:
                    continue
                out.append(t)
            tiles = out
            removed = before - len(tiles)
            if removed > 0:
                parts = []
                if bonus_resource_mins:
                    parts.append(
                        "mins="
                        + ",".join(f"{r}>={p}%" for r, p in sorted(bonus_resource_mins.items()))
                    )
                if bonus_total_levels:
                    parts.append(
                        "totals=" + ",".join(f"{lv}%" for lv in sorted(bonus_total_levels))
                    )
                post_filter_msgs.append(f"Bonus filter ({'; '.join(parts)}): -{removed}")
            if misses_unparseable:
                logger.warning(
                    "Bonus filter dropped %d oasis tile(s) with unparseable bonus "
                    "while a filter was active — Travian locale or HTML may have "
                    "changed.",
                    misses_unparseable,
                )

        # ── Village oasis-bonus filter ──────────────────────────────
        # Same two axes as the oasis bonus filter, but read against a
        # VILLAGE's aggregated occupied-oasis breakdown and with MINIMUM
        # (>=) semantics for the total — a village's summed bonus can exceed
        # 100%, so exact-bucket matching is meaningless here. Only runs when
        # a filter is set; with no filter the mode just surfaces every
        # in-radius village with its aggregated bonus for display.
        if villages_by_oasis_bonus and (bonus_resource_mins or bonus_total_levels):
            min_total = min(bonus_total_levels) if bonus_total_levels else 0
            before = len(tiles)
            out: list = []
            misses_unparseable = 0
            for t in tiles:
                if t.is_oasis:
                    # Defensive: this mode forces show_oases off, so oases
                    # shouldn't reach here, but never emit one if it does.
                    continue
                if t.village_oasis_count == 0:
                    continue  # no oases → can't meet any minimum
                breakdown = t.village_oasis_breakdown or {}
                if not breakdown:
                    # Has oases but none parsed (locale/HTML change) — drop.
                    misses_unparseable += 1
                    continue
                if any(
                    breakdown.get(res, 0) < min_pct for res, min_pct in bonus_resource_mins.items()
                ):
                    continue
                if bonus_total_levels and sum(breakdown.values()) < min_total:
                    continue
                out.append(t)
            tiles = out
            removed = before - len(tiles)
            if removed > 0:
                parts = []
                if bonus_resource_mins:
                    parts.append(
                        "mins="
                        + ",".join(f"{r}>={p}%" for r, p in sorted(bonus_resource_mins.items()))
                    )
                if bonus_total_levels:
                    parts.append(f"total>={min_total}%")
                post_filter_msgs.append(f"Village oasis bonus ({'; '.join(parts)}): -{removed}")
            if misses_unparseable:
                logger.warning(
                    "Village oasis-bonus filter dropped %d village(s) whose oasis "
                    "bonus couldn't be parsed while a filter was active.",
                    misses_unparseable,
                )

        if max_player_pop is not None:
            before = len(tiles)
            removed_players = set()
            filtered = []
            for t in tiles:
                if not t.player_id:
                    # Unoccupied tiles (oases without owner, abandoned
                    # valleys) have no player to gate against — keep them.
                    filtered.append(t)
                    continue
                # Use the tile's own owner_population (populated above
                # from profile fetch or visible-village sum). Fall back
                # to player_pops dict for safety in case the field
                # wasn't set, but the assignment loop covers all cases.
                ppop = t.owner_population or player_pops.get(t.player_id, 0)
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

        if len(tiles) > limit:
            tiles = tiles[:limit]
            post_filter_msgs.append(f"Capped at limit={limit}")

        filter_detail = (
            "; ".join(post_filter_msgs) if post_filter_msgs else "no additional filtering needed"
        )
        ctx.push(
            {
                "type": "phase",
                "phase": "post_filter",
                "message": (f"Post-filter: {len(tiles)} targets remaining ({filter_detail})"),
            }
        )

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
                # `population` is the village's own pop for villages and
                # the owner-village's pop for occupied oases (set above).
                # Unoccupied oases stay at 0 because they have no owner.
                "population": t.population,
                # `owner_population` is the player's TOTAL pop. 0 for
                # unoccupied. The frontend renders these as separate
                # columns (V.Pop and Player Pop).
                "owner_population": t.owner_population,
                "distance": t.distance,
                "is_oasis": t.is_oasis,
                "is_abandoned": t.is_abandoned,
                "is_capital": t.is_capital,
                # Oasis bonus string (e.g. "25% Clay" or "25% Iron, 25% Crop").
                # Empty for non-oasis tiles. Frontend renders it in the
                # dedicated Bonus column.
                "bonus": t.bonus,
                # Canonical breakdown — locale-stable. Empty {} on
                # non-oasis tiles AND on oases the parser couldn't
                # classify. Frontend uses this to compute bonus_total
                # for sorting and to render compact chips if desired.
                "bonus_breakdown": t.bonus_breakdown,
                # Village-mode aggregate: the summed bonus of all oases this
                # VILLAGE occupies, the contributing-oasis count, and a
                # human-readable string. Empty/0 outside villages-by-oasis
                # mode. Frontend renders village_oasis_bonus in the Bonus
                # column and sorts on village_oasis_breakdown in this mode.
                "village_oasis_breakdown": t.village_oasis_breakdown,
                "village_oasis_count": t.village_oasis_count,
                "village_oasis_bonus": _format_bonus_breakdown(t.village_oasis_breakdown),
            }
            for t in tiles
        ]

        ctx.push(
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
            }
        )

    return coro


async def _receive_start_config(websocket: WebSocket) -> dict | None:
    """Wait for the client's first message and extract the op config.

    Accepts both the legacy bare-config shape (``{"radius": 15, ...}``)
    and the resumable-hook shape (``{"action": "start", "config": {...}}``)
    so the same FE code paths work whether they were written before or
    after the OperationManager refactor.
    Returns the config dict, or None if the WS died / payload was bad
    (in which case the WS has already been closed with an error reason).
    """
    try:
        raw = await websocket.receive_text()
    except (WebSocketDisconnect, RuntimeError):
        return None

    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        await websocket.send_json(
            {"type": "error", "message": "Invalid JSON config", "fatal": True}
        )
        await websocket.close(code=4002)
        return None

    if not isinstance(msg, dict):
        await websocket.send_json(
            {"type": "error", "message": "Config must be an object", "fatal": True}
        )
        await websocket.close(code=4002)
        return None

    if msg.get("action") == "start" and isinstance(msg.get("config"), dict):
        return msg["config"]
    return msg


# ---------------------------------------------------------------------------
# WebSocket endpoints — thin wrappers around OperationManager
# ---------------------------------------------------------------------------


@router.websocket("/ws/scout/auto")
async def auto_scout_ws(websocket: WebSocket) -> None:
    """Auto-scout WS: spawn detached op, tail its session stream."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    op_label = "scout"
    if op_label in active_ops.get_active(user_id):
        existing = next(
            (op for op in operation_manager.list_for_user(user_id) if op.label == op_label),
            None,
        )
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "An auto-scout operation is already running for this account",
            }
        )
        await websocket.close(code=4009, reason="Auto-scout already running")
        return

    await websocket.accept()

    config = await _receive_start_config(websocket)
    if config is None:
        return

    op = operation_manager.start(
        user_id=user_id,
        label=op_label,
        session_type="scout-auto",
        session_label="Auto Scout",
        session=session,
        coro=_build_auto_scout_coro(config),
        require_unique_label=True,
    )
    if op is None:
        existing = operation_manager.find_by_label(user_id, op_label)
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "An auto-scout operation is already running for this account",
            }
        )
        await websocket.close(code=4009, reason="Auto-scout already running")
        return

    await subscribe_and_tail(websocket, user_id, CHANNEL, op.session_id)


@router.websocket("/ws/scout/scan")
async def scout_scan_ws(websocket: WebSocket) -> None:
    """Map scan WS: spawn detached scan op, tail its session stream."""

    user_id = await ws_manager.authenticate(websocket)
    if user_id is None:
        return

    session: Optional[TravianSession] = session_manager.get(user_id)
    if session is None or session.auth_state is None:
        await websocket.close(code=4003, reason="No active Travian session")
        return

    scan_op_label = "scout-scan"
    if scan_op_label in active_ops.get_active(user_id):
        existing = next(
            (op for op in operation_manager.list_for_user(user_id) if op.label == scan_op_label),
            None,
        )
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "A map scan is already running for this account",
            }
        )
        await websocket.close(code=4009, reason="Map scan already running")
        return

    await websocket.accept()

    config = await _receive_start_config(websocket)
    if config is None:
        return

    op = operation_manager.start(
        user_id=user_id,
        label=scan_op_label,
        session_type="scout-scan",
        session_label="Map Scan",
        session=session,
        coro=_build_scout_scan_coro(config),
        require_unique_label=True,
    )
    if op is None:
        existing = operation_manager.find_by_label(user_id, scan_op_label)
        await websocket.send_json(
            {
                "type": "already_running",
                "session_id": existing.session_id if existing else None,
                "message": "A map scan is already running for this account",
            }
        )
        await websocket.close(code=4009, reason="Map scan already running")
        return

    await subscribe_and_tail(websocket, user_id, SCAN_CHANNEL, op.session_id)
