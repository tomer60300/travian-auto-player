"""Browser-accurate HTTP headers with proper Referer chains.

Real browsers send a consistent set of headers that vary by browser type.
This module generates headers that match what Chrome/Firefox/Edge would send
when browsing Travian, including proper Sec-Fetch-* headers and Referer chains.
"""

from __future__ import annotations

from typing import Dict, Optional

from .persona import Persona
from .user_agents import UserAgentRotator

# Headers curl-impersonate injects from its own Chrome default block, which are
# correct for a NAVIGATION and impossible on a fetch/XHR. Mapping a name to None
# makes curl_cffi emit "name:" on the wire, which deletes it; the httpx path
# strips these keys instead, since httpx adds nothing of its own.
#
# Verified on the wire, not assumed: with our XHR dict and no suppression,
# curl-impersonate sent `Upgrade-Insecure-Requests: 1` and `Sec-Fetch-User: ?1`
# alongside `Sec-Fetch-Mode: cors` and `Sec-Fetch-Dest: empty` -- a combination
# Chrome is structurally incapable of producing, on every API request the app
# makes. curl_easy_impersonate applies its defaults AFTER CurlOpt.HTTPHEADER, so
# our dict could override values but never remove keys, which is why this was
# invisible from Python.
_NAVIGATION_ONLY_HEADERS = {
    "Sec-Fetch-User": None,
    "Upgrade-Insecure-Requests": None,
}

# Fetch/XHR priority. curl-impersonate's default is the DOCUMENT priority
# `u=0, i`; a captured real client request carried `u=1, i`.
_SUBRESOURCE_PRIORITY = "u=1, i"


def _as_subresource(headers: Dict[str, str]) -> Dict[str, Optional[str]]:
    """Strip the navigation-only headers curl-impersonate would re-add."""
    return {**headers, **_NAVIGATION_ONLY_HEADERS, "Priority": _SUBRESOURCE_PRIORITY}


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

    def _accept_encoding(self) -> str:
        """Accept-Encoding string matching the persona's browser capabilities.

        Chromium 124+ stable advertises zstd alongside the older codecs.
        Sending only "gzip, deflate, br" while claiming a recent Chrome UA
        is a subtle fingerprint mismatch — capability strings are checked
        against UA in modern bot detectors.
        """
        if self._persona.is_chromium:
            return "gzip, deflate, br, zstd"
        return "gzip, deflate, br"

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
        """Headers for a normal page navigation (GET request).

        No ``Connection`` header: the curl_cffi transport negotiates HTTP/2,
        on which ``Connection``/``Keep-Alive`` are forbidden hop-by-hop headers
        (RFC 9113 §8.2.2) that Chrome never emits on an h2 stream — sending one
        is an immediate header-analysis tell. No ``Cache-Control: max-age=0``
        either: Chrome sends that only on a reload or address-bar navigation,
        NOT on the link clicks the navigator simulates, so putting it on every
        navigation is a uniform-behavior tell.
        """
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": self._accept_encoding(),
            "Upgrade-Insecure-Requests": "1",
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

    def for_json_post(self, path: str = "") -> Dict[str, Optional[str]]:
        """Headers for an API POST (JSON body, e.g., GraphQL)."""
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": self._accept_encoding(),
            "Content-Type": "application/json",
            "Origin": self._base_url,
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        return _as_subresource(headers)

    def for_form_post(self, path: str = "") -> Dict[str, Optional[str]]:
        """Headers for a form POST (URL-encoded body)."""
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": self._accept_encoding(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self._base_url,
            "Upgrade-Insecure-Requests": "1",
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-User"] = "?1"
            headers["Sec-Fetch-Dest"] = "document"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        # NOT _as_subresource: a form POST is a document navigation in Travian's
        # flow (it answers with a PRG redirect to a page), so
        # Upgrade-Insecure-Requests and Sec-Fetch-User are exactly what a
        # browser sends here. Its Sec-Fetch-Dest is `document`, which is how
        # the wire test tells the two classes apart.
        return headers

    def for_fetch(self, path: str = "") -> Dict[str, Optional[str]]:
        """Headers for a plain ``fetch()`` call, matching a captured request.

        This is the shape a real client sends to ``/api/v1/*``, verified against
        a Europe 2 capture (gpack 597.6). It differs from :meth:`for_xhr` by
        exactly one header -- no ``X-Requested-With``, because ``fetch`` never
        adds it -- and from :meth:`for_json_post` by its ``Accept``, which is
        the ``*/*`` fetch default rather than axios's
        ``application/json, text/plain, */*``. Neither existing shape matched,
        which is why this one exists.

        It also carries no ``X-Version``. The capture's header list has none,
        and a custom header the real request does not send is a fingerprint
        wherever it appears -- so the caller decides, and for this traffic the
        answer is no. ``Content-Type`` is likewise the caller's to set, since
        only it knows the body.
        """
        headers = {
            "User-Agent": self._ua.ua,
            # The fetch default. axios would send
            # "application/json, text/plain, */*"; the captured client did not.
            "Accept": "*/*",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": self._accept_encoding(),
            "Origin": self._base_url,
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        return _as_subresource(headers)

    def for_xhr(self, path: str = "") -> Dict[str, Optional[str]]:
        """Headers for a legacy-XHR request (the endpoints Travian's client
        drives via XMLHttpRequest, which set ``X-Requested-With``).

        ``Origin`` + ``Sec-Fetch-*`` + ``X-Requested-With`` coexist on purpose:
        a real jQuery/XHR POST carries all three — the browser adds Origin (on
        any same-origin non-GET) and Sec-Fetch-* to every request, and the page
        sets X-Requested-With. This is NOT the plain-``fetch()`` shape (fetch
        never adds X-Requested-With) — that traffic uses ``for_json_post``
        instead. Dropping Origin here would leave an XHR POST without the Origin
        Chrome always sends. No ``Connection`` header for the h2 reason above.
        """
        headers = {
            "User-Agent": self._ua.ua,
            "Accept": "*/*",
            "Accept-Language": self._persona.accept_language,
            "Accept-Encoding": self._accept_encoding(),
            "Origin": self._base_url,
            "X-Requested-With": "XMLHttpRequest",
        }

        if not self._ua.is_firefox:
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
            headers.update(self._sec_ch_headers())

        if self._last_page:
            headers["Referer"] = self._last_page

        return _as_subresource(headers)
