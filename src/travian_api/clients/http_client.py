"""HTTP client for Travian API with JWT authentication, retry logic, and stealth mode.

Uses curl_cffi for Chrome TLS fingerprint impersonation when available,
falling back to httpx if curl_cffi is not installed.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, Optional, Union
from urllib.parse import urlencode, urljoin

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import logging

from ..config import Settings
from ..exceptions import NetworkError, SessionExpiredError, TravianError
from ..utils.helpers import mask_sensitive_data

logger = logging.getLogger(__name__)

# Try to import curl_cffi for Chrome TLS fingerprinting
try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession
    from curl_cffi.requests import Response as CurlResponse
    from curl_cffi import CurlError
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

    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.base_url.rstrip('/')

        # Initialize stealth components
        self._stealth_enabled = settings.stealth
        self._init_stealth(settings)

        # Determine transport mode
        self._use_curl = HAS_CURL_CFFI and settings.stealth
        self._curl_session: Optional[Any] = None  # CurlAsyncSession (lazy init)

        if not HAS_CURL_CFFI and settings.stealth:
            logger.warning(
                "curl_cffi not found — using httpx (TLS fingerprint will be detectable). "
                "Install with: pip install curl_cffi"
            )

        # Create httpx client (used as fallback or when stealth is off)
        ua = self._browser_headers.for_page_load().get("User-Agent", "Mozilla/5.0") if self._stealth_enabled else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            headers={
                'User-Agent': ua,
                'X-Version': settings.x_version,
            }
        )

        self._auth_callback: Optional[callable] = None

    def _init_stealth(self, settings: Settings) -> None:
        """Initialize stealth/anti-bot components."""
        from ..stealth.user_agents import UserAgentRotator
        from ..stealth.headers import BrowserHeaders
        from ..stealth.throttler import RequestThrottler
        from ..stealth.human_delay import HumanDelay
        from ..stealth.navigator import PageNavigator
        from ..stealth.noise import NoiseInjector
        from ..stealth.scheduler import ActivityScheduler

        self._ua_rotator = UserAgentRotator()
        self._browser_headers = BrowserHeaders(self._ua_rotator, settings.base_url)
        self._throttler = RequestThrottler(
            min_gap_s=settings.stealth_min_gap,
            max_gap_s=settings.stealth_max_gap,
            burst_max_requests=settings.stealth_burst_max,
            burst_cooldown_s=settings.stealth_burst_cooldown,
            enabled=settings.stealth,
        )
        self._human_delay = HumanDelay(
            speed_factor=settings.stealth_speed,
            enabled=settings.stealth,
        )
        self._navigator = PageNavigator(
            http_client=self,
            human_delay=self._human_delay,
            enabled=settings.stealth and settings.stealth_navigate,
        )
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
        )

    async def _ensure_curl_session(self) -> Any:
        """Lazy-create the curl_cffi session with Chrome impersonation."""
        if self._curl_session is None and HAS_CURL_CFFI:
            self._curl_session = CurlAsyncSession(
                impersonate="chrome",
            )
        return self._curl_session

    # ── Stealth accessors (for services to use) ──────────────────────

    @property
    def stealth_enabled(self) -> bool:
        return self._stealth_enabled

    @property
    def throttler(self) -> "RequestThrottler":
        return self._throttler

    @property
    def human_delay(self) -> "HumanDelay":
        return self._human_delay

    @property
    def navigator(self) -> "PageNavigator":
        return self._navigator

    @property
    def noise_injector(self) -> "NoiseInjector":
        return self._noise_injector

    @property
    def activity_scheduler(self) -> "ActivityScheduler":
        return self._activity_scheduler

    @property
    def browser_headers(self) -> "BrowserHeaders":
        return self._browser_headers

    def set_auth_callback(self, callback: callable) -> None:
        """Set callback function to call when re-authentication is needed."""
        self._auth_callback = callback

    async def close(self) -> None:
        """Close the HTTP client."""
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
        if not self._stealth_enabled:
            return {
                'Content-Type': 'application/json' if request_type == "json" else 'application/x-www-form-urlencoded' if request_type == "form" else '',
                'X-Version': self.settings.x_version,
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
        headers['X-Version'] = self.settings.x_version

        return headers

    def _stealth_post_request(self, url: str) -> None:
        """Update stealth state after a request."""
        if self._stealth_enabled:
            self._browser_headers.update_last_page(url)

    def _check_suspicious_response(self, response_text: str) -> None:
        """Check if server response indicates bot detection.

        Looks for captcha, rate limiting, or ban indicators.
        Adds throttle penalty if suspicious.

        IMPORTANT: Travian HTML contains words like "upgradeBlocked" (CSS class)
        and "blocked" in normal game contexts. We must only flag patterns that
        genuinely indicate anti-bot action, not normal game UI text.
        """
        if not self._stealth_enabled:
            return

        response_lower = response_text.lower()

        # Phase 1: High-confidence bot detection patterns (exact phrases)
        # These should NEVER appear in normal game HTML
        high_confidence_patterns = [
            'recaptcha',
            'bot-detection',
            'suspicious activity',
            'automated access',
            'your ip has been',
            'access denied',
        ]

        for pattern in high_confidence_patterns:
            if pattern in response_lower:
                logger.warning(f"BOT DETECTION: '{pattern}' found in response")
                self._throttler.add_penalty(120.0)
                return

        # Phase 2: Medium-confidence patterns — only flag if they appear
        # in a context that suggests anti-bot (not normal game UI)
        # "blocked" in game HTML = "upgradeBlocked" CSS class (normal)
        # "blocked" in error page = actual block (suspicious)

        # Check for captcha (but not in script/CSS references)
        if 'captcha' in response_lower:
            # Only flag if it looks like an actual captcha challenge, not a JS variable
            if re.search(r'<(form|div|iframe)[^>]*captcha', response_lower):
                logger.warning("BOT DETECTION: captcha form detected in response")
                self._throttler.add_penalty(120.0)
                return

        # HTTP 429 is handled separately in post_json/post_form/get_html
        # "too many requests" as page text (not in normal game HTML)
        if 'too many requests' in response_lower:
            # Check it's not inside game text / script
            if len(response_text) < 2000:  # error pages are short
                logger.warning("BOT DETECTION: 'too many requests' in short response (likely error page)")
                self._throttler.add_penalty(60.0)
                return

        # "banned" — only if it's the main page content, not a player name or chat
        if 'your account has been banned' in response_lower or 'you have been banned' in response_lower:
            logger.warning("BOT DETECTION: ban message detected")
            self._throttler.add_penalty(300.0)
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
            if hasattr(curl_response, 'cookies'):
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

    async def _curl_delete_json(self, url: str, headers: Dict[str, str]) -> Any:
        """Make a DELETE request via curl_cffi."""
        session = await self._ensure_curl_session()
        response = await session.delete(
            url,
            headers=headers,
            timeout=self.settings.timeout,
            allow_redirects=False,
        )
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

    async def _curl_get(self, url: str, headers: Dict[str, str], follow_redirects: bool = True) -> Any:
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

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException) + ((CurlError,) if HAS_CURL_CFFI else ()))
    )
    async def post_json(self, url: str, data: Dict[str, Any], *, skip_reauth: bool = False) -> Dict[str, Any]:
        """Make a POST request with JSON data."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))

        headers = await self._stealth_pre_request(url, "json")

        try:
            masked_data = mask_sensitive_data(json.dumps(data))
            logger.debug(f"POST {url} - {masked_data}")

            if self._use_curl:
                response = await self._curl_post_json(url, data, headers)
            else:
                response = await self.client.post(url, json=data, headers=headers)

            self._stealth_post_request(url)

            # Check for session expiry indicators
            if not skip_reauth and (response.status_code == 302 or (
                'redirectTo' in response.text and 'code' not in response.text
            )):
                await self._handle_session_expired()
                if self._use_curl:
                    response = await self._curl_post_json(url, data, headers)
                else:
                    response = await self.client.post(url, json=data, headers=headers)

            # Check for bot detection
            self._check_suspicious_response(response.text)

            if response.status_code >= 400:
                if response.status_code == 429:
                    if self._stealth_enabled:
                        self._throttler.add_penalty(120.0)
                        logger.warning("429 Too Many Requests — adding 120s penalty")
                raise NetworkError(f"HTTP {response.status_code}: {response.text}", response.status_code)

            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return {"response_text": response.text}

        except NetworkError:
            raise
        except (httpx.HTTPStatusError,) as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
                    logger.warning("429 Too Many Requests — adding 120s penalty")
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except (httpx.RequestError,) as e:
            raise NetworkError(f"Request failed: {e}")
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                raise NetworkError(f"Request failed (curl): {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException) + ((CurlError,) if HAS_CURL_CFFI else ()))
    )
    async def delete_json(self, url: str, *, skip_reauth: bool = False) -> Dict[str, Any]:
        """Make a DELETE request (JSON response expected)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))

        headers = await self._stealth_pre_request(url, "json")

        try:
            logger.debug(f"DELETE {url}")

            if self._use_curl:
                response = await self._curl_delete_json(url, headers)
            else:
                response = await self.client.delete(url, headers=headers)

            self._stealth_post_request(url)

            # Check for session expiry indicators
            if not skip_reauth and (response.status_code == 302 or (
                'redirectTo' in response.text and 'code' not in response.text
            )):
                await self._handle_session_expired()
                if self._use_curl:
                    response = await self._curl_delete_json(url, headers)
                else:
                    response = await self.client.delete(url, headers=headers)

            # Check for bot detection
            self._check_suspicious_response(response.text)

            if response.status_code >= 400:
                if response.status_code == 429:
                    if self._stealth_enabled:
                        self._throttler.add_penalty(120.0)
                        logger.warning("429 Too Many Requests — adding 120s penalty")
                raise NetworkError(f"HTTP {response.status_code}: {response.text}", response.status_code)

            try:
                return response.json()
            except (json.JSONDecodeError, ValueError):
                return {"response_text": response.text}

        except NetworkError:
            raise
        except (httpx.HTTPStatusError,) as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
                    logger.warning("429 Too Many Requests — adding 120s penalty")
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except (httpx.RequestError,) as e:
            raise NetworkError(f"Request failed: {e}")
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                raise NetworkError(f"Request failed (curl): {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException) + ((CurlError,) if HAS_CURL_CFFI else ()))
    )
    async def post_form(self, url: str, data: Dict[str, str]) -> str:
        """Make a POST request with form data."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))

        headers = await self._stealth_pre_request(url, "form")

        try:
            form_str = urlencode(data)
            masked_form = mask_sensitive_data(form_str)
            logger.debug(f"POST {url} - {masked_form}")

            if self._use_curl:
                response = await self._curl_post_form(url, form_str, headers)
            else:
                response = await self.client.post(url, content=form_str.encode(), headers=headers)

            self._stealth_post_request(url)

            if response.status_code == 302:
                await self._handle_session_expired()
                if self._use_curl:
                    response = await self._curl_post_form(url, form_str, headers)
                else:
                    response = await self.client.post(url, content=form_str.encode(), headers=headers)

            self._check_suspicious_response(response.text)

            if response.status_code >= 400:
                if response.status_code == 429:
                    if self._stealth_enabled:
                        self._throttler.add_penalty(120.0)
                raise NetworkError(f"HTTP {response.status_code}: {response.text}", response.status_code)

            return response.text

        except NetworkError:
            raise
        except (httpx.HTTPStatusError,) as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except (httpx.RequestError,) as e:
            raise NetworkError(f"Request failed: {e}")
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                raise NetworkError(f"Request failed (curl): {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException) + ((CurlError,) if HAS_CURL_CFFI else ()))
    )
    async def get_html(self, url: str, follow_redirects: bool = True, *, skip_reauth: bool = False) -> str:
        """Make a GET request and return HTML."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))

        headers = await self._stealth_pre_request(url, "page")

        try:
            logger.debug(f"GET {url}")

            if self._use_curl:
                response = await self._curl_get(url, headers, follow_redirects=follow_redirects)
            else:
                original_follow = self.client.follow_redirects
                self.client.follow_redirects = follow_redirects
                try:
                    response = await self.client.get(url, headers=headers)
                finally:
                    self.client.follow_redirects = original_follow

            self._stealth_post_request(url)

            # Check for session expiry
            response_url = str(response.url) if hasattr(response, 'url') else url
            if not skip_reauth and ('login' in response_url.lower() or (
                'auth' in response_url.lower() and 'code' not in response_url
            )):
                await self._handle_session_expired()

                if self._use_curl:
                    response = await self._curl_get(url, headers, follow_redirects=follow_redirects)
                else:
                    self.client.follow_redirects = follow_redirects
                    try:
                        response = await self.client.get(url, headers=headers)
                    finally:
                        self.client.follow_redirects = original_follow

            self._check_suspicious_response(response.text)

            if follow_redirects and response.status_code >= 400:
                raise NetworkError(f"HTTP {response.status_code}: {response.text}", response.status_code)

            return response.text

        except NetworkError:
            raise
        except (httpx.HTTPStatusError,) as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except (httpx.RequestError,) as e:
            raise NetworkError(f"Request failed: {e}")
        except Exception as e:
            if HAS_CURL_CFFI and isinstance(e, CurlError):
                raise NetworkError(f"Request failed (curl): {e}")
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
