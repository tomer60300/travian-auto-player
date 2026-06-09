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


def test_throttler_gap_is_right_skewed_not_uniform():
    """Inter-request gaps must be heavy-tailed, not a flat uniform band.

    A uniform draw over ``[min, max]`` produces the flat gap histogram that a
    KS test against real human traffic flags as automation. The shifted
    log-normal must keep the floor, skew right, and have an occasional tail.
    """
    import random

    from travian_api.stealth.throttler import RequestThrottler

    # Seed BEFORE constructing: the per-session gap-shape params are drawn in
    # __init__, so seeding first makes the whole sample deterministic.
    random.seed(1234)
    throttler = RequestThrottler(min_gap_s=1.0, max_gap_s=2.5)

    samples = [throttler._sample_gap() for _ in range(20000)]
    n = len(samples)
    ordered = sorted(samples)
    mean = sum(samples) / n
    median = ordered[n // 2]

    # Floor preserved: a gap is never below the configured minimum, so no
    # spike piles up below min_gap_s.
    assert min(samples) >= 1.0
    # Tail soft-capped so a single draw can't stall a loop.
    assert max(samples) <= 2.5 * 3.0
    # Right-skewed: a uniform distribution has mean == median; ours does not.
    assert mean > median + 0.02
    # Body stays in the lower half of the configured band (median fraction is
    # drawn from [0.30, 0.48], so median in [1.45, 1.72]).
    assert 1.0 < median < 1.75
    # Heavy tail exists but is a minority — not the ~50% a uniform band over a
    # wider range would give.
    over_max = sum(1 for s in samples if s > 2.5) / n
    assert 0.0 < over_max < 0.30


def test_throttler_gap_shape_is_persona_stable_and_account_distinct():
    """Gap shape must be stable per account across restarts, distinct between accounts.

    Unseeded throttlers draw a fresh shape each time (so two accounts on the
    same config don't share one shape). Seeding with a persona-stable identity
    makes the shape deterministic for that account — no cross-session drift a
    two-sample KS test could catch — while a different identity yields a
    different shape.
    """
    from travian_api.stealth.throttler import RequestThrottler

    def shape(identity: str) -> tuple[float, float]:
        t = RequestThrottler(min_gap_s=1.0, max_gap_s=2.5)
        t.seed_gap_shape(identity)
        return (t._gap_median_frac, t._gap_sigma)

    account_a = "Mozilla/5.0 Chrome/133|en-US,en;q=0.9|https://ts2.x1.europe.travian.com"
    account_b = "Mozilla/5.0 Chrome/131|de-DE,de;q=0.9|https://ts1.x1.travian.de"

    # Stable across restarts: same identity -> identical shape.
    assert shape(account_a) == shape(account_a)
    # Distinct between accounts.
    assert shape(account_a) != shape(account_b)
    # Within documented bounds.
    frac, sigma = shape(account_a)
    assert 0.30 <= frac <= 0.48
    assert 0.45 <= sigma <= 0.85
