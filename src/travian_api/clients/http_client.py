"""HTTP client for Travian API with JWT authentication, retry logic, and stealth mode.

Uses curl_cffi for Chrome TLS fingerprint impersonation when available,
falling back to httpx if curl_cffi is not installed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional
from urllib.parse import urlencode, urljoin

if TYPE_CHECKING:
    from ..stealth.browser_headers import BrowserHeaders
    from ..stealth.captcha_guard import CaptchaGuard
    from ..stealth.human_delay import HumanDelay
    from ..stealth.navigator import PageNavigator
    from ..stealth.noise import NoiseInjector
    from ..stealth.scheduler import ActivityScheduler
    from ..stealth.throttler import RequestThrottler

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_random_exponential

from ..config import Settings
from ..debug_dump import debug_dumper
from ..exceptions import ActivityBudgetExhausted, NetworkError, SessionExpiredError
from ..utils.helpers import mask_sensitive_data

logger = logging.getLogger(__name__)

# Try to import curl_cffi for Chrome TLS fingerprinting
try:
    from curl_cffi import CurlError
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False
    CurlError = Exception  # placeholder for retry_if_exception_type


class HttpClient:
    """HTTP client for Travian API with session management, retry logic, and stealth mode.

    When stealth mode is enabled (default), all requests go through:
    1. Request throttler (minimum gap between requests)
    2. Human-like delay (appropriate to action type)
    3. Realistic browser headers (UA, Sec-Fetch-*, Referer)
    4. Page navigation simulation (optional)
    5. Chrome TLS fingerprint via curl_cffi (if available)
    """

    def __init__(self, settings: Settings, cookie_file: Path | None = None):
        self.settings = settings
        self.base_url = settings.base_url.rstrip("/")
        self._resolved_x_version: str | None = None
        self._auth_callback: Optional[callable] = None
        self._cookie_file = (
            cookie_file if cookie_file is not None else Path(".travian_cookies.json")
        )

        # Initialize stealth components (needs _cookie_file for persona/scheduler paths)
        self._stealth_enabled = settings.stealth
        self._init_stealth(settings)

        # Determine transport mode
        self._use_curl = HAS_CURL_CFFI and settings.stealth
        self._curl_session: Optional[Any] = None  # CurlAsyncSession (lazy init)

        if not HAS_CURL_CFFI and settings.stealth:
            # Fail closed: stealth mode + httpx-only means we'd send Chrome-shaped
            # headers over a non-Chrome TLS/JA3 fingerprint. That mismatch is a
            # stronger bot-tell than running with stealth disabled. Force the
            # operator to either install curl_cffi or explicitly turn off stealth.
            raise RuntimeError(
                "stealth mode requires curl_cffi for matching TLS fingerprints. "
                "Install with: pip install curl_cffi — or disable stealth in settings."
            )

        # Create httpx client (used as fallback or when stealth is off)
        ua = (
            self._browser_headers.for_page_load().get("User-Agent", "Mozilla/5.0")
            if self._stealth_enabled
            else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            headers={
                "User-Agent": ua,
                "X-Version": settings.x_version,
            },
        )

        # Load persisted cookies from file
        self._load_cookies()

    def _load_cookies(self) -> None:
        """Load cookies from persistent file (same browser session across CLI invocations)."""
        try:
            if self._cookie_file.exists():
                data = json.loads(self._cookie_file.read_text())
                for name, value in data.items():
                    self.client.cookies.set(name, value)
                logger.debug(f"Loaded {len(data)} cookies from {self._cookie_file}")
        except Exception as e:
            logger.warning("Failed to load cookies from %s: %s", self._cookie_file, e)

    def _save_cookies(self) -> None:
        """Save all cookies to persistent file."""
        try:
            cookies = self.get_cookies()
            if cookies:
                self._cookie_file.write_text(json.dumps(cookies, indent=2))
                try:
                    os.chmod(self._cookie_file, 0o600)
                except OSError:
                    pass  # Windows ACL may not support chmod
                logger.debug(f"Saved {len(cookies)} cookies to {self._cookie_file}")
        except Exception as e:
            logger.warning("Failed to save cookies to %s: %s", self._cookie_file, e)

    def _init_stealth(self, settings: Settings) -> None:
        """Initialize stealth/anti-bot components."""
        from ..stealth.captcha_guard import CaptchaGuard
        from ..stealth.headers import BrowserHeaders
        from ..stealth.human_delay import HumanDelay
        from ..stealth.navigator import PageNavigator
        from ..stealth.noise import NoiseInjector
        from ..stealth.persona import build_persona, load_persona, save_persona
        from ..stealth.scheduler import ActivityScheduler
        from ..stealth.session_tempo import SessionTempo
        from ..stealth.throttler import RequestThrottler
        from ..stealth.user_agents import UserAgentRotator

        # ── Persistent persona: same cookies = same browser identity ──
        self._persona_file = self._cookie_file.parent / ".travian_persona.json"
        persona = load_persona(self._persona_file, server_url=settings.base_url)
        if persona is None:
            persona = build_persona(server_url=settings.base_url)
            save_persona(persona, self._persona_file, server_url=settings.base_url)
            logger.info(
                "Created new persona: %s (impersonate=%s)", persona.user_agent, persona.impersonate
            )
        else:
            logger.debug(
                "Loaded existing persona: %s (impersonate=%s)",
                persona.user_agent,
                persona.impersonate,
            )

        self._persona = persona
        self._ua_rotator = UserAgentRotator(persona=persona, server_url=settings.base_url)
        self._browser_headers = BrowserHeaders(self._ua_rotator, settings.base_url)
        self._captcha_guard = CaptchaGuard()
        self._throttler = RequestThrottler(
            min_gap_s=settings.stealth_min_gap,
            max_gap_s=settings.stealth_max_gap,
            burst_max_requests=settings.stealth_burst_max,
            burst_cooldown_s=settings.stealth_burst_cooldown,
            enabled=settings.stealth,
        )
        self._throttler.set_captcha_guard(self._captcha_guard)
        # Stable per-account identity for all local behavioral seeds. Includes
        # the persona salt because UA/lang/server alone are low-entropy on one
        # world — without the salt, accounts collide into a few behavioral
        # buckets a detector could cluster.
        behavioral_identity = (
            f"{persona.user_agent}|{persona.accept_language}|{settings.base_url}|{persona.salt}"
        )
        # Bind the request-gap shape to the identity so one account keeps a
        # stable timing fingerprint across restarts (no cross-session drift)
        # while differing from other accounts.
        self._throttler.seed_gap_shape(behavioral_identity)
        self._human_delay = HumanDelay(
            speed_factor=settings.stealth_speed,
            enabled=settings.stealth,
        )
        # Per-account spread on the action-delay distributions, same identity.
        self._human_delay.seed_delays(behavioral_identity)
        # One shared, slowly-drifting session tempo feeds BOTH the action-delay
        # engine and the request throttler, so consecutive delays and gaps are
        # positively correlated (a human session tempo) rather than iid.
        self._session_tempo = SessionTempo(behavioral_identity)
        self._human_delay.set_tempo(self._session_tempo)
        self._throttler.set_tempo(self._session_tempo)
        self._navigator = PageNavigator(
            http_client=self,
            human_delay=self._human_delay,
            enabled=settings.stealth and settings.stealth_navigate,
        )
        # Bind warm-up route preferences to the same identity so each account
        # has a stable-but-distinct browsing pattern (no shared deterministic
        # route, and no collision into a few buckets on one world).
        self._navigator.seed_routes(behavioral_identity)
        self._noise_injector = NoiseInjector(
            navigator=self._navigator,
            human_delay=self._human_delay,
            noise_rate=settings.stealth_noise_rate,
            enabled=settings.stealth,
        )
        self._activity_scheduler = ActivityScheduler(
            max_daily_hours=settings.stealth_max_daily_hours,
            max_continuous_hours=settings.stealth_max_continuous_hours,
            min_break_minutes=settings.stealth_min_break_minutes,
            enabled=settings.stealth,
            state_file=self._cookie_file.parent / ".scheduler_state.json",
        )
        # Bind the night-rest phase + wake distribution to the same identity, so
        # accounts on one host don't share a synchronized circadian phase /
        # wake-time CDF (the last stealth component that was identity-blind).
        self._activity_scheduler.seed_circadian(behavioral_identity)

    async def _fetch_x_version(self) -> str:
        """Return the best known X-Version value.

        On first call, returns the config fallback immediately (no network).
        After authentication succeeds, call ``try_resolve_x_version()`` to
        attempt a live lookup from a game page.  This avoids surprise
        pre-login fetches and ensures stealth mode is not bypassed.
        """
        if self._resolved_x_version is not None:
            return self._resolved_x_version
        # First call — use config default (no network request)
        self._resolved_x_version = self.settings.x_version
        return self._resolved_x_version

    async def try_resolve_x_version(self) -> None:
        """Attempt to resolve X-Version from a live game page.

        Call this AFTER successful authentication.  Parses ``gpack/{VERSION}/``
        from /dorf1.php.  On failure, keeps the config fallback silently.
        """
        try:
            html = await self.get_html("/dorf1.php", skip_reauth=True)
            m = re.search(r"gpack/(\d+)/", html)
            if not m:
                m = re.search(r'window\.Travian\.version\s*=\s*["\'](\d+)["\']', html)
            if m:
                self._resolved_x_version = m.group(1)
                logger.info("Resolved X-Version from live page: %s", self._resolved_x_version)
        except Exception as e:
            logger.debug("Could not resolve X-Version from live page: %s", e)

    async def _ensure_curl_session(self) -> Any:
        """Lazy-create the curl_cffi session with persona-matched impersonation."""
        if self._curl_session is None and HAS_CURL_CFFI:
            target = getattr(self, "_persona", None)
            impersonate = target.impersonate if target else "chrome"
            self._curl_session = CurlAsyncSession(
                impersonate=impersonate,
            )
            # Sync persisted cookies (loaded into httpx) into the new curl session
            self._sync_cookies_to_curl()
        return self._curl_session

    # ── Stealth accessors (for services to use) ──────────────────────

    @property
    def stealth_enabled(self) -> bool:
        return self._stealth_enabled

    def tempo_scale(self, seconds: float) -> float:
        """Scale a human-paced wait by the shared session tempo.

        Lets macro loop intervals drift with the same tempo that already
        modulates per-action delays and request gaps, so a detector can't find
        a session where short gaps drift but raid/build/scout cadence stays
        independent (Ljung-Box / runs / lag-1 autocorrelation; periodogram /
        Lomb-Scargle on fixed polling cadences).

        No-op when stealth is off. Use ONLY for human-controlled loop/reaction
        pacing — NEVER for server-deadline countdowns, retry backoffs, or the
        ATG/video tick cadence.
        """
        if not self._stealth_enabled:
            return seconds
        return seconds * self._session_tempo.current()

    @property
    def throttler(self) -> RequestThrottler:
        return self._throttler

    @property
    def human_delay(self) -> HumanDelay:
        return self._human_delay

    @property
    def navigator(self) -> PageNavigator:
        return self._navigator

    @property
    def noise_injector(self) -> NoiseInjector:
        return self._noise_injector

    @property
    def activity_scheduler(self) -> ActivityScheduler:
        return self._activity_scheduler

    @property
    def browser_headers(self) -> BrowserHeaders:
        return self._browser_headers

    @property
    def captcha_guard(self) -> CaptchaGuard:
        return self._captcha_guard

    def check_activity_budget(self) -> bool:
        """Check whether the activity budget allows continued operation.

        Returns True if operations can continue, False if the daily or
        continuous session budget is exhausted.  WS handlers should call
        this at the start of each operation cycle.

        Raises:
            ActivityBudgetExhausted: when the budget is used up (so callers
                that forget to check the return value still stop).
        """
        if not self._stealth_enabled:
            return True
        if self._activity_scheduler.can_continue():
            return True
        sched = self._activity_scheduler
        rolling_h = sched.daily_hours_used
        session_h = sched.session_hours
        logger.warning(
            "Activity budget exhausted: rolling_24h=%.1fh/%.1fh, session=%.1fh/%.1fh",
            rolling_h,
            sched.max_daily_hours,
            session_h,
            sched.max_continuous_hours,
        )
        # Show which limit was actually hit
        if rolling_h >= sched.max_daily_hours:
            reason = f"rolling 24h limit reached ({rolling_h:.1f}h / {sched.max_daily_hours}h)"
        elif session_h >= sched.max_continuous_hours:
            reason = (
                f"continuous session limit reached ({session_h:.1f}h / {sched.max_continuous_hours}h)"
                f" — take a {sched.min_break_minutes:.0f}min break"
            )
        else:
            reason = f"rolling 24h {rolling_h:.1f}h / {sched.max_daily_hours}h, session {session_h:.1f}h / {sched.max_continuous_hours}h"
        raise ActivityBudgetExhausted(f"Activity budget exhausted: {reason}")

    def set_auth_callback(self, callback: callable) -> None:
        """Set callback function to call when re-authentication is needed."""
        self._auth_callback = callback

    async def close(self) -> None:
        """Close the HTTP client and persist cookies."""
        self._save_cookies()
        await self.client.aclose()
        if self._curl_session is not None:
            await self._curl_session.close()
            self._curl_session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def get_cookies(self) -> Dict[str, str]:
        cookies = {}
        # Get from httpx client
        for name in self.client.cookies:
            cookies[name] = self.client.cookies[name]
        # Also get from curl_cffi session if active
        if self._curl_session is not None:
            try:
                for name, value in self._curl_session.cookies.items():
                    cookies[name] = value
            except Exception:
                pass
        return cookies

    def set_cookie(self, name: str, value: str) -> None:
        self.client.cookies.set(name, value)
        # Also set in curl_cffi session if active
        if self._curl_session is not None:
            try:
                self._curl_session.cookies.set(name, value)
            except Exception:
                pass

    def clear_cookies(self) -> None:
        self.client.cookies.clear()
        if self._curl_session is not None:
            try:
                self._curl_session.cookies.clear()
            except Exception:
                pass

    async def _stealth_pre_request(self, url: str, request_type: str = "page") -> Dict[str, str]:
        """Run stealth pre-request checks and return appropriate headers.

        Args:
            url: Request URL (for context)
            request_type: "page", "json", "form", or "xhr"

        Returns:
            Headers dict to use for the request
        """
        x_version = await self._fetch_x_version()

        if not self._stealth_enabled:
            return {
                "Content-Type": "application/json"
                if request_type == "json"
                else "application/x-www-form-urlencoded"
                if request_type == "form"
                else "",
                "X-Version": x_version,
            }

        # Throttle
        await self._throttler.wait(context=url)

        # Get browser-appropriate headers
        if request_type == "json":
            headers = self._browser_headers.for_json_post(url)
        elif request_type == "form":
            headers = self._browser_headers.for_form_post(url)
        elif request_type == "xhr":
            headers = self._browser_headers.for_xhr(url)
        else:
            headers = self._browser_headers.for_page_load(url)

        # Always include X-Version
        headers["X-Version"] = x_version

        return headers

    def _stealth_post_request(
        self,
        request_type: str,
        response: Any | None = None,
        *,
        fallback_url: str = "",
    ) -> None:
        """Update stealth state after a request.

        Only document-like requests should advance page context.  API/XHR
        endpoints are intentionally excluded so they cannot become future
        Referer values.
        """
        if not self._stealth_enabled or request_type not in {"page", "form"}:
            return

        target_url = ""
        if response is not None and hasattr(response, "url"):
            target_url = str(response.url)
        if not target_url:
            target_url = fallback_url
        if target_url:
            self._browser_headers.update_last_page(target_url)

    @staticmethod
    def _dump_http_error(
        *,
        method: str,
        url: str,
        status: int,
        body: str,
        error: str = "",
    ) -> None:
        """Persist an unexpected HTTP response for later inspection."""
        # Use last path segment + query hash in the key so related errors
        # group together without exploding filename length.
        from urllib.parse import urlparse

        parsed = urlparse(url)
        key_base = (parsed.path.rstrip("/").rsplit("/", 1)[-1] or "root")[:40]
        key = f"{method.lower()}_{key_base}_{status}"
        debug_dumper.dump(
            "http_error",
            body,
            key=key,
            context={
                "method": method,
                "url": url,
                "status": status,
                "error": error,
            },
        )

    @staticmethod
    def _extract_snippet(text: str, pattern: str, context_chars: int = 200) -> str:
        """Extract a snippet around the first occurrence of *pattern*.

        Returns up to *context_chars* characters around the match with
        HTML tags stripped for readability.  Used for diagnostic logging.
        """
        idx = text.lower().find(pattern.lower())
        if idx == -1:
            return ""
        start = max(0, idx - context_chars // 2)
        end = min(len(text), idx + len(pattern) + context_chars // 2)
        snippet = text[start:end]
        # Strip HTML tags for readability
        snippet = re.sub(r"<[^>]+>", " ", snippet)
        # Collapse whitespace
        snippet = re.sub(r"\s+", " ", snippet).strip()
        if start > 0:
            snippet = "..." + snippet
        if end < len(text):
            snippet = snippet + "..."
        return snippet

    async def _check_suspicious_response(
        self,
        response_text: str,
        *,
        url: str = "",
        status_code: int = 0,
    ) -> None:
        """Check if server response indicates bot detection.

        Triggers the captcha guard (blocking ALL further requests) when
        captcha, rate limiting, or ban indicators are found.

        IMPORTANT: Travian HTML contains words like "upgradeBlocked" (CSS class)
        and "blocked" in normal game contexts.  Normal game pages also reference
        "recaptcha" in JavaScript bundles.  We use structural evidence (HTML
        elements, response size, HTTP status) to distinguish real captcha
        challenges from normal game content.
        """
        if not self._stealth_enabled:
            return

        response_lower = response_text.lower()
        resp_len = len(response_text)

        async def _fire(label: str) -> None:
            """Log diagnostics and trigger the captcha guard."""
            snippet = self._extract_snippet(response_text, label)
            logger.warning(
                "BOT DETECTION CONFIRMED: '%s' | url=%s | status=%d | "
                "response_len=%d | snippet: %s",
                label,
                url,
                status_code,
                resp_len,
                snippet,
            )
            debug_dumper.dump(
                "captcha",
                response_text,
                key=f"{label}_{status_code}",
                context={
                    "label": label,
                    "url": url,
                    "status_code": status_code,
                    "response_len": resp_len,
                    "snippet": snippet,
                },
            )
            await self._captcha_guard.trigger(
                label,
                url=url,
                status_code=status_code,
                response_snippet=snippet,
            )

        def _soft_penalty_seconds(default_seconds: float) -> float:
            """Avoid double-penalizing explicit 429 handlers later in the request flow."""
            if status_code == 429:
                return 0.0
            return default_seconds

        def _soft_fire(label: str, penalty_seconds: float) -> None:
            """Log a soft block/rate-limit signal without freezing the session."""
            snippet = self._extract_snippet(response_text, label)
            logger.warning(
                "SOFT BLOCK DETECTED: '%s' | url=%s | status=%d | response_len=%d | snippet: %s",
                label,
                url,
                status_code,
                resp_len,
                snippet,
            )
            debug_dumper.dump(
                "soft_block",
                response_text,
                key=f"{label}_{status_code}",
                context={
                    "label": label,
                    "url": url,
                    "status_code": status_code,
                    "response_len": resp_len,
                    "snippet": snippet,
                    "penalty_seconds": penalty_seconds,
                },
            )
            if penalty_seconds > 0 and self._stealth_enabled:
                self._throttler.add_penalty(penalty_seconds)

        # ── Phase 1: recaptcha — require structural HTML evidence ────
        #
        # The word "recaptcha" appears in Travian JS bundles and script
        # references on normal game pages.  Only trigger if it appears in
        # an actual captcha challenge context:
        #   - <div class="g-recaptcha" ...>
        #   - <script src="...recaptcha/api...">
        #   - <iframe ...recaptcha...>
        #   - Short error/block page (<5000 chars) with the word present
        if "recaptcha" in response_lower:
            is_structural = bool(
                re.search(
                    r'(class=["\']g-recaptcha|'
                    r"<(script|iframe)[^>]*recaptcha/api|"
                    r"<(div|form|iframe)[^>]*recaptcha)",
                    response_lower,
                )
            )
            is_short_page = resp_len < 5000
            is_error_status = status_code in (403, 429, 503)

            if is_structural:
                await _fire("recaptcha")
                return
            if is_short_page or is_error_status:
                _soft_fire("recaptcha_indicator", _soft_penalty_seconds(120.0))
                return
            logger.debug(
                "BOT DETECTION SKIPPED (likely false positive): 'recaptcha' in "
                "large response (%d chars, status=%d) without structural HTML "
                "evidence | url=%s",
                resp_len,
                status_code,
                url,
            )

        # ── Phase 1b: other high-confidence patterns ─────────────────
        #
        # These phrases don't normally appear in game HTML, but they can
        # show up in large JS bundles or error messages embedded in normal
        # pages. Distinguish:
        #   - Short page (<5000) with 403/503 → real block page → hard stop
        #     via captcha guard (a human would notice and stop, so should we)
        #   - Otherwise but still suspicious → soft penalty (transient or
        #     embedded-in-bundle false positive)
        # 429 stays soft regardless because Travian rate-limits legitimate
        # browser sessions too.
        error_page_patterns = [
            "bot-detection",
            "suspicious activity",
            "automated access",
            "your ip has been",
            "access denied",
        ]

        for pattern in error_page_patterns:
            if pattern in response_lower:
                is_short = resp_len < 5000
                is_block_status = status_code in (403, 503)
                is_rate_limit = status_code == 429
                if is_short and is_block_status:
                    await _fire(pattern)
                    return
                if is_short or is_rate_limit or status_code in (0, 403, 503):
                    _soft_fire(pattern, _soft_penalty_seconds(90.0))
                    return
                logger.debug(
                    "BOT DETECTION SKIPPED: '%s' in large response (%d chars, "
                    "status=%d) — likely embedded in normal page | url=%s",
                    pattern,
                    resp_len,
                    status_code,
                    url,
                )

        # ── Phase 2: captcha form — structural regex check ───────────
        if "captcha" in response_lower:
            if re.search(r"<(form|div|iframe)[^>]*captcha", response_lower):
                await _fire("captcha_form")
                return

        # ── Phase 3: "too many requests" — transient rate limit (soft) ─
        # These are temporary throttle events, not real captcha/bot detection.
        # Handle with a throttler penalty, not the captcha hard-stop.
        if "too many requests" in response_lower:
            if resp_len < 2000:
                _soft_fire("too_many_requests", _soft_penalty_seconds(120.0))
                return

        # ── Phase 4: ban message — exact phrase match ────────────────
        if (
            "your account has been banned" in response_lower
            or "you have been banned" in response_lower
        ):
            await _fire("account_banned")
            return

        # ── Phase 5: unknown block page — tiered handling for 403/429/503
        #
        # 429 is always a transient rate limit → throttler penalty.
        # 403/503 on short non-game pages → captcha guard (may be real block).
        if status_code in (403, 429, 503):
            _has_game_marker = (
                "travian" in response_lower
                or "troop" in response_lower
                or "village" in response_lower
                or "dorf1" in response_lower
                or "dorf2" in response_lower
                or "buildingSlot" in response_lower
            )
            _is_login_page = "login" in response_lower or "password" in response_lower
            if not _has_game_marker and not _is_login_page and resp_len < 5000:
                _soft_fire("unknown_block_page", _soft_penalty_seconds(90.0))
                return

    def _sync_cookies_to_curl(self) -> None:
        """Copy httpx cookies to curl_cffi session (call after httpx-based requests)."""
        if self._curl_session is not None:
            try:
                for name in self.client.cookies:
                    self._curl_session.cookies.set(name, self.client.cookies[name])
            except Exception:
                pass

    def _sync_cookies_from_curl(self, curl_response: Any) -> None:
        """Copy cookies from curl_cffi response to httpx client."""
        try:
            if hasattr(curl_response, "cookies"):
                for name, value in curl_response.cookies.items():
                    self.client.cookies.set(name, value)
            # Also sync from session cookies
            if self._curl_session is not None:
                for name, value in self._curl_session.cookies.items():
                    self.client.cookies.set(name, value)
        except Exception:
            pass

    async def _curl_post_json(self, url: str, data: Dict[str, Any], headers: Dict[str, str]) -> Any:
        """Make a POST request with JSON data via curl_cffi."""
        session = await self._ensure_curl_session()
        response = await session.post(
            url,
            json=data,
            headers=headers,
            timeout=self.settings.timeout,
            allow_redirects=False,
        )
        self._sync_cookies_from_curl(response)
        return response

    async def _curl_delete_json(
        self, url: str, headers: Dict[str, str], data: Dict[str, Any] | None = None
    ) -> Any:
        """Make a DELETE request via curl_cffi."""
        session = await self._ensure_curl_session()
        kwargs: Dict[str, Any] = {
            "headers": headers,
            "timeout": self.settings.timeout,
            "allow_redirects": False,
        }
        if data is not None:
            kwargs["json"] = data
        response = await session.delete(url, **kwargs)
        self._sync_cookies_from_curl(response)
        return response

    async def _curl_post_form(self, url: str, form_data: str, headers: Dict[str, str]) -> Any:
        """Make a POST request with form data via curl_cffi."""
        session = await self._ensure_curl_session()
        response = await session.post(
            url,
            data=form_data.encode(),
            headers=headers,
            timeout=self.settings.timeout,
            allow_redirects=False,
        )
        self._sync_cookies_from_curl(response)
        return response

    async def _curl_get(
        self, url: str, headers: Dict[str, str], follow_redirects: bool = True
    ) -> Any:
        """Make a GET request via curl_cffi."""
        session = await self._ensure_curl_session()
        response = await session.get(
            url,
            headers=headers,
            timeout=self.settings.timeout,
            allow_redirects=follow_redirects,
        )
        self._sync_cookies_from_curl(response)
        return response

    async def post_json(
        self,
        url: str,
        data: Dict[str, Any],
        *,
        skip_reauth: bool = False,
        safe_to_retry: bool = True,
        request_type: str = "json",
        _retry: int = 0,
    ) -> Dict[str, Any]:
        """Make a POST request with JSON data.

        Args:
            request_type: "json" (default, generic API client) or "xhr" — use
                "xhr" for endpoints that the Travian frontend JavaScript calls
                via fetch/XMLHttpRequest (map/position, tile-details,
                /api/v1/farm-list/*). The XHR shape adds X-Requested-With and
                Sec-Fetch-Mode: cors so browser-frontend traffic is not
                fingerprinted as a generic JSON client.
        """
        if not url.startswith("http"):
            url = urljoin(self.base_url, url.lstrip("/"))

        # XHR requests still send a JSON body; merge in JSON Content-Type on top
        # of the XHR header shape so the request stays a valid fetch+json POST.
        rt = "xhr" if request_type == "xhr" else "json"
        headers = await self._stealth_pre_request(url, rt)
        # The non-stealth header path returns Content-Type="" which would
        # bypass the `not in` form of this guard. `not headers.get(...)`
        # treats both "missing" and "empty string" identically.
        if rt == "xhr" and not headers.get("Content-Type"):
            headers["Content-Type"] = "application/json"

        try:
            masked_data = mask_sensitive_data(json.dumps(data))
            logger.debug(f"POST {url} - {masked_data}")

            if self._use_curl:
                response = await self._curl_post_json(url, data, headers)
            else:
                response = await self.client.post(url, json=data, headers=headers)

            # Check for session expiry indicators
            if not skip_reauth and (
                response.status_code == 302
                or ("redirectTo" in response.text and "code" not in response.text)
            ):
                await self._handle_session_expired()
                if self._use_curl:
                    response = await self._curl_post_json(url, data, headers)
                else:
                    response = await self.client.post(url, json=data, headers=headers)

            # Check for bot detection
            await self._check_suspicious_response(
                response.text, url=url, status_code=response.status_code
            )

            if response.status_code >= 400:
                if response.status_code == 429:
                    if self._stealth_enabled:
                        self._throttler.add_penalty(120.0)
                        logger.warning("429 Too Many Requests — adding 120s penalty")
                raise NetworkError(
                    f"HTTP {response.status_code}: {response.text}", response.status_code
                )

            self._stealth_post_request("json", response, fallback_url=url)

            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return {"response_text": response.text}

        except NetworkError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
                    logger.warning("429 Too Many Requests — adding 120s penalty")
            raise NetworkError(
                f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code
            )
        except httpx.RequestError as e:
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise  # Let tenacity see the original exception and retry
        except (ConnectionResetError, ConnectionError, OSError) as e:
            if not safe_to_retry:
                raise NetworkError(f"Connection reset (non-retryable): {e}")
            # Server forcibly closed connection (rate limit) — penalty + retry once
            if _retry < 1 and self._stealth_enabled:
                self._throttler.add_penalty(30.0)
                logger.warning("Connection reset in post_json — 30s penalty then retry: %s", e)
                await asyncio.sleep(30.0)
                return await self.post_json(
                    url,
                    data,
                    skip_reauth=skip_reauth,
                    safe_to_retry=safe_to_retry,
                    request_type=request_type,
                    _retry=_retry + 1,
                )
            raise NetworkError(f"Connection reset: {e}")
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                if not safe_to_retry:
                    raise NetworkError(f"Request failed (curl, non-retryable): {e}")
                raise  # Let tenacity retry
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise

    async def delete_json(
        self,
        url: str,
        *,
        data: Dict[str, Any] | None = None,
        skip_reauth: bool = False,
        safe_to_retry: bool = True,
        request_type: str = "json",
    ) -> Dict[str, Any]:
        """Make a DELETE request (JSON response expected).

        Args:
            data: Optional JSON body to include with the DELETE request.
            request_type: "json" (default) or "xhr" — see post_json.
        """
        if not url.startswith("http"):
            url = urljoin(self.base_url, url.lstrip("/"))

        rt = "xhr" if request_type == "xhr" else "json"
        headers = await self._stealth_pre_request(url, rt)
        if rt == "xhr" and data is not None and not headers.get("Content-Type"):
            headers["Content-Type"] = "application/json"

        try:
            logger.debug(f"DELETE {url}")

            if self._use_curl:
                response = await self._curl_delete_json(url, headers, data=data)
            else:
                if data is not None:
                    response = await self.client.request("DELETE", url, json=data, headers=headers)
                else:
                    response = await self.client.delete(url, headers=headers)

            # Check for session expiry indicators
            if not skip_reauth and (
                response.status_code == 302
                or ("redirectTo" in response.text and "code" not in response.text)
            ):
                await self._handle_session_expired()
                if self._use_curl:
                    response = await self._curl_delete_json(url, headers, data=data)
                else:
                    if data is not None:
                        response = await self.client.request(
                            "DELETE", url, json=data, headers=headers
                        )
                    else:
                        response = await self.client.delete(url, headers=headers)

            # Check for bot detection
            await self._check_suspicious_response(
                response.text, url=url, status_code=response.status_code
            )

            if response.status_code >= 400:
                if response.status_code == 429:
                    if self._stealth_enabled:
                        self._throttler.add_penalty(120.0)
                        logger.warning("429 Too Many Requests — adding 120s penalty")
                raise NetworkError(
                    f"HTTP {response.status_code}: {response.text}", response.status_code
                )

            self._stealth_post_request("json", response, fallback_url=url)

            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return {"response_text": response.text}

        except NetworkError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
                    logger.warning("429 Too Many Requests — adding 120s penalty")
            raise NetworkError(
                f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code
            )
        except httpx.RequestError as e:
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise  # Let tenacity retry
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                if not safe_to_retry:
                    raise NetworkError(f"Request failed (curl, non-retryable): {e}")
                raise  # Let tenacity retry
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise

    async def post_form(self, url: str, data: Dict[str, str], *, safe_to_retry: bool = True) -> str:
        """Make a POST request with form data."""
        if not url.startswith("http"):
            url = urljoin(self.base_url, url.lstrip("/"))

        headers = await self._stealth_pre_request(url, "form")

        try:
            form_str = urlencode(data)
            masked_form = mask_sensitive_data(form_str)
            logger.debug(f"POST {url} - {masked_form}")

            if self._use_curl:
                response = await self._curl_post_form(url, form_str, headers)
            else:
                response = await self.client.post(url, content=form_str.encode(), headers=headers)

            if response.status_code == 302:
                location = response.headers.get("Location") or response.headers.get("location", "")
                location_lower = location.lower()
                # Travian uses POST -> 302 -> GET (PRG pattern) for every
                # state-mutating endpoint. The 302 means the action already
                # succeeded server-side; re-posting after a re-auth dispatches
                # the action twice (the oasis-raider double-raid bug).
                # Only treat 302s pointing at a login/auth page as real
                # session expiry; otherwise follow the redirect with a GET.
                is_login_redirect = bool(location_lower) and (
                    "login" in location_lower
                    or ("auth" in location_lower and "code" not in location_lower)
                )
                if is_login_redirect:
                    logger.info(
                        "POST returned 302 to login (%s) — re-authenticating and retrying", location
                    )
                    await self._handle_session_expired()
                    if self._use_curl:
                        response = await self._curl_post_form(url, form_str, headers)
                    else:
                        response = await self.client.post(
                            url, content=form_str.encode(), headers=headers
                        )
                else:
                    target_url = (
                        location
                        if location.startswith("http")
                        else urljoin(self.base_url, location)
                    )
                    logger.debug("POST 302 PRG: GET %s", target_url)
                    # Real browsers issue the redirected GET as a fresh document
                    # navigation — they do NOT carry the form POST's
                    # Content-Type/Origin/Sec-Fetch-Dest=document headers across.
                    # Reusing form headers on the GET is a recognizable
                    # automation tell, so build page-load headers for the
                    # redirected GET instead.
                    if self._stealth_enabled:
                        redirect_headers = self._browser_headers.for_page_load(target_url)
                        redirect_headers["X-Version"] = await self._fetch_x_version()
                    else:
                        redirect_headers = {"X-Version": await self._fetch_x_version()}
                    if self._use_curl:
                        response = await self._curl_get(
                            target_url, redirect_headers, follow_redirects=True
                        )
                    else:
                        response = await self.client.get(
                            target_url, headers=redirect_headers, follow_redirects=True
                        )

            await self._check_suspicious_response(
                response.text, url=url, status_code=response.status_code
            )

            if response.status_code >= 400:
                if response.status_code == 429:
                    if self._stealth_enabled:
                        self._throttler.add_penalty(120.0)
                self._dump_http_error(
                    method="POST",
                    url=url,
                    status=response.status_code,
                    body=response.text,
                )
                raise NetworkError(
                    f"HTTP {response.status_code}: {response.text}", response.status_code
                )

            self._stealth_post_request("form", response, fallback_url=url)

            return response.text

        except NetworkError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
            self._dump_http_error(
                method="POST",
                url=url,
                status=e.response.status_code,
                body=e.response.text,
                error=str(e),
            )
            raise NetworkError(
                f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code
            )
        except httpx.RequestError as e:
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise  # Let tenacity see the original exception and retry
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                if not safe_to_retry:
                    raise NetworkError(f"Request failed (curl, non-retryable): {e}")
                raise  # Let tenacity retry
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        # Random-exponential backoff: avoids the textbook 1s, 2s, 4s, 8s
        # cadence (a recognizable bot signature) by sampling each wait
        # uniformly between 0 and 2^attempt — same expected throughput, no
        # power-of-two stripes in request timing.
        wait=wait_random_exponential(multiplier=0.8, max=12),
        retry=retry_if_exception_type(
            (httpx.RequestError, httpx.TimeoutException) + ((CurlError,) if HAS_CURL_CFFI else ())
        ),
    )
    async def get_html(
        self,
        url: str,
        follow_redirects: bool = True,
        *,
        skip_reauth: bool = False,
        safe_to_retry: bool = True,
    ) -> str:
        """Make a GET request and return HTML."""
        if not url.startswith("http"):
            url = urljoin(self.base_url, url.lstrip("/"))

        headers = await self._stealth_pre_request(url, "page")

        try:
            logger.debug(f"GET {url}")

            if self._use_curl:
                response = await self._curl_get(url, headers, follow_redirects=follow_redirects)
            else:
                response = await self.client.get(
                    url, headers=headers, follow_redirects=follow_redirects
                )

            # Check for session expiry
            response_url = str(response.url) if hasattr(response, "url") else url
            if not skip_reauth and (
                "login" in response_url.lower()
                or ("auth" in response_url.lower() and "code" not in response_url)
            ):
                await self._handle_session_expired()

                if self._use_curl:
                    response = await self._curl_get(url, headers, follow_redirects=follow_redirects)
                else:
                    response = await self.client.get(
                        url, headers=headers, follow_redirects=follow_redirects
                    )

            await self._check_suspicious_response(
                response.text, url=url, status_code=response.status_code
            )

            # Fail-closed: detect login/auth pages even when skip_reauth
            # is True.  This prevents callers (e.g., navigator._visit)
            # from recording a successful page visit when the server
            # actually returned a login redirect.
            if skip_reauth:
                resp_url = str(response.url) if hasattr(response, "url") else url
                if "login" in resp_url.lower() or (
                    "auth" in resp_url.lower() and "code" not in resp_url
                ):
                    logger.warning(
                        "Login page detected with skip_reauth=True: url=%s -> %s",
                        url,
                        resp_url,
                    )
                    debug_dumper.dump(
                        "session_expired",
                        response.text,
                        key="skip_reauth_login_redirect",
                        context={
                            "requested_url": url,
                            "landed_url": resp_url,
                            "status_code": response.status_code,
                        },
                    )
                    raise SessionExpiredError(f"Session expired (redirected to login): {resp_url}")

            if follow_redirects and response.status_code >= 400:
                if response.status_code == 429 and self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
                    logger.warning("429 Too Many Requests on GET — adding 120s penalty")
                self._dump_http_error(
                    method="GET",
                    url=url,
                    status=response.status_code,
                    body=response.text,
                )
                raise NetworkError(
                    f"HTTP {response.status_code}: {response.text}", response.status_code
                )

            self._stealth_post_request("page", response, fallback_url=url)

            return response.text

        except NetworkError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
            self._dump_http_error(
                method="GET",
                url=url,
                status=e.response.status_code,
                body=e.response.text,
                error=str(e),
            )
            raise NetworkError(
                f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code
            )
        except httpx.RequestError as e:
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise  # Let tenacity see the original exception and retry
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                if not safe_to_retry:
                    raise NetworkError(f"Request failed (curl, non-retryable): {e}")
                raise  # Let tenacity retry
            if not safe_to_retry:
                raise NetworkError(f"Request failed (non-retryable): {e}")
            raise

    async def _handle_session_expired(self) -> None:
        """Handle session expiry by calling re-auth callback."""
        if self._auth_callback:
            try:
                await self._auth_callback()
            except Exception as e:
                raise SessionExpiredError(f"Re-authentication failed: {e}")
        else:
            raise SessionExpiredError("Session expired and no re-auth callback set")


# Alias for backward compatibility
TravianClient = HttpClient
