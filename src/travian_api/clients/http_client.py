"""HTTP client for Travian API with JWT authentication, retry logic, and stealth mode.

Stealth mode adds:
- Realistic browser User-Agent (per session)
- Browser-accurate HTTP headers with Sec-Fetch-*, Referer chains
- Global request rate limiting with burst detection
- Random human-like delays between requests
- Session activity tracking
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
from ..stealth.user_agents import UserAgentRotator
from ..stealth.headers import BrowserHeaders
from ..stealth.throttler import RequestThrottler
from ..stealth.human_delay import HumanDelay
from ..stealth.session_manager import SessionManager

logger = logging.getLogger(__name__)


class HttpClient:
    """HTTP client for Travian API with session management, retry logic, and stealth."""
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.base_url.rstrip('/')
        
        # ── Stealth components ──
        stealth_on = getattr(settings, 'stealth', True)
        speed = getattr(settings, 'stealth_speed', 1.0)
        
        self._ua_rotator = UserAgentRotator()
        self._browser_headers = BrowserHeaders(self._ua_rotator, self.base_url)
        self._throttler = RequestThrottler(
            min_gap_s=getattr(settings, 'stealth_min_gap', 1.5),
            max_gap_s=getattr(settings, 'stealth_max_gap', 3.0),
            burst_max_requests=getattr(settings, 'stealth_burst_max', 20),
            burst_cooldown_s=getattr(settings, 'stealth_burst_cooldown', 15.0),
            enabled=stealth_on,
        )
        self._delay = HumanDelay(
            speed_factor=speed,
            enabled=stealth_on,
        )
        self._session_mgr = SessionManager(enabled=stealth_on)
        self._stealth_enabled = stealth_on
        
        # Create HTTP client with cookie jar — use realistic UA
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.timeout,
            follow_redirects=False,
            headers={
                'User-Agent': self._ua_rotator.ua,
                'X-Version': settings.x_version,
            }
        )
        
        self._auth_callback: Optional[callable] = None
    
    # ── Public stealth accessors ──
    
    @property
    def throttler(self) -> RequestThrottler:
        """Access the request throttler (for external penalty/burst checks)."""
        return self._throttler
    
    @property
    def delay(self) -> HumanDelay:
        """Access the human delay generator."""
        return self._delay
    
    @property
    def session_manager(self) -> SessionManager:
        """Access the session manager."""
        return self._session_mgr
    
    @property
    def browser_headers(self) -> BrowserHeaders:
        """Access browser headers (for Referer tracking)."""
        return self._browser_headers
    
    @property
    def stealth_enabled(self) -> bool:
        return self._stealth_enabled
    
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
    
    # ── Stealth-aware request methods ──
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def post_json(self, url: str, data: Dict[str, Any], *, skip_reauth: bool = False) -> Dict[str, Any]:
        """Make a POST request with JSON data (stealth-aware)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        # Stealth: throttle + browser headers
        await self._throttler.wait(context=f"POST JSON {url.split('/')[-1]}")
        headers = self._browser_headers.for_json_post(url) if self._stealth_enabled else {
            'Content-Type': 'application/json',
            'X-Version': self.settings.x_version,
        }
        headers['X-Version'] = self.settings.x_version
        
        self._session_mgr.record_action()
        
        try:
            masked_data = mask_sensitive_data(json.dumps(data))
            logger.debug(f"POST {url} - {masked_data}")
            
            response = await self.client.post(url, json=data, headers=headers)
            
            # Check for captcha/bot detection indicators
            self._check_bot_detection(response)
            
            if not skip_reauth and (response.status_code == 302 or (
                'redirectTo' in response.text and 'code' not in response.text
            )):
                await self._handle_session_expired()
                response = await self.client.post(url, json=data, headers=headers)
            
            response.raise_for_status()
            
            # Update Referer tracking
            self._browser_headers.update_last_page(url)
            
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"response_text": response.text}
                
        except httpx.HTTPStatusError as e:
            self._handle_http_error(e)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def post_form(self, url: str, data: Dict[str, str]) -> str:
        """Make a POST request with form data (stealth-aware)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        # Stealth: throttle + browser headers
        await self._throttler.wait(context=f"POST form {url.split('/')[-1]}")
        headers = self._browser_headers.for_form_post(url) if self._stealth_enabled else {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Version': self.settings.x_version,
        }
        headers['X-Version'] = self.settings.x_version
        
        self._session_mgr.record_action()
        
        try:
            form_str = urlencode(data)
            masked_form = mask_sensitive_data(form_str)
            logger.debug(f"POST {url} - {masked_form}")
            
            response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            self._check_bot_detection(response)
            
            if response.status_code == 302:
                await self._handle_session_expired()
                response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            response.raise_for_status()
            
            self._browser_headers.update_last_page(url)
            return response.text
            
        except httpx.HTTPStatusError as e:
            self._handle_http_error(e)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def get_html(self, url: str, follow_redirects: bool = True, *, skip_reauth: bool = False) -> str:
        """Make a GET request and return HTML (stealth-aware)."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        # Stealth: throttle + browser headers
        await self._throttler.wait(context=f"GET {url.split('?')[0].split('/')[-1]}")
        headers = self._browser_headers.for_page_load(url) if self._stealth_enabled else {
            'X-Version': self.settings.x_version,
        }
        headers['X-Version'] = self.settings.x_version
        
        self._session_mgr.record_action()
        
        try:
            logger.debug(f"GET {url}")
            
            original_follow = self.client.follow_redirects
            self.client.follow_redirects = follow_redirects
            
            try:
                response = await self.client.get(url, headers=headers)
            finally:
                self.client.follow_redirects = original_follow
            
            self._check_bot_detection(response)
            
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
                self._browser_headers.update_last_page(url)
                return response.text
            else:
                response.raise_for_status()
                self._browser_headers.update_last_page(url)
                return response.text
                
        except httpx.HTTPStatusError as e:
            self._handle_http_error(e)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    # ── Bot detection & error handling ──
    
    def _check_bot_detection(self, response: httpx.Response) -> None:
        """Check response for bot detection indicators and add penalties."""
        text = response.text[:2000] if response.text else ""
        
        # Captcha detection
        if any(marker in text.lower() for marker in [
            'captcha', 'recaptcha', 'robot', 'bot detection',
            'automated access', 'unusual activity',
        ]):
            logger.warning("⚠️ BOT DETECTION: Captcha or bot challenge detected!")
            self._throttler.add_penalty(120.0)  # 2 min cooldown
        
        # Rate limiting (429)
        if response.status_code == 429:
            logger.warning("⚠️ RATE LIMITED: Server returned 429")
            retry_after = response.headers.get('Retry-After', '60')
            try:
                penalty = float(retry_after) + 30
            except ValueError:
                penalty = 90.0
            self._throttler.add_penalty(penalty)
        
        # Suspicious redirect patterns
        if response.status_code == 403:
            logger.warning("⚠️ FORBIDDEN: Server returned 403 — possible IP ban")
            self._throttler.add_penalty(300.0)  # 5 min cooldown
    
    def _handle_http_error(self, error: httpx.HTTPStatusError) -> None:
        """Add throttle penalties on server errors."""
        status = error.response.status_code
        if status >= 500:
            # Server error — back off
            self._throttler.add_penalty(30.0)
        elif status == 429:
            self._throttler.add_penalty(90.0)
    
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
