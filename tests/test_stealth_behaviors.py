"""Focused regression tests for stealth behavior."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _DummyResponse:
    def __init__(self, url: str):
        self.url = url


def test_page_context_tracks_documents_only():
    from travian_api.clients.http_client import HttpClient
    from travian_api.config import Settings

    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
    )
    try:
        assert client.browser_headers._last_page is None

        client._stealth_post_request(
            "json",
            _DummyResponse("https://ts2.x1.europe.travian.com/api/v1/graphql"),
        )
        assert client.browser_headers._last_page is None

        client._stealth_post_request(
            "page",
            _DummyResponse("https://ts2.x1.europe.travian.com/dorf1.php"),
        )
        assert client.browser_headers._last_page.endswith("/dorf1.php")

        client._stealth_post_request(
            "json",
            _DummyResponse("https://ts2.x1.europe.travian.com/api/v1/auth/login"),
        )
        assert client.browser_headers._last_page.endswith("/dorf1.php")

        client._stealth_post_request(
            "form",
            _DummyResponse("https://ts2.x1.europe.travian.com/build.php?gid=16&tt=2"),
        )
        assert client.browser_headers._last_page.endswith("/build.php?gid=16&tt=2")
    finally:
        asyncio.run(client.close())


def test_transient_rate_limit_is_soft_block_not_captcha():
    from travian_api.clients.http_client import HttpClient
    from travian_api.config import Settings

    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
    )
    try:
        start_penalty = client.throttler._penalty_until
        asyncio.run(
            client._check_suspicious_response(
                "temporarily unavailable",
                url="https://ts2.x1.europe.travian.com/api/v1/graphql",
                status_code=503,
            )
        )
        assert not client.captcha_guard.is_blocked
        assert client.throttler._penalty_until > start_penalty
    finally:
        asyncio.run(client.close())


def test_structural_captcha_still_blocks():
    from travian_api.clients.http_client import HttpClient
    from travian_api.config import Settings

    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
    )
    try:
        asyncio.run(
            client._check_suspicious_response(
                '<html><div class="g-recaptcha"></div></html>',
                url="https://ts2.x1.europe.travian.com/dorf1.php",
                status_code=200,
            )
        )
        assert client.captcha_guard.is_blocked
    finally:
        client.captcha_guard.resolve()
        asyncio.run(client.close())


def test_navigate_to_rally_point_fetches_document():
    from travian_api.stealth.human_delay import HumanDelay
    from travian_api.stealth.navigator import PageNavigator

    class FakeHttpClient:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get_html(self, url: str, skip_reauth: bool = True) -> str:
            self.urls.append(url)
            return "<html></html>"

    fake_http = FakeHttpClient()
    navigator = PageNavigator(fake_http, HumanDelay(enabled=False), enabled=True)

    asyncio.run(navigator.navigate_to_rally_point(village_id=123))

    assert fake_http.urls == [
        "/dorf2.php?newdid=123",
        "/build.php?gid=16&tt=2&newdid=123",
    ]
    assert navigator.current_page == "/build.php?gid=16&tt=2&newdid=123"
