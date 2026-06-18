"""REST + WebSocket routes for remote execution session mirroring.

REST:
    GET  /api/sessions          — list the current user's execution sessions
    POST /api/sessions/stop-all — signal all active operations to stop

WebSocket:
    WS /ws/sessions/{session_id}/stream?token=<JWT> — subscribe to a session's
    live output (sends history on connect, then streams new messages).
    Also accepts ``{"action": "stop"}`` from the client to halt the
    underlying operation, enabling cross-device control.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect

from travian_api.operation_manager import operation_manager
from travian_api.web.auth import get_current_user
from travian_api.web.execution_sessions import exec_session_manager
from travian_api.web.operation_gate import active_ops, captcha_stop
from travian_api.web.sessions import get_travian_session
from travian_api.web.ws.manager import ws_manager

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# REST: list sessions
# ---------------------------------------------------------------------------


@router.get("/api/sessions")
async def list_sessions(user=Depends(get_current_user)):
    """Return the caller's execution sessions (running + disconnected <24h)."""
    return exec_session_manager.list_for_user(user.id)


@router.get("/api/__diag/sessions")
async def diag_sessions(user=Depends(get_current_user)):
    """Diagnostic snapshot of this user's execution sessions.

    Returns subscriber counts, message-count, status, etc. — meant to be
    cross-referenced against the client-side `__resumableDebug.history()`
    when the page appears to hang. Read-only, no side effects.
    """
    sessions = exec_session_manager.list_for_user(user.id)
    enriched = []
    for s in sessions:
        sess = exec_session_manager.get(s["id"])
        sub_count = len(sess._subscribers) if sess is not None else 0
        last_msg_ts = None
        last_msg_type = None
        if sess is not None and sess.messages:
            last = sess.messages[-1]
            if isinstance(last, dict):
                last_msg_ts = last.get("ts")
                last_msg_type = last.get("type")
        enriched.append(
            {
                **s,
                "subscriber_count": sub_count,
                "last_message_ts": last_msg_ts,
                "last_message_type": last_msg_type,
            }
        )
    return {"sessions": enriched, "user_id": user.id}


@router.get("/api/__diag/profile-parse")
async def diag_profile_parse(
    player_id: int | None = None,
    name: str | None = None,
    user=Depends(get_current_user),
    tsess=Depends(get_travian_session),
):
    """Fetch /profile/{player_id} via the caller's authenticated session
    and report exactly what the capital-id parser sees.

    Accepts either ``?player_id=N`` directly OR ``?name=<player_name>``;
    when a name is given, the most recent scout-scan session's complete
    payload is searched for a matching tile (case-insensitive) to find
    the player_id.

    Read-only — temporary diagnostic for hunting capital-detection bugs.
    """
    if player_id is None and name:
        target = name.strip().lower()
        sessions = exec_session_manager.list_for_user(user.id)
        scout_sessions = [s for s in sessions if s["session_type"] == "scout-scan"]
        scout_sessions.sort(key=lambda s: -s["created_at"])
        for s in scout_sessions:
            sess = exec_session_manager.get(s["id"])
            if sess is None:
                continue
            for msg in reversed(list(sess.messages)):
                if not isinstance(msg, dict) or msg.get("type") != "complete":
                    continue
                for t in msg.get("tiles", []):
                    pname = t.get("player_name") or ""
                    if pname.strip().lower() == target:
                        player_id = t.get("player_id")
                        break
                if player_id:
                    break
            if player_id:
                break
        if player_id is None:
            raise HTTPException(
                status_code=404,
                detail=f"No tile with player_name={name!r} found in your recent scout-scan sessions.",
            )
    if player_id is None:
        raise HTTPException(
            status_code=400,
            detail="Provide ?player_id=N or ?name=<player_name>",
        )

    try:
        html = await tsess.http_client.get_html(f"/profile/{player_id}")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Profile fetch failed: {exc}")

    result: dict = {
        "player_id": player_id,
        "html_length": len(html),
    }

    pop_m = re.search(r'"ranks"\s*:\s*\{[^}]*"population"\s*:\s*(\d+)', html)
    result["population"] = int(pop_m.group(1)) if pop_m else None

    json_matches: list[dict] = []
    for obj in re.finditer(r"\{[^{}]{0,800}\}", html):
        chunk = obj.group(0)
        has_main = '"isMainVillage"' in chunk
        has_cap = '"isCapital"' in chunk
        if not (has_main or has_cap):
            continue
        marker_true = bool(re.search(r'"(?:isMainVillage|isCapital)"\s*:\s*true', chunk))
        id_match = re.search(r'"id"\s*:\s*(\d+)', chunk)
        json_matches.append(
            {
                "snippet": chunk[:400],
                "has_isMainVillage_key": has_main,
                "has_isCapital_key": has_cap,
                "marker_true": marker_true,
                "id_found": int(id_match.group(1)) if id_match else None,
            }
        )
    result["json_chunks_with_marker_key"] = json_matches[:10]

    html_fallback: list[dict] = []
    for pat_name, pat in (
        (
            "link_before_marker",
            r"<a[^>]*newdid=(\d+)[^>]*>.{0,120}?(?:capital|hauptdorf|stolica|kapital)",
        ),
        (
            "marker_before_link",
            r"(?:capital|hauptdorf|stolica|kapital)[^<>]{0,120}?<a[^>]*newdid=(\d+)",
        ),
    ):
        m = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        html_fallback.append(
            {
                "pattern": pat_name,
                "matched_id": int(m.group(1)) if m else None,
                "matched_excerpt": m.group(0)[:300] if m else None,
            }
        )
    result["html_fallback"] = html_fallback

    keyword_counts: dict[str, int] = {}
    for kw in (
        "isMainVillage",
        "isCapital",
        "mainVillage",
        "isMainVil",
        "capital",
        "Hauptdorf",
        "stolica",
        "Kapital",
        "newdid",
        "is_main_village",
        '"main"',
        "MainVillage",
    ):
        keyword_counts[kw] = html.lower().count(kw.lower())
    result["keyword_counts"] = keyword_counts

    excerpts: dict[str, str] = {}
    for kw in (
        "isMainVillage",
        "isCapital",
        "MainVillage",
        "capital",
        "Capital",
        "crown",
        "wonder",
        "&#x2605;",
        "&#9733;",
        "mainVillage",
        "main-village",
    ):
        idx = html.find(kw)
        if idx >= 0:
            excerpts[kw] = html[max(0, idx - 250) : idx + 500]
    result["excerpts"] = excerpts

    newdid_contexts: list[dict] = []
    for m in re.finditer(r"newdid=(\d+)", html):
        start = max(0, m.start() - 400)
        end = min(len(html), m.end() + 400)
        newdid_contexts.append(
            {
                "newdid": int(m.group(1)),
                "context": html[start:end],
            }
        )
    result["newdid_contexts"] = newdid_contexts

    villages_pat = re.search(
        r'<table[^>]*class="[^"]*villages[^"]*"[^>]*>(.*?)</table>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    if villages_pat:
        result["villages_table_excerpt"] = villages_pat.group(0)[:3000]

    class_matches: list[str] = []
    for m in re.finditer(
        r'class="([^"]*(?:capital|main|wonder|crown|hero)[^"]*)"',
        html,
        re.IGNORECASE,
    ):
        cls = m.group(1)
        if cls not in class_matches:
            class_matches.append(cls)
    result["relevant_classes"] = class_matches[:20]

    from travian_api.services.auto_scout_service import AutoScoutService

    svc = AutoScoutService(tsess.http_client)
    parsed = await svc.get_player_profile_info(player_id)
    result["parser_output"] = parsed

    return result


@router.post("/api/sessions/stop-all")
async def stop_all_sessions(user=Depends(get_current_user)):
    """Signal all active operations for this user to stop gracefully."""
    captcha_stop.signal(user.id)
    active = active_ops.get_active(user.id)
    logger.info("Stop-all requested by user %s, active ops: %s", user.id, active)
    return {"stopped": True, "active_operations": active}


@router.post("/api/sessions/{session_id}/stop")
async def stop_one_session(session_id: str, user=Depends(get_current_user)):
    """Signal a single operation to stop gracefully.

    The bulk ``/stop-all`` is appropriate for "kill switch" UX. This
    endpoint lets the per-session detail view target one op without
    collateral. Returns 404 when the session doesn't exist or doesn't
    belong to the caller.
    """
    session = exec_session_manager.get(session_id)
    if session is None or session.user_id != user.id:
        # FastAPI does not honor Flask-style ``return body, status_code``
        # tuples — raise HTTPException so the client actually sees 404.
        raise HTTPException(status_code=404, detail="Session not found")
    ok = operation_manager.request_stop(session_id)
    logger.info(
        "Stop requested for session %s by user %s (op_found=%s)",
        session_id,
        user.id,
        ok,
    )
    return {"stopped": True, "op_running": ok, "session_id": session_id}


# ---------------------------------------------------------------------------
# WebSocket: stream session output
# ---------------------------------------------------------------------------


@router.websocket("/ws/sessions/{session_id}/stream")
async def session_stream_ws(websocket: WebSocket, session_id: str):
    """Subscribe to an execution session's message stream.

    Protocol:
        1. Authenticate via JWT query param.
        2. Verify session exists and belongs to the calling user.
        3. Send session_meta + full history.
        4. If session is already disconnected, send session_ended and close.
        5. Otherwise stream live messages until the session ends or the
           client disconnects.
    """
    user_id = await ws_manager.authenticate(websocket, require_travian_session=False)
    if user_id is None:
        return

    session = exec_session_manager.get(session_id)
    if session is None:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Session not found or expired"})
        await websocket.close(code=4004)
        return

    if session.user_id != user_id:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "Access denied"})
        await websocket.close(code=4003)
        return

    channel = f"session_stream_{session_id}"
    await ws_manager.connect(websocket, user_id, channel)

    sub_id = id(websocket)
    result = exec_session_manager.subscribe(session_id, sub_id)
    if result is None:
        logger.warning(
            "session_stream subscriber: subscribe() returned None — session vanished. "
            "user=%s session=%s",
            user_id,
            session_id,
        )
        await websocket.send_json({"type": "error", "message": "Session not found"})
        await ws_manager.disconnect(user_id, channel, websocket)
        return

    history, queue = result
    logger.info(
        "session_stream subscriber attached: user=%s session=%s sub_id=%s "
        "session.status=%s history_len=%d",
        user_id,
        session_id,
        sub_id,
        session.status,
        len(history),
    )

    stop_listener: asyncio.Task | None = None
    sent_count = 0

    try:
        # Send metadata
        await websocket.send_json(
            {
                "type": "session_meta",
                "id": session.id,
                "session_type": session.session_type,
                "label": session.label,
                "status": session.status,
                "created_at": session.created_at,
            }
        )
        sent_count += 1

        # Send full history
        await websocket.send_json({"type": "history", "messages": history})
        sent_count += 1

        # If session already ended, tell the client and close gracefully
        if session.status == "disconnected":
            await websocket.send_json({"type": "session_ended"})
            sent_count += 1
            logger.info(
                "session_stream subscriber: session already disconnected, sent "
                "session_ended and closing. user=%s session=%s sub_id=%s sent=%d",
                user_id,
                session_id,
                sub_id,
                sent_count,
            )
            return

        # Cross-device stop: a client connected to the stream can ask the
        # underlying operation to halt without touching the original WS.
        stop_listener = asyncio.create_task(
            _listen_for_stop(websocket, session_id),
            name=f"session-stream-stop:{session_id}",
        )

        # Stream live messages until session ends or client disconnects
        while True:
            data = await queue.get()
            if data is None:
                # None sentinel = session ended
                await websocket.send_json({"type": "session_ended"})
                sent_count += 1
                logger.info(
                    "session_stream subscriber: None sentinel received, sent "
                    "session_ended. session=%s sub_id=%s sent=%d",
                    session_id,
                    sub_id,
                    sent_count,
                )
                break
            await websocket.send_json({"type": "message", "data": data})
            sent_count += 1

    except WebSocketDisconnect:
        logger.info(
            "session_stream subscriber: WebSocketDisconnect. session=%s sub_id=%s sent=%d",
            session_id,
            sub_id,
            sent_count,
        )
    except Exception:
        logger.exception(
            "Error in session stream WS: session=%s user=%s sub_id=%s sent=%d",
            session_id,
            user_id,
            sub_id,
            sent_count,
        )
    finally:
        if stop_listener is not None:
            stop_listener.cancel()
            try:
                await stop_listener
            except (asyncio.CancelledError, Exception):
                pass
        exec_session_manager.unsubscribe(session_id, sub_id)
        await ws_manager.disconnect(user_id, channel, websocket)
        logger.info(
            "session_stream subscriber detached. session=%s sub_id=%s total_sent=%d",
            session_id,
            sub_id,
            sent_count,
        )


async def _listen_for_stop(websocket: WebSocket, session_id: str) -> None:
    """Background reader: forward ``{"action": "stop"}`` to the running op.

    The session-stream WS is read-only by default; this listener gives
    subscribers an opt-in way to halt the underlying operation. Unknown
    payloads are ignored so a noisy client can't tear the listener down.
    """
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(msg, dict) and msg.get("action") == "stop":
                operation_manager.request_stop(session_id)
                # Don't return — leave the listener alive so a misbehaving
                # client that sends stop multiple times is harmless.
    except (WebSocketDisconnect, RuntimeError):
        return
