"""Per-user Travian session manager.

Each logged-in web UI user who connects to a Travian server gets a completely
independent set of service instances (Settings, HttpClient, AuthService, etc.)
so that cookie jars, JWT tokens, and stealth state never leak between users.
"""

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.models.auth import AuthState
from travian_api.services.auth_service import AuthService
from travian_api.services.auto_scout_service import AutoScoutService
from travian_api.services.build_queue_service import BuildQueueService
from travian_api.services.building_service import BuildingService
from travian_api.services.farm_list_service import FarmListService
from travian_api.services.military_service import MilitaryService
from travian_api.services.reports_service import ReportsService
from travian_api.services.target_resolver import TargetResolver
from travian_api.services.video_reward_service import VideoRewardService
from travian_api.services.raid_analyzer_service import RaidAnalyzerService
from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User, get_db

logger = logging.getLogger(__name__)

# Directory for per-session cookie/JWT files so users never collide.
_SESSION_DATA_DIR = Path(tempfile.gettempdir()) / "travian_web_sessions"


class TravianSession:
    """Holds isolated service instances for one user's Travian connection."""

    def __init__(self, user_id: int, server_url: str, username: str, password: str):
        self.user_id = user_id
        self.server_url = server_url

        # ── Per-user data directory ───────────────────────────────────
        self._data_dir = _SESSION_DATA_DIR / str(user_id)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self._data_dir, 0o700)
        except OSError:
            pass  # Windows ACL may not support chmod

        # ── Isolated Settings ─────────────────────────────────────────
        # Start from a default Settings (reads .env for stealth config) then
        # FORCE override all identity fields so .env credentials never leak
        # between users.
        base_settings = Settings()
        self.settings: Settings = base_settings.model_copy(update={
            "base_url": server_url.rstrip("/"),
            "username": username,
            "password": password,
            # Isolate JWT + cookie cache per user so files never collide
            "jwt_cache_file": str(self._data_dir / "jwt_cache.json"),
            "jwt_cache_path": str(self._data_dir / "jwt_cache.json"),
        })

        # ── Isolated HTTP client ──────────────────────────────────────
        self.http_client = HttpClient(self.settings)

        # CRITICAL: Cookie/session isolation for multi-user safety.
        # HttpClient.__init__ loads cookies from the global .travian_cookies.json
        # which may contain another user's session. We must:
        # 1. Clear any cookies loaded from the global file
        # 2. Set the per-user cookie file path
        # 3. Load cookies from the per-user file instead
        self.http_client.clear_cookies()
        self.http_client._cookie_file = self._data_dir / "cookies.json"
        self.http_client._load_cookies()  # load from per-user file

        # ── Captcha guard notification callback ─────────────────────
        self.http_client.captcha_guard.set_trigger_callback(
            self._on_captcha_triggered
        )

        # ── Services (all share the same isolated http_client) ────────
        self.auth_service = AuthService(self.http_client, self.settings)
        self.building_service = BuildingService(self.http_client)
        self.target_resolver = TargetResolver(self.http_client)
        self.military_service = MilitaryService(self.http_client, self.target_resolver)
        self.build_queue_service = BuildQueueService(self.http_client)
        self.reports_service = ReportsService(self.http_client)
        self.farm_service = FarmListService(self.http_client)
        self.scout_service = AutoScoutService(self.http_client)
        self.video_service = VideoRewardService(self.http_client)

        # ── Lazy services (need auth_state) ──────────────────────────
        self._raid_analyzer: Optional[RaidAnalyzerService] = None

        # ── State ─────────────────────────────────────────────────────
        self.auth_state: Optional[AuthState] = None
        self.active_village_id: Optional[int] = None
        self.player_name: Optional[str] = None
        self.tribe_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Captcha guard
    # ------------------------------------------------------------------

    async def _on_captcha_triggered(
        self,
        pattern: str,
        *,
        url: str = "",
        status_code: int = 0,
        response_snippet: str = "",
    ) -> None:
        """Broadcast captcha alert to all user's WS connections + log stream."""
        import time as _time
        from travian_api.web.ws.manager import ws_manager
        from travian_api.web.log_broadcast import log_stream_manager

        await ws_manager.broadcast_to_user(self.user_id, {
            "type": "captcha_alert",
            "active": True,
            "pattern": pattern,
            "triggered_at": _time.time(),
            "url": url,
            "status_code": status_code,
            "response_snippet": response_snippet,
            "message": f"Bot detection triggered ({pattern}). All operations paused.",
        })

        log_stream_manager.push({
            "timestamp": _time.time(),
            "level": "error",
            "source": "stealth",
            "message": (
                f"CAPTCHA/BOT DETECTION: '{pattern}' | url={url} | "
                f"status={status_code} | All operations halted until resolved."
            ),
            "detail": response_snippet or None,
            "user_id": self.user_id,
        })

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> AuthState:
        """Authenticate to the Travian server and populate state."""
        self.auth_state = await self.auth_service.login()
        self.active_village_id = self.auth_state.village_id
        self.player_name = self.auth_state.player_name
        self.tribe_id = self.auth_state.tribe_id
        self._raid_analyzer = RaidAnalyzerService(self.http_client, self.auth_state)
        return self.auth_state

    @property
    def raid_analyzer(self) -> RaidAnalyzerService:
        if self._raid_analyzer is None:
            raise RuntimeError("Not connected — raid analyzer unavailable")
        return self._raid_analyzer

    async def disconnect(self) -> None:
        """Clean up HTTP client and release resources."""
        try:
            await self.http_client.close()
        except Exception as e:
            logger.warning("Error closing HTTP client for user %s: %s", self.user_id, e)

    # ------------------------------------------------------------------
    # Village helpers
    # ------------------------------------------------------------------

    def switch_village(self, village_id: int) -> None:
        """Switch the active village context.

        Raises ``ValueError`` if *village_id* doesn't belong to this player.
        """
        if self.auth_state:
            for v in self.auth_state.villages:
                if v.id == village_id:
                    self.active_village_id = village_id
                    return
        raise ValueError(f"Village {village_id} not found for this player")


class SessionManager:
    """Manages all active Travian sessions, keyed by ``user_id``."""

    def __init__(self) -> None:
        self._sessions: dict[int, TravianSession] = {}
        self._lock = asyncio.Lock()
        self._reconnect_locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def connect(
        self,
        user_id: int,
        server_url: str,
        username: str,
        password: str,
    ) -> TravianSession:
        """Create a new session and authenticate to Travian.

        If the user already has an active session it is disconnected first.
        """
        await self.disconnect(user_id)

        session = TravianSession(user_id, server_url, username, password)
        await session.connect()

        async with self._lock:
            self._sessions[user_id] = session

        logger.info(
            "User %s connected to %s as %s",
            user_id,
            server_url,
            session.player_name,
        )
        return session

    async def disconnect(self, user_id: int) -> None:
        """Disconnect and remove a user's session (no-op if none exists)."""
        async with self._lock:
            session = self._sessions.pop(user_id, None)
        if session:
            await session.disconnect()
            logger.info("User %s disconnected from %s", user_id, session.server_url)

    def get(self, user_id: int) -> Optional[TravianSession]:
        """Return an active session for *user_id*, or ``None``."""
        return self._sessions.get(user_id)

    def get_reconnect_lock(self, user_id: int) -> asyncio.Lock:
        """Return a per-user lock for serializing auto-reconnect attempts."""
        if user_id not in self._reconnect_locks:
            self._reconnect_locks[user_id] = asyncio.Lock()
        return self._reconnect_locks[user_id]

    async def disconnect_all(self) -> None:
        """Disconnect every session (call during application shutdown)."""
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.disconnect()


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

session_manager = SessionManager()


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_travian_session(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TravianSession:
    """FastAPI dependency returning the caller's active Travian session.

    If no session exists but the user has saved credentials, attempts to
    auto-reconnect using the most recently connected saved credential.
    Raises HTTP 403 only if reconnection fails or no credentials are saved.
    """
    session = session_manager.get(user.id)
    if session is not None:
        return session

    # Serialize reconnect attempts per user — prevents two concurrent requests
    # from both triggering session_manager.connect() simultaneously.
    reconnect_lock = session_manager.get_reconnect_lock(user.id)
    async with reconnect_lock:
        # Re-check after acquiring lock — another request may have reconnected
        session = session_manager.get(user.id)
        if session is not None:
            return session

        # Try auto-reconnect from saved credentials
        from sqlalchemy import select
        from travian_api.web.models.db import TravianCredential
        from travian_api.web.auth import decrypt_credential

        result = await db.execute(
            select(TravianCredential)
            .where(TravianCredential.user_id == user.id)
            .order_by(TravianCredential.last_connected.desc().nulls_last())
            .limit(1)
        )
        cred = result.scalar_one_or_none()

        if cred is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not connected to a Travian server. Use POST /api/travian/connect first.",
            )

        try:
            password = decrypt_credential(cred.encrypted_password)
            session = await session_manager.connect(
                user_id=user.id,
                server_url=cred.server_url,
                username=cred.travian_username,
                password=password,
            )
            from datetime import datetime, timezone
            cred.last_connected = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Auto-reconnected user %s to %s", user.id, cred.server_url)
            return session
        except Exception as exc:
            logger.warning("Auto-reconnect failed for user %s: %s", user.id, exc)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Auto-reconnect failed: {exc}. Please reconnect manually.",
            )
