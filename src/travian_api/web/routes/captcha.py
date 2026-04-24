"""Captcha/bot-detection status and resolution endpoints."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends

from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User
from travian_api.web.sessions import session_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/captcha", tags=["captcha"])


@router.get("/status")
async def captcha_status(user: User = Depends(get_current_user)):
    """Return the current captcha guard state for this user.

    Uses ``get_current_user`` (not ``get_travian_session``) to avoid
    triggering auto-reconnect — we just need to peek at state.
    """
    session = session_manager.get(user.id)
    if session is None:
        return {"active": False, "triggered_at": None, "pattern": None}
    return session.http_client.captcha_guard.status


@router.post("/resolve")
async def captcha_resolve(user: User = Depends(get_current_user)):
    """User signals they have resolved the captcha manually.

    Lifts the guard — all blocked HTTP requests resume.
    """
    session = session_manager.get(user.id)
    if session is None:
        return {"status": "ok", "message": "No active session"}

    guard = session.http_client.captcha_guard
    if not guard.is_blocked:
        return {"status": "ok", "message": "No active captcha block"}

    guard.resolve()

    from travian_api.web.log_broadcast import log_stream_manager
    from travian_api.web.operation_gate import captcha_stop
    from travian_api.web.ws.manager import ws_manager

    # Signal all active operations to stop so they don't auto-resume
    # after being unblocked by captcha_guard.resolve().
    captcha_stop.signal(user.id)

    await ws_manager.broadcast_to_user(
        user.id,
        {
            "type": "captcha_resolved",
            "active": False,
            "operations_stopped": True,
        },
    )

    log_stream_manager.push(
        {
            "timestamp": time.time(),
            "level": "info",
            "source": "stealth",
            "message": "Captcha block resolved by user. Active operations signalled to stop.",
            "user_id": user.id,
        }
    )

    return {
        "status": "ok",
        "message": "Captcha block lifted. Active operations stopped — restart them manually.",
    }
