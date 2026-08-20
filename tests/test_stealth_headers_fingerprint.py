"""Header-fingerprint invariants that a byte-for-byte Chrome diff would check.

These pin the Cluster-1 hardening: no HTTP/2-forbidden Connection header, Origin
on every non-GET fetch, no Cache-Control on link-click navigations, X-Version
only on API calls (never document navigations), and a version resolver that
handles real gpack path shapes instead of silently pinning a stale constant.
"""

import asyncio

from travian_api.clients.http_client import HttpClient, extract_x_version
from travian_api.config import Settings


def _client(x_version: str = "389") -> HttpClient:
    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
            x_version=x_version,
        )
    )
    return client


class TestHeaderBuilders:
    def setup_method(self):
        self.h = _client()._browser_headers

    def test_no_connection_header_anywhere(self):
        # Forbidden hop-by-hop header on HTTP/2 (RFC 9113); Chrome never sends it.
        for build in (
            self.h.for_page_load,
            self.h.for_form_post,
            self.h.for_xhr,
            self.h.for_json_post,
        ):
            assert "Connection" not in build(), build.__name__

    def test_xhr_and_json_carry_origin(self):
        assert self.h.for_xhr()["Origin"] == "https://ts2.x1.europe.travian.com"
        assert self.h.for_json_post()["Origin"] == "https://ts2.x1.europe.travian.com"

    def test_navigations_have_no_cache_control(self):
        # Chrome sends Cache-Control only on reloads, not the link clicks the
        # navigator simulates.
        assert "Cache-Control" not in self.h.for_page_load()
        assert "Cache-Control" not in self.h.for_form_post()


class TestXVersionPlacement:
    def _headers_for(self, request_type: str) -> dict:
        client = _client()

        async def _no_throttle(context=""):
            return 0.0

        client._throttler.wait = _no_throttle
        return asyncio.run(client._stealth_pre_request("/whatever", request_type))

    def test_api_calls_carry_x_version(self):
        assert "X-Version" in self._headers_for("json")
        assert "X-Version" in self._headers_for("xhr")

    def test_document_navigations_do_not(self):
        # A real browser never sends X-Version on a page load or a form POST;
        # it is an AJAX-wrapper header. This is the highest-volume request class.
        assert "X-Version" not in self._headers_for("page")
        assert "X-Version" not in self._headers_for("form")


class TestVersionResolver:
    def test_bare_numeric_gpack_path(self):
        assert extract_x_version('src="/gpack/389/js/app.js"') == "389"

    def test_dotted_version(self):
        assert extract_x_version('href="/gpack/4030.9/css/main.css"') == "4030.9"

    def test_themed_gpack_path_still_finds_the_version(self):
        # The shape the old numeric-only regex missed → silent stale pin.
        assert extract_x_version('"/gpack/tichi_hdr-4031.2/img/x.png"') == "4031.2"

    def test_travian_version_global(self):
        assert extract_x_version('window.Travian.version = "4031";') == "4031"

    def test_meta_version_tag(self):
        assert extract_x_version('<meta name="x-version" content="4032.1">') == "4032.1"

    def test_no_marker_returns_none(self):
        assert extract_x_version("<html><body>nothing here</body></html>") is None

    def test_a_non_version_token_is_not_mistaken(self):
        # A themed path with no numeric version must not yield a garbage value.
        assert extract_x_version('"/gpack/tichi_theme/img/x.png"') is None
