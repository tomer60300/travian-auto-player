"""HTTP client for Travian API with JWT authentication and retry logic."""

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
    """HTTP client for Travian API with session management and retry logic."""
    
    def __init__(self, settings: Settings):
        """
        Initialize HTTP client.
        
        Args:
            settings: Application settings
        """
        self.settings = settings
        self.base_url = settings.base_url.rstrip('/')
        
        # Create HTTP client with cookie jar
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=settings.timeout,
            follow_redirects=False,  # We handle redirects manually for auth
            headers={
                'User-Agent': 'Travian-API/1.0',
                'X-Version': settings.x_version,
            }
        )
        
        self._auth_callback: Optional[callable] = None
    
    def set_auth_callback(self, callback: callable) -> None:
        """Set callback function to call when re-authentication is needed."""
        self._auth_callback = callback
    
    async def close(self) -> None:
        """Close the HTTP client."""
        await self.client.aclose()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
    
    def get_cookies(self) -> Dict[str, str]:
        """Get current cookies as dictionary."""
        cookies = {}
        for name in self.client.cookies:
            cookies[name] = self.client.cookies[name]
        return cookies
    
    def set_cookie(self, name: str, value: str) -> None:
        """Set a cookie."""
        self.client.cookies.set(name, value)
    
    def clear_cookies(self) -> None:
        """Clear all cookies."""
        self.client.cookies.clear()
    
    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type((httpx.RequestError, httpx.TimeoutException))
    )
    async def post_json(self, url: str, data: Dict[str, Any], *, skip_reauth: bool = False) -> Dict[str, Any]:
        """
        Make a POST request with JSON data.
        
        Args:
            url: URL to request (relative or absolute)
            data: Data to send as JSON
            skip_reauth: If True, skip session expiry detection (used during login flow)
            
        Returns:
            Response data as dictionary
            
        Raises:
            TravianError: On request failure
            SessionExpiredError: If session expired and re-auth needed
        """
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        headers = {
            'Content-Type': 'application/json',
            'X-Version': self.settings.x_version,
        }
        
        try:
            # Log request (mask sensitive data)
            masked_data = mask_sensitive_data(json.dumps(data))
            logger.debug(f"POST {url} - {masked_data}")
            
            response = await self.client.post(url, json=data, headers=headers)
            
            # Check for session expiry indicators (skip during login flow)
            if not skip_reauth and (response.status_code == 302 or (
                'redirectTo' in response.text and 'code' not in response.text
            )):
                await self._handle_session_expired()
                # Retry the request after re-auth
                response = await self.client.post(url, json=data, headers=headers)
            
            response.raise_for_status()
            
            # Parse JSON response
            try:
                return response.json()
            except json.JSONDecodeError:
                # Sometimes responses are not JSON
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
        """
        Make a POST request with form data.
        
        Args:
            url: URL to request (relative or absolute)
            data: Form data to send
            
        Returns:
            Response text
            
        Raises:
            TravianError: On request failure
        """
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Version': self.settings.x_version,
        }
        
        try:
            # Manually encode form body to avoid httpx double-encoding brackets
            form_str = urlencode(data)
            masked_form = mask_sensitive_data(form_str)
            logger.debug(f"POST {url} - {masked_form}")
            
            response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            # Check for session expiry — only on actual redirects, NOT on HTML pages
            # that happen to contain the word 'redirectTo' in their JS code
            if response.status_code == 302:
                await self._handle_session_expired()
                response = await self.client.post(url, content=form_str.encode(), headers=headers)
            
            response.raise_for_status()
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
        """
        Make a GET request and return HTML.
        
        Args:
            url: URL to request (relative or absolute)
            follow_redirects: Whether to follow redirects
            
        Returns:
            Response text (HTML)
            
        Raises:
            TravianError: On request failure
        """
        if not url.startswith('http'):
            url = urljoin(self.base_url, url.lstrip('/'))
        
        headers = {
            'X-Version': self.settings.x_version,
        }
        
        try:
            logger.debug(f"GET {url}")
            
            # Temporarily set redirect behavior
            original_follow = self.client.follow_redirects
            self.client.follow_redirects = follow_redirects
            
            try:
                response = await self.client.get(url, headers=headers)
            finally:
                # Restore original setting
                self.client.follow_redirects = original_follow
            
            # Check for session expiry (skip during login flow)
            if not skip_reauth and ('login' in response.url.path.lower() or (
                'auth' in response.url.path.lower() and 'code' not in str(response.url)
            )):
                await self._handle_session_expired()
                
                # Retry request
                self.client.follow_redirects = follow_redirects
                try:
                    response = await self.client.get(url, headers=headers)
                finally:
                    self.client.follow_redirects = original_follow
            
            if not follow_redirects:
                # For auth flow, don't raise on redirects
                return response.text
            else:
                response.raise_for_status()
                return response.text
                
        except httpx.HTTPStatusError as e:
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
