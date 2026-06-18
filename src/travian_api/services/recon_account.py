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

    def is_configured(self) -> bool:
        """True iff recon credentials are present in settings/env."""
        s = _get_settings()
        return bool(s.recon_username and s.recon_password)

    def get_proxy_username(self) -> Optional[str]:
        """The recon account's username, for log/UI display so it's
        always visible WHICH disposable account is acting as proxy.
        Returns None when not configured."""
        s = _get_settings()
        return s.recon_username or None

    async def get_or_create_client(self, server_url: str) -> Optional[HttpClient]:
        """Return an authenticated HttpClient for the recon account on
        the given server, or None when:
          * Credentials aren't configured, OR
          * Auth has previously failed and the retry window hasn't
            elapsed yet.

        Callers MUST tolerate None and fall back to their primary
        HttpClient — this function never raises.
        """
        s = _get_settings()
        if not (s.recon_username and s.recon_password):
            return None
        key = server_url.rstrip("/")
        async with self._auth_lock:
            account = self._accounts.get(key)
            if account is None:
                account = ReconAccount(key, s.recon_username, s.recon_password)
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
