"""HTTP client for Travian API with JWT authentication, retry logic, and stealth mode."""

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


class HttpClient:
    """HTTP client for Travian API with session management, retry logic, and stealth mode.
    
    When stealth mode is enabled (default), all requests go through:
    1. Request throttler (minimum gap between requests)
    2. Human-like delay (appropriate to action type)
    3. Realistic browser headers (UA, Sec-Fetch-*, Referer)
    4. Page navigation simulation (optional)
    """
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self.base_url = settings.base_url.rstrip('/')
        
        # Initialize stealth components
        self._stealth_enabled = settings.stealth
        self._init_stealth(settings)
        
        # Create HTTP client with cookie jar
        # Use the stealth UA if enabled, otherwise fall back to static UA
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
    def browser_headers(self) -> "BrowserHeaders":
        return self._browser_headers
    
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
        """
        if not self._stealth_enabled:
            return
        
        suspicious_patterns = [
            'captcha',
            'recaptcha',
            'bot-detection',
            'rate limit',
            'too many requests',
            'blocked',
            'banned',
            'suspicious activity',
        ]
        
        response_lower = response_text.lower()
        for pattern in suspicious_patterns:
            if pattern in response_lower:
                logger.warning(f"SUSPICIOUS RESPONSE detected: '{pattern}' found in response")
                # Add heavy penalty — slow way down
                self._throttler.add_penalty(60.0)
                break
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def post_json(self, url: str, data: Dict[str, Any], *, skip_reauth: bool = False) -> Dict[str, Any]:
        """Make a POST request with JSON data."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        headers = await self._stealth_pre_request(url, "json")
        
        try:
            masked_data = mask_sensitive_data(json.dumps(data))
            logger.debug(f"POST {url} - {masked_data}")
            
            response = await self.client.post(url, json=data, headers=headers)
            
            self._stealth_post_request(url)
            
            # Check for session expiry indicators
            if not skip_reauth and (response.status_code == 302 or (
                'redirectTo' in response.text and 'code' not in response.text
            )):
                await self._handle_session_expired()
                response = await self.client.post(url, json=data, headers=headers)
            
            # Check for bot detection
            self._check_suspicious_response(response.text)
            
            response.raise_for_status()
            
            try:
                return response.json()
            except json.JSONDecodeError:
                return {"response_text": response.text}
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Rate limited — add heavy penalty
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
                    logger.warning("429 Too Many Requests — adding 120s penalty")
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
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
            
            response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            self._stealth_post_request(url)
            
            if response.status_code == 302:
                await self._handle_session_expired()
                response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            self._check_suspicious_response(response.text)
            
            response.raise_for_status()
            return response.text
            
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def get_html(self, url: str, follow_redirects: bool = True, *, skip_reauth: bool = False) -> str:
        """Make a GET request and return HTML."""
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        headers = await self._stealth_pre_request(url, "page")
        
        try:
            logger.debug(f"GET {url}")
            
            original_follow = self.client.follow_redirects
            self.client.follow_redirects = follow_redirects
            
            try:
                response = await self.client.get(url, headers=headers)
            finally:
                self.client.follow_redirects = original_follow
            
            self._stealth_post_request(url)
            
            # Check for session expiry
            if not skip_reauth and ('login' in response.url.path.lower() or (
                'auth' in response.url.path.lower() and 'code' not in str(response.url)
            )):
                await self._handle_session_expired()
                
                self.client.follow_redirects = follow_redirects
                try:
                    response = await self.client.get(url, headers=headers)
                finally:
                    self.client.follow_redirects = original_follow
            
            self._check_suspicious_response(response.text)
            
            if not follow_redirects:
                return response.text
            else:
                response.raise_for_status()
                return response.text
                
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if self._stealth_enabled:
                    self._throttler.add_penalty(120.0)
            raise NetworkError(f"HTTP {e.response.status_code}: {e.response.text}", e.response.status_code)
        except httpx.RequestError as e:
            raise NetworkError(f"Request failed: {e}")
    
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
