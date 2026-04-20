"""Browser-accurate HTTP headers with proper Referer chains.

Real browsers send a consistent set of headers that vary by browser type.
This module generates headers that match what Chrome/Firefox/Edge would send
when browsing Travian, including proper Sec-Fetch-* headers and Referer chains.
"""

from __future__ import annotations

from typing import Dict, Optional
from .user_agents import UserAgentRotator
from .persona import Persona


class BrowserHeaders:
    """Generates realistic browser headers for each request type.

    Travian's anti-bot can check:
    - Missing or wrong Sec-Fetch-* headers
    - Missing Accept-Language
    - Missing or wrong Referer (should match navigation flow)
    - Inconsistent UA across requests
    """

    def __init__(self, ua_rotator: UserAgentRotator, base_url: str):
        self._ua = ua_rotator
        self._persona: Persona = ua_rotator.persona
        self._base_url = base_url.rstrip("/")
        self._last_page: Optional[str] = None  # tracks last visited page for Referer

    def update_last_page(self, path: str) -> None:
        """Update the last visited page (for Referer header)."""
        if path.startswith("http"):
            self._last_page = path
        else:
            self._last_page = f"{self._base_url}/{path.lstrip('/')}"

    def _sec_ch_headers(self) -> Dict[str, str]:
        """Return sec-ch-ua headers derived from the persona.

        Chromium browsers send these; Firefox does not.
        """
        if not self._persona.is_chromium:
            return {}
        return {
            "sec-ch-ua": self._persona.sec_ch_ua,
            "sec-ch-ua-mobile": self._persona.sec_ch_ua_mobile,
            "sec-ch-ua-platform": self._persona.sec_ch_ua_platform,
        }

    def for_page_load(self, path: str = "") -> Dict[str, str]:
        """Headers for a normal page navigation (GET request)."""
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }

        # Sec-Fetch headers (Chrome/Edge only, Firefox doesn't send all of them)
        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin" if self._last_page else "none"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-User"] = "?1"
            headers["Sec-Fetch-Dest"] = "document"
            headers.update(self._sec_ch_headers())

        # Referer — only if we've navigated before (first load has no referer)
        if self._last_page:
            headers["Referer"] = self._last_page

        return headers

    def for_json_post(self, path: str = "") -> Dict[str, str]:
        """Headers for an API POST (JSON body, e.g., GraphQL)."""
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
            "Origin": self._base_url,
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        return headers

    def for_form_post(self, path: str = "") -> Dict[str, str]:
        """Headers for a form POST (URL-encoded body)."""
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "keep-alive",
            "Origin": self._base_url,
            "Upgrade-Insecure-Requests": "1",
            "Cache-Control": "max-age=0",
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-User"] = "?1"
            headers["Sec-Fetch-Dest"] = "document"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        return headers

    def for_xhr(self, path: str = "") -> Dict[str, str]:
        """Headers for an XHR/fetch request (AJAX)."""
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "*/*",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "X-Requested-With": "XMLHttpRequest",
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        return headers
