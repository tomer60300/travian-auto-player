"""Background recon account for AutoScout read operations.

Why this exists
---------------
The auto-scout's heavy traffic — ``/api/v1/map/position`` calls during the
sweep, ``/api/v1/map/tile-details`` for every enriched tile, and
``/profile/<player_id>`` for population/capital lookups — is the
single biggest source of bot-detection / rate-limit pressure for the
user's main account. A scan of a few hundred tiles can issue ~350
authenticated requests in a tight window. Even with the stealth
throttler keeping the per-window count under the burst cap, sustained
daily scans rack up a fingerprint that a vigilant Travian admin
would notice.

The recon account is a disposable secondary login. The data those
read endpoints return is identical regardless of *which* account
asks — map tiles, tile-detail popups, and player profiles are
visible to every logged-in player on the same Travian server.
Routing the read traffic through the recon HttpClient keeps every
scout-related request fingerprintable to the throwaway account; if
Travian eventually flags the recon, the user makes a new one and
their primary account never lit up.

Architecture
------------
One process-global HttpClient per Travian server URL. Travian
restricts each account to ONE active session at a time, so multiple
parallel users on the same server share the SAME recon HttpClient
(its own throttler still bounds the request rate). Lazy auth on
first use; cached for the process lifetime; transparent re-auth on
session expiry via the HttpClient's existing auth-callback wiring.

Write operations (sending scouts, querying the user's rally point)
must NEVER route through the recon client — recon has no villages
to send troops from. Read operations (the three listed above) are
the only ones that route through recon. The split lives in
:py:class:`AutoScoutService`.

Failure modes
-------------
- Credentials not configured -> ``get_or_create`` returns ``None``.
  Callers transparently fall back to the user's primary http_client.
- Credentials wrong / recon banned -> first login raises; we log a
  warning and cache ``None`` so we don't retry every operation.
  Operator sees the warning and rotates credentials.
- Recon session expires mid-scan -> existing http_client reauth
  callback re-logs the recon account using the same credentials.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from ..clients.http_client import HttpClient
from ..config import Settings
from ..services.auth_service import AuthService

logger = logging.getLogger(__name__)


_RECON_DATA_DIR = Path(tempfile.gettempdir()) / "travian_web_sessions" / "_recon"

# How long to wait before retrying recon login after an auth failure.
# Without this, a transient network blip or a Travian login captcha
# during one scan would permanently disable the recon proxy until
# operator restart. 30 min strikes a balance: long enough that we
# don't hammer Travian on a real ban, short enough that intermittent
# issues self-heal.
_RECON_AUTH_RETRY_AFTER_S = 30 * 60


def _server_slug(server_url: str) -> str:
    """File-system-safe identifier for a Travian server URL. Used to
    namespace the cookie / persona / scheduler files so a deployment
    that touches multiple Travian worlds keeps recon state per world."""
    host = urlparse(server_url).hostname or "unknown"
    return host.replace(".", "_")


class ReconAccount:
    """One authenticated recon HttpClient + AuthService pair, scoped to
    a single Travian server URL."""

    def __init__(self, server_url: str, username: str, password: str) -> None:
        self.server_url = server_url.rstrip("/")
        self.username = username
        # Per-server data dir so persona/cookies don't collide across
        # multiple Travian worlds in a single deployment.
        data_dir = _RECON_DATA_DIR / _server_slug(self.server_url)
        data_dir.mkdir(parents=True, exist_ok=True)

        settings = Settings().model_copy(
            update={
                "base_url": self.server_url,
                "username": username,
                "password": password,
                "jwt_cache_file": str(data_dir / "jwt_cache.json"),
                "jwt_cache_path": str(data_dir / "jwt_cache.json"),
            }
        )
        self.http_client = HttpClient(settings, cookie_file=data_dir / "cookies.json")
        self.auth_service = AuthService(self.http_client, settings)
        # Authentication state machine:
        #   _authed=True              -> session is live, use it
        #   _authed=False AND _last_failure_at within retry window
        #                             -> sticky failure, don't hammer
        #   _authed=False AND _last_failure_at older than window
        #                             -> retry on next call
        #   _authed=None              -> never tried, attempt on next call
        self._authed: Optional[bool] = None
        self._last_failure_at: float = 0.0

    async def ensure_authed(self) -> bool:
        """Authenticate if we haven't already. Returns True iff the
        recon HttpClient now has live cookies.

        Failures are sticky for ``_RECON_AUTH_RETRY_AFTER_S`` (30 min)
        so transient issues self-heal without operator intervention,
        but a real ban doesn't drown Travian's login endpoint in
        per-scan retries.
        """
        if self._authed is True:
            return True
        if self._authed is False:
            elapsed = time.monotonic() - self._last_failure_at
            if elapsed < _RECON_AUTH_RETRY_AFTER_S:
                return False
            # Window elapsed — fall through and retry. Log so an operator
            # debugging "why is recon coming back" sees the transition.
            logger.info(
                "Recon account: retry window elapsed (%.0fs since last "
                "failure), attempting re-auth for server=%s username=%s",
                elapsed,
                self.server_url,
                self.username,
            )
        try:
            await self.auth_service.login()
        except Exception as exc:
            self._authed = False
            self._last_failure_at = time.monotonic()
            logger.warning(
                "Recon proxy login failed (server=%s username=%s): %r. "
                "Read ops will fall back to the primary account for up "
                "to %ds, then re-attempt.",
                self.server_url,
                self.username,
                exc,
                _RECON_AUTH_RETRY_AFTER_S,
            )
            return False
        self._authed = True
        self._last_failure_at = 0.0
        logger.info(
            "Recon proxy account authenticated: server=%s username=%s. "
            "Subsequent AutoScout read ops will dispatch through this "
            "account instead of any user's primary login.",
            self.server_url,
            self.username,
        )
        return True

    async def close(self) -> None:
        try:
            await self.http_client.close()
        except Exception:
            pass


# Module-level cache for Settings so we don't pay .env re-read cost on
# every recon lookup. The settings object is immutable after creation,
# so re-using it across calls is safe.
_cached_settings: Optional[Settings] = None


def _get_settings() -> Settings:
    global _cached_settings
    if _cached_settings is None:
        _cached_settings = Settings()
    return _cached_settings


class ReconAccountManager:
    """Process-global registry of recon accounts, keyed by Travian
    server URL. Thread-unsafe by design — assumes asyncio event loop.
    """

    def __init__(self) -> None:
        self._accounts: dict[str, ReconAccount] = {}
        self._auth_lock = asyncio.Lock()
        # Operator-supplied credentials, loaded from the DB at startup and
        # replaced when they are rotated through the UI. None means "no stored
        # credentials" and the .env values remain in force. The third element
        # scopes them to one Travian world (None = any).
        self._override: Optional[tuple[str, str, Optional[str]]] = None

    def credentials(self, server_url: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Active recon credentials: stored ones win, .env is the fallback.

        Stored credentials scoped to a world are only handed out for THAT
        world: the recon account exists on one server, and using it to mask
        logins elsewhere fails and silently unmasks the reads. Keeping the env
        path alive means existing deployments that never touch the UI keep
        working exactly as before.
        """
        if self._override is not None:
            username, password, scope = self._override
            if scope is not None and server_url is not None and scope != server_url.rstrip("/"):
                return None, None
            return username, password
        s = _get_settings()
        if s.recon_username and s.recon_password:
            return s.recon_username, s.recon_password
        return None, None

    def credentials_source(self) -> Optional[str]:
        """Where the active credentials came from: 'stored', 'env', or None."""
        if self._override is not None:
            return "stored"
        s = _get_settings()
        return "env" if (s.recon_username and s.recon_password) else None

    def is_configured(self) -> bool:
        """True iff recon credentials are available (stored, or from env)."""
        username, password = self.credentials()
        return bool(username and password)

    def get_proxy_username(self) -> Optional[str]:
        """The recon account's username, for log/UI display so it's
        always visible WHICH disposable account is acting as proxy.
        Returns None when not configured."""
        username, _ = self.credentials()
        return username or None

    def set_credentials(
        self, username: str, password: str, server_url: Optional[str] = None
    ) -> None:
        """Install rotated credentials, optionally scoped to one Travian world.

        Caller must invalidate() to apply them to any already-authenticated
        client."""
        self._override = (username, password, server_url.rstrip("/") if server_url else None)

    def clear_credentials(self) -> None:
        """Forget stored credentials and fall back to .env."""
        self._override = None

    async def invalidate(self) -> None:
        """Drop every cached recon account.

        Necessary after a credential change: a ReconAccount captures its
        username/password at construction and caches both the authenticated
        session and a 30-minute sticky-failure window, so without this a
        rotation could not take effect and a prior failure would keep
        suppressing retries.
        """
        async with self._auth_lock:
            for account in list(self._accounts.values()):
                await account.close()
            self._accounts.clear()

    async def get_or_create_client(self, server_url: str) -> Optional[HttpClient]:
        """Return an authenticated HttpClient for the recon account on
        the given server, or None when:
          * Credentials aren't configured, OR
          * Auth has previously failed and the retry window hasn't
            elapsed yet.

        Callers MUST tolerate None and fall back to their primary
        HttpClient — this function never raises.
        """
        username, password = self.credentials(server_url)
        if not (username and password):
            return None
        key = server_url.rstrip("/")
        async with self._auth_lock:
            account = self._accounts.get(key)
            if account is None:
                account = ReconAccount(key, username, password)
                self._accounts[key] = account
            ok = await account.ensure_authed()
            return account.http_client if ok else None

    async def shutdown(self) -> None:
        """Close every cached HttpClient on app shutdown. Called from
        the FastAPI app's lifespan handler — without this, the
        curl_cffi / httpx connection pools leak on hot reload."""
        for account in list(self._accounts.values()):
            await account.close()
        self._accounts.clear()


# Module-level singleton — imported by AutoScoutService and the WS
# orchestrators. Idempotent across multiple imports.
recon_account_manager = ReconAccountManager()


async def acquire_recon_client(base_url: str) -> Optional[HttpClient]:
    """Recon (masking) client for account-INDEPENDENT gathering reads —
    map tiles, tile-details, profiles, name/coords lookups, village
    metadata about OTHER players. Returns the recon client when it is
    configured + authenticated, else ``None`` so the caller falls back to
    its own primary.

    This is the by-default masking entry point for gathering reads that
    live OUTSIDE the AutoScout WS coroutines (oasis raider, target
    resolver, raid analyzer, diagnostics). It NEVER raises — masking is
    best-effort by default. The strict "Require background account"
    escalation (abort instead of fall back) is AutoScout-scan-only and
    lives in :meth:`AutoScoutService._read_client`; gathering reads here
    always degrade gracefully to the primary rather than fail an
    operation that the user didn't flag strict.

    Account-SPECIFIC reads (your own villages/troops/reports) must NOT use
    this — recon is a different account and would return the wrong data.
    """
    try:
        return await recon_account_manager.get_or_create_client(base_url)
    except Exception:
        logger.warning(
            "recon acquisition failed for server=%s; caller will fall back "
            "to its primary account for this read.",
            base_url,
            exc_info=True,
        )
        return None
