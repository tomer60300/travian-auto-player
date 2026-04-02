"""HTTP client for Travian API with stealth anti-bot protection.

Integrates all stealth modules:
- Realistic browser User-Agent (per-session)
- Browser-accurate HTTP headers with Referer chains
- Global request rate limiting with burst detection
- Human-like delays between requests
- Page navigation simulation
- Session lifetime management
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, Optional, Union
from urllib.parse import urlencode, urljoin

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ..config import Settings
from ..exceptions import NetworkError, SessionExpiredError, TravianError
from ..utils.helpers import mask_sensitive_data

# Stealth imports
from ..stealth.user_agents import UserAgentRotator
from ..stealth.headers import BrowserHeaders
from ..stealth.throttler import RequestThrottler
from ..stealth.human_delay import HumanDelay, ActionType
from ..stealth.navigator import PageNavigator
from ..stealth.session_manager import SessionManager

logger = logging.getLogger(__name__)

# Response patterns that suggest bot detection
_BOT_DETECTION_PATTERNS = [
    r'captcha',
    r'bot.detected',
    r'automated.access',
    r'rate.limit',
    r'too.many.requests',
    r'access.denied',
    r'blocked',
]


class HttpClient:
    """HTTP client for Travian API with stealth anti-bot protection.
    
    When stealth mode is enabled (default), every request goes through:
    1. Rate limiting (throttler) — minimum gap between requests
    2. Human-like delay — action-appropriate random wait
    3. Realistic headers — browser-accurate User-Agent, Referer, Sec-Fetch-*
    4. Bot detection monitoring — watches responses for captcha/block signals
    """
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.base_url.rstrip('/')
        
        # ── Stealth components ──
        stealth_enabled = getattr(settings, 'stealth', True)
        stealth_speed = getattr(settings, 'stealth_speed', 1.0)
        
        self._ua_rotator = UserAgentRotator()
        self._browser_headers = BrowserHeaders(self._ua_rotator, self.base_url)
        
        self._throttler = RequestThrottler(
            min_gap_s=getattr(settings, 'stealth_min_gap', 1.5),
            max_gap_s=getattr(settings, 'stealth_max_gap', 3.0),
            burst_max_requests=getattr(settings, 'stealth_burst_max', 20),
            burst_cooldown_s=getattr(settings, 'stealth_burst_cooldown', 15.0),
            enabled=stealth_enabled,
        )
        
        self._human_delay = HumanDelay(
            speed_multiplier=stealth_speed,
            enabled=stealth_enabled,
        )
        
        self._navigator = PageNavigator(
            delay=self._human_delay,
            headers=self._browser_headers,
            enabled=stealth_enabled and getattr(settings, 'stealth_navigate', True),
        )
        
        self._session_manager = SessionManager(enabled=stealth_enabled)
        
        # Create HTTP client with cookie jar — using realistic UA from the start
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            headers={
                'User-Agent': self._ua_rotator.ua,
                'X-Version': settings.x_version,
            }
        )
        
        # Wire navigator back to this http_client (deferred to avoid circular init)
        self._navigator.set_http_client(self)
        
        self._auth_callback: Optional[callable] = None
        
        if stealth_enabled:
            logger.info(f"Stealth mode ON — UA: {self._ua_rotator.ua[:60]}... "
                       f"speed={stealth_speed}x gaps={self._throttler.min_gap_s}-{self._throttler.max_gap_s}s")
    
    # ── Public stealth accessors ──
    
    @property
    def navigator(self) -> PageNavigator:
        """Access the page navigator for pre-action navigation."""
        return self._navigator
    
    @property
    def human_delay(self) -> HumanDelay:
        """Access the human delay generator."""
        return self._human_delay
    
    @property
    def throttler(self) -> RequestThrottler:
        """Access the request throttler."""
        return self._throttler
    
    @property
    def session_manager(self) -> SessionManager:
        """Access the session manager."""
        return self._session_manager
    
    def set_auth_callback(self, callback: callable) -> None:
        """Set callback function to call when re-authentication is needed."""
        self._auth_callback = callback
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
    
    async def __aenter__(self):
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    def get_cookies(self) -> Dict[str, str]:
        cookies = {}
        for name in self.client.cookies:
            cookies[name] = self.client.cookies[name]
        return cookies
    
    def set_cookie(self, name: str, value: str) -> None:
        self.client.cookies.set(name, value)
    
    def clear_cookies(self) -> None:
        self.client.cookies.clear()
    
    # ── Core request methods with stealth integration ──
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def post_json(self, url: str, data: Dict[str, Any], *, skip_reauth: bool = False) -> Dict[str, Any]:
        """Make a POST request with JSON data (with stealth)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        # Stealth: throttle + headers
        await self._throttler.wait(f"POST JSON {url.split('/')[-1]}")
        headers = self._browser_headers.for_json_post(url)
        headers['X-Version'] = self.settings.x_version
        
        try:
            masked_data = mask_sensitive_data(json.dumps(data))
            logger.debug(f"POST {url} - {masked_data}")
            
            response = await self.client.post(url, json=data, headers=headers)
            
            # Bot detection check
            self._check_bot_detection(response.text, url)
            
            # Session expiry check
            if not skip_reauth and (response.status_code == 302 or (
                'redirectTo' in response.text and 'code' not in response.text
            )):
                await self._handle_session_expired()
                response = await self.client.post(url, json=data, headers=headers)
            
            response.raise_for_status()
            
            # Track page for Referer chain
            self._browser_headers.update_last_page(url)
            self._session_manager.record_request()
            
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"response_text": response.text}
                
        except httpx.HTTPStatusError as e:
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def post_form(self, url: str, data: Dict[str, str]) -> str:
        """Make a POST request with form data (with stealth)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        # Stealth: throttle + headers
        await self._throttler.wait(f"POST form {url.split('/')[-1]}")
        headers = self._browser_headers.for_form_post(url)
        headers['X-Version'] = self.settings.x_version
        
        try:
            form_str = urlencode(data)
            masked_form = mask_sensitive_data(form_str)
            logger.debug(f"POST {url} - {masked_form}")
            
            response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            # Bot detection check
            self._check_bot_detection(response.text, url)
            
            if response.status_code == 302:
                await self._handle_session_expired()
                response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            response.raise_for_status()
            
            self._browser_headers.update_last_page(url)
            self._session_manager.record_request()
            return response.text
            
        except httpx.HTTPStatusError as e:
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def get_html(self, url: str, follow_redirects: bool = True, *, skip_reauth: bool = False) -> str:
        """Make a GET request and return HTML (with stealth)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        # Stealth: throttle + headers
        await self._throttler.wait(f"GET {url.split('?')[0].split('/')[-1]}")
        headers = self._browser_headers.for_page_load(url)
        headers['X-Version'] = self.settings.x_version
        
        try:
            logger.debug(f"GET {url}")
            
            original_follow = self.client.follow_redirects
            self.client.follow_redirects = follow_redirects
            
            try:
                response = await self.client.get(url, headers=headers)
            finally:
                self.client.follow_redirects = original_follow
            
            # Bot detection check
            self._check_bot_detection(response.text, url)
            
            # Session expiry check
            if not skip_reauth and ('login' in response.url.path.lower() or (
                'auth' in response.url.path.lower() and 'code' not in str(response.url)
            )):
                await self._handle_session_expired()
                
                self.client.follow_redirects = follow_redirects
                try:
                    response = await self.client.get(url, headers=headers)
                finally:
                    self.client.follow_redirects = original_follow
            
            if not follow_redirects:
                return response.text
            else:
                response.raise_for_status()
                
                self._browser_headers.update_last_page(url)
                self._session_manager.record_request()
                return response.text
                
        except httpx.HTTPStatusError as e:
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    # ── Bot detection ──
    
    def _check_bot_detection(self, response_text: str, url: str) -> None:
        """Check response for signs of bot detection. Add penalty if detected."""
        if not response_text:
            return
        
        text_lower = response_text[:5000].lower()  # only check start of response
        
        for pattern in _BOT_DETECTION_PATTERNS:
            if re.search(pattern, text_lower):
                logger.warning(f"⚠️ Possible bot detection signal in response from {url}: "
                             f"matched pattern '{pattern}'")
                # Add a significant penalty to slow down
                self._throttler.add_penalty(30.0)
                break
    
    async def _handle_session_expired(self) -> None:
        """Handle session expiry by calling re-auth callback."""
        if self._auth_callback:
            try:
                # Small delay before re-auth (don't spam login)
                await self._human_delay.wait(ActionType.BETWEEN_TASKS, "session expired, re-authenticating")
                await self._auth_callback()
                # Rotate UA on re-auth (new session = potentially new browser)
                self._ua_rotator.rotate()
                self.client.headers['User-Agent'] = self._ua_rotator.ua
                self._navigator.clear_visited()
                self._session_manager.reset()
                logger.info(f"Re-authenticated with new UA: {self._ua_rotator.ua[:60]}...")
            except Exception as e:
                raise SessionExpiredError(f"Re-authentication failed: {e}")
        else:
            raise SessionExpiredError("Session expired and no re-auth callback set")


# Alias for backward compatibility
TravianClient = HttpClient
