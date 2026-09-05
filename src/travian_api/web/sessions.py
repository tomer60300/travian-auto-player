"""Per-user Travian session manager.

Each logged-in web UI user who connects to a Travian server gets a completely
independent set of service instances (Settings, HttpClient, AuthService, etc.)
so that cookie jars, JWT tokens, and stealth state never leak between users.
"""

import asyncio
import hashlib
import logging
import os
import tempfile
import time
from datetime import UTC
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
from travian_api.services.raid_analyzer_service import RaidAnalyzerService
from travian_api.services.reports_service import ReportsService
from travian_api.services.target_resolver import TargetResolver
from travian_api.services.trade_route_service import TradeRouteService
from travian_api.services.video_reward_service import VideoRewardService
from travian_api.web.auth import get_current_user
from travian_api.web.models.db import User, get_db
from travian_api.web.url_guard import ensure_safe_server_url

logger = logging.getLogger(__name__)

# Directory for per-session cookie/JWT files so users never collide.
_SESSION_DATA_DIR = Path(tempfile.gettempdir()) / "travian_web_sessions"


class TravianSession:
    """Holds isolated service instances for one user's Travian connection."""

    def __init__(self, user_id: int, server_url: str, username: str, password: str):
        self.user_id = user_id
        self.server_url = server_url

        # ── Per-identity data directory ───────────────────────────────
        # Keyed by (server, Travian username), not just the web user: the JWT
        # and cookie caches resume sessions, so a web user who reconnects to
        # the same world with different credentials must not come up as the
        # previous player. Same identity keeps its cache — stealth resume of
        # a still-valid session stays cheap.
        identity = hashlib.sha256(f"{server_url.rstrip('/')}|{username}".encode()).hexdigest()[:16]
        self._data_dir = _SESSION_DATA_DIR / str(user_id) / identity
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
        self.settings: Settings = base_settings.model_copy(
            update={
                "base_url": server_url.rstrip("/"),
                "username": username,
                "password": password,
                # Isolate JWT + cookie cache per user so files never collide
                "jwt_cache_file": str(self._data_dir / "jwt_cache.json"),
                "jwt_cache_path": str(self._data_dir / "jwt_cache.json"),
            }
        )

        # ── Isolated HTTP client ──────────────────────────────────────
        # Pass per-user cookie path so persona/scheduler files land in the
        # per-user directory from the start (instead of the working dir).
        self._cookie_file = self._data_dir / "cookies.json"
        self.http_client = HttpClient(self.settings, cookie_file=self._cookie_file)

        # ── Captcha guard notification callback ─────────────────────
        self.http_client.captcha_guard.set_trigger_callback(self._on_captcha_triggered)

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
        # Live creation is ON unless the operator turns it off:
        # `trade_route_live` has defaulted True since 2026-08-27, because the
        # opt-in reverted to preview-only on every server restart. The payload
        # is verified and /execute has its own confirm-and-verify machinery;
        # `TRAVIAN_TRADE_ROUTE_LIVE=false` is the emergency preview-only mode.
        # `TradeRouteService`'s own default is still off -- that is the
        # library's safe default, and this is the one place that overrides it.
        self.trade_route_service = TradeRouteService(
            self.http_client, live_enabled=self.settings.trade_route_live
        )

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

        from travian_api.web.log_broadcast import log_stream_manager
        from travian_api.web.ws.manager import ws_manager

        await ws_manager.broadcast_to_user(
            self.user_id,
            {
                "type": "captcha_alert",
                "active": True,
                "pattern": pattern,
                "triggered_at": _time.time(),
                "url": url,
                "status_code": status_code,
                "response_snippet": response_snippet,
                "message": f"Bot detection triggered ({pattern}). All operations paused.",
            },
        )

        log_stream_manager.push(
            {
                "timestamp": _time.time(),
                "level": "error",
                "source": "stealth",
                "message": (
                    f"CAPTCHA/BOT DETECTION: '{pattern}' | url={url} | "
                    f"status={status_code} | All operations halted until resolved."
                ),
                "detail": response_snippet or None,
                "user_id": self.user_id,
            }
        )

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
        # Users who explicitly disconnected. Auto-restore exists for crashes
        # and restarts, not to override the user: the flag suppresses it until
        # an explicit connect. In-memory on purpose — a process restart clears
        # it, which is exactly when seamless recovery is wanted.
        self._explicit_disconnects: set[int] = set()

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

        Serialized on the per-user reconnect lock so a logout that returns
        while this login is in flight cannot be undone when the new session
        installs. Auto-reconnect paths that already hold that lock call
        :meth:`_connect_locked` directly -- the URL guard lives there so those
        paths are validated too.
        """
        async with self.get_reconnect_lock(user_id):
            return await self._connect_locked(user_id, server_url, username, password)

    async def _connect_locked(
        self,
        user_id: int,
        server_url: str,
        username: str,
        password: str,
    ) -> TravianSession:
        """Login + install; the caller holds the per-user reconnect lock.

        Any existing session is replaced only AFTER the new login succeeds, so
        a reconnect that hits a transient Travian failure (downtime, captcha)
        cannot destroy a still-working session.

        Refused while background operations are running on the session being
        replaced: those jobs hold the old session's HttpClient, and closing it
        under them makes every following request in the job fail mid-run. The
        pre-login check avoids wasting a real login in the common case; the
        re-check under the install lock closes the race with jobs that start
        while the login is in flight.

        The SSRF guard runs here, the single choke point every path funnels
        through -- foreground connect AND the auto-restore paths that replay a
        saved URL. A saved host whose DNS has since moved to a private address,
        or a legacy row from before the guard existed, is caught at connection
        time rather than trusted because it was once accepted.
        """
        await ensure_safe_server_url(server_url)
        if user_id in self._sessions:
            running = self._running_operation_labels(user_id)
            if running:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Cannot reconnect while operations are running: "
                        f"{', '.join(running)}. Stop them first."
                    ),
                )

        session = TravianSession(user_id, server_url, username, password)
        try:
            await session.connect()
        except BaseException:
            # Bad credentials, a network error or a captcha leaves a session
            # that was never installed anywhere -- but its httpx/curl clients
            # and file-backed state already exist. Abandoning it leaks a
            # connection pool per failed attempt, and reconnect retries make
            # that a steady drip. Close it before letting the error out.
            try:
                await session.disconnect()
            except Exception:
                logger.exception("Cleanup after a failed connect also failed")
            raise

        # Re-check under the install lock: an operation may have started while
        # the login was in flight, and swapping now would close the HttpClient
        # it just captured. No awaits sit between this check and the swap, so
        # nothing can slip in between.
        old: Optional[TravianSession] = None
        conflict: list[str] = []
        async with self._lock:
            if user_id in self._sessions:
                conflict = self._running_operation_labels(user_id)
            if not conflict:
                old = self._sessions.pop(user_id, None)
                self._sessions[user_id] = session
                self._explicit_disconnects.discard(user_id)
        if conflict:
            await session.disconnect()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Cannot reconnect while operations are running: "
                    f"{', '.join(conflict)}. Stop them first."
                ),
            )
        if old is not None:
            await old.disconnect()

        logger.info(
            "User %s connected to %s as %s",
            user_id,
            server_url,
            session.player_name,
        )
        return session

    async def disconnect(self, user_id: int) -> None:
        """Disconnect and remove a user's session (no-op if none exists).

        Serialized with the per-user reconnect lock: an auto-reconnect holds
        that lock across its whole login, so a logout arriving mid-login waits
        for the install and then tears it down — the explicit disconnect stays
        authoritative instead of being silently undone by the racing login.

        Refused (409) while background operations are running, exactly like
        reconnect and for the same reason: the detached job holds this
        session's HttpClient, and closing it underneath makes every following
        request in the job fail — loops that treat request failures as
        nonfatal then fail indefinitely. The check sits under the locks with
        no await before the pop, so an operation cannot start in between.
        """
        reconnect_lock = self.get_reconnect_lock(user_id)
        async with reconnect_lock, self._lock:
            if user_id in self._sessions:
                running = self._running_operation_labels(user_id)
                if running:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"Cannot disconnect while operations are running: "
                            f"{', '.join(running)}. Stop them first."
                        ),
                    )
            session = self._sessions.pop(user_id, None)
            self._explicit_disconnects.add(user_id)
        # The lock entry is deliberately never removed: waiters queued on it
        # hold a reference, and handing later callers a fresh lock from
        # get_reconnect_lock() would let two reconnects run concurrently. One
        # small Lock per user who ever connected is a bounded cost.
        if session:
            await session.disconnect()
            logger.info("User %s disconnected from %s", user_id, session.server_url)

    def get(self, user_id: int) -> Optional[TravianSession]:
        """Return an active session for *user_id*, or ``None``."""
        return self._sessions.get(user_id)

    def auto_reconnect_allowed(self, user_id: int) -> bool:
        """False after an explicit disconnect, until an explicit connect."""
        return user_id not in self._explicit_disconnects

    def clear_explicit_disconnect(self, user_id: int) -> None:
        """An explicit reconnect request overrides an earlier explicit disconnect."""
        self._explicit_disconnects.discard(user_id)

    def _running_operation_labels(self, user_id: int) -> list[str]:
        """Labels of the user's background operations that are still running.

        Two independent registries hold work that pins this session's
        HttpClient: OperationManager (detached farm/scout/queue/raid tasks)
        and ActiveOpRegistry (WebSocket-driven work like the raid analyzer,
        which never becomes an OperationManager task). Both must be consulted
        — an operation missing from the guard lets disconnect/reconnect close
        the client underneath it mid-run.
        """
        from travian_api.operation_manager import operation_manager
        from travian_api.web.operation_gate import active_ops

        labels = {op.label for op in operation_manager.list_for_user(user_id) if not op.task.done()}
        labels.update(active_ops.get_active(user_id))
        return sorted(labels)

    def get_reconnect_lock(self, user_id: int) -> asyncio.Lock:
        """Return a per-user lock for serializing auto-reconnect attempts.

        setdefault keeps creation atomic at the dict level, so concurrent
        first-use callers can never end up holding two different locks.
        """
        return self._reconnect_locks.setdefault(user_id, asyncio.Lock())

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

# After a FAILED background restore, do not retry for this long. The /status
# health poll calls try_restore_session on every tick; with stale credentials
# or a downed world, retrying each poll is unbounded login traffic and
# bot-detection pressure. Keyed by user id, monotonic deadline.
_RESTORE_RETRY_SECONDS = 60.0
_restore_backoff: dict[int, float] = {}


def reset_restore_backoff(user_id: int) -> None:
    """Drop the failed-restore backoff for a user.

    Called when the user EXPLICITLY reconnects: the backoff exists to stop
    per-request auto-reconnect storms, not to block an intentional retry.
    """
    _restore_backoff.pop(user_id, None)


def require_village_id(village_id: Optional[int]) -> int:
    """A village-specific route must be told which village, explicitly.

    Village selection is client-side (tab-local /switch), so falling back to a
    shared session default risks acting on the WRONG village — a wasted or
    wrong Travian request, which the fewer-requests/stealth goals reject. Every
    first-party caller already sends its own ``village_id``; a missing one is a
    caller bug and fails loud with 400 rather than silently guessing.
    """
    if village_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="village_id is required.",
        )
    return village_id


def latest_credential_query(user_id: int):
    """The saved credential auto-restore should use: most recently connected,
    with newest-id as the tie-break — timestamps are commonly NULL on freshly
    saved rows and legacy duplicates, and without the tie-break SQLite may
    pick an older row with an outdated password."""
    from sqlalchemy import select

    from travian_api.web.models.db import TravianCredential

    return (
        select(TravianCredential)
        .where(TravianCredential.user_id == user_id)
        .order_by(
            TravianCredential.last_connected.desc().nulls_last(),
            TravianCredential.id.desc(),
        )
        .limit(1)
    )


async def try_restore_session(user_id: int) -> Optional[TravianSession]:
    """Best-effort auto-reconnect from saved credentials, for WebSocket auth.

    HTTP requests restore through :func:`get_travian_session`; sockets have no
    request-scoped db dependency, so this opens its own session. Returns the
    live or restored session, or None when nothing is saved or the login fails
    — WebSocket auth turns None into a policy-violation close, not a 403.
    """
    session = session_manager.get(user_id)
    if session is not None:
        return session
    if not session_manager.auto_reconnect_allowed(user_id):
        # The user explicitly disconnected; restoring would override them.
        return None
    if time.monotonic() < _restore_backoff.get(user_id, 0.0):
        return None

    reconnect_lock = session_manager.get_reconnect_lock(user_id)
    async with reconnect_lock:
        session = session_manager.get(user_id)
        if session is not None:
            return session
        if not session_manager.auto_reconnect_allowed(user_id):
            return None
        if time.monotonic() < _restore_backoff.get(user_id, 0.0):
            return None

        from datetime import datetime

        from travian_api.web.auth import decrypt_credential
        from travian_api.web.models.db import async_session_factory

        try:
            async with async_session_factory() as db:
                result = await db.execute(latest_credential_query(user_id))
                cred = result.scalar_one_or_none()
                if cred is None:
                    return None
                server_url = cred.server_url
                username = cred.travian_username
                password = decrypt_credential(cred.encrypted_password)
        except Exception as exc:
            # A corrupt/undecryptable credential or a DB read error fails the
            # same way a bad login does — back off so reconnecting sockets do
            # not repeat this restore work on every retry.
            logger.warning("WebSocket auto-reconnect failed for user %s: %s", user_id, exc)
            _restore_backoff[user_id] = time.monotonic() + _RESTORE_RETRY_SECONDS
            return None

        try:
            # The reconnect lock is already held; go through the locked core.
            session = await session_manager._connect_locked(
                user_id=user_id,
                server_url=server_url,
                username=username,
                password=password,
            )
        except Exception as exc:
            logger.warning("WebSocket auto-reconnect failed for user %s: %s", user_id, exc)
            _restore_backoff[user_id] = time.monotonic() + _RESTORE_RETRY_SECONDS
            return None

        _restore_backoff.pop(user_id, None)

        # Bookkeeping only: the session is live regardless, and a failed stamp
        # must not make the caller report a login failure.
        try:
            async with async_session_factory() as db:
                result = await db.execute(latest_credential_query(user_id))
                cred = result.scalar_one_or_none()
                if cred is not None:
                    cred.last_connected = datetime.now(UTC)
                    await db.commit()
        except Exception:
            logger.warning("Auto-reconnected user %s but could not stamp last_connected", user_id)

        logger.info("Auto-reconnected user %s for a WebSocket", user_id)
        return session


async def get_live_travian_session(
    user: User = Depends(get_current_user),
) -> TravianSession:
    """The live session only — NO auto-reconnect.

    For endpoints that price their game traffic explicitly (the distribution
    snapshot): an implicit login would spend unreported requests before the
    handler even starts. 403 tells the operator to reconnect deliberately.
    """
    session = session_manager.get(user.id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Not connected. Reconnect first — this endpoint never spends "
                "login traffic implicitly."
            ),
        )
    return session


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


async def get_travian_session(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TravianSession:
    """FastAPI dependency returning the caller's active Travian session.

    If no session exists but the user has saved credentials, attempts to
    auto-reconnect using the most recently connected saved credential — unless
    the user explicitly disconnected, which stands until an explicit connect.
    Raises HTTP 403 if reconnection fails, is suppressed, or nothing is saved.
    """
    session = session_manager.get(user.id)
    if session is not None:
        return session
    if not session_manager.auto_reconnect_allowed(user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Disconnected. Use POST /api/travian/connect to reconnect.",
        )
    # A page load fans out several protected calls; after a FAILED restore,
    # each retrying with a real login is the same storm /status avoids.
    if time.monotonic() < _restore_backoff.get(user.id, 0.0):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Auto-reconnect recently failed and is backing off. "
                "Use POST /api/travian/connect to reconnect now."
            ),
        )

    # Serialize reconnect attempts per user — prevents two concurrent requests
    # from both triggering session_manager.connect() simultaneously.
    reconnect_lock = session_manager.get_reconnect_lock(user.id)
    async with reconnect_lock:
        # Re-check after acquiring lock — another request may have reconnected
        session = session_manager.get(user.id)
        if session is not None:
            return session
        if not session_manager.auto_reconnect_allowed(user.id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Disconnected. Use POST /api/travian/connect to reconnect.",
            )
        if time.monotonic() < _restore_backoff.get(user.id, 0.0):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "Auto-reconnect recently failed and is backing off. "
                    "Use POST /api/travian/connect to reconnect now."
                ),
            )

        # Try auto-reconnect from saved credentials
        from travian_api.web.auth import decrypt_credential

        result = await db.execute(latest_credential_query(user.id))
        cred = result.scalar_one_or_none()

        if cred is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not connected to a Travian server. Use POST /api/travian/connect first.",
            )

        try:
            password = decrypt_credential(cred.encrypted_password)
            # The reconnect lock is already held; go through the locked core.
            session = await session_manager._connect_locked(
                user_id=user.id,
                server_url=cred.server_url,
                username=cred.travian_username,
                password=password,
            )
        except Exception as exc:
            logger.warning("Auto-reconnect failed for user %s: %s", user.id, exc)
            _restore_backoff[user.id] = time.monotonic() + _RESTORE_RETRY_SECONDS
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Auto-reconnect failed: {exc}. Please reconnect manually.",
            )

        _restore_backoff.pop(user.id, None)

        # Bookkeeping only: the session is live regardless, and a failed stamp
        # must not turn a successful login into a 403.
        try:
            from datetime import datetime

            cred.last_connected = datetime.now(UTC)
            await db.commit()
        except Exception:
            logger.warning("Auto-reconnected user %s but could not stamp last_connected", user.id)

        logger.info("Auto-reconnected user %s to %s", user.id, cred.server_url)
        return session
