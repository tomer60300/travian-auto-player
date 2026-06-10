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


def test_scheduler_caps_are_jittered_below_hard_ceiling():
    """Effective stop caps must vary below the configured max, never exceed it.

    A fixed cap makes every limit-hitting session exactly ``max_continuous_hours``
    long and every capped day exactly ``max_daily_hours`` — a sharp spike in the
    session-length / daily-total histogram. Jittered effective caps smear that
    spike while never letting the bot work *longer* than the safety ceiling.
    """
    import random

    from travian_api.stealth.scheduler import ActivityScheduler

    random.seed(7)
    s = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0)

    # Continuous cap re-jitters every session.
    cont_caps = set()
    for _ in range(200):
        s.start_session()
        cont_caps.add(s._effective_continuous_hours)

    # Never exceed the hard ceiling (safety invariant); stay in band; vary.
    assert max(cont_caps) <= 6.0
    assert min(cont_caps) >= 6.0 * 0.80
    assert len(cont_caps) > 50

    # The daily sampler likewise stays in band and varies.
    daily = [s._sample_daily_cap() for _ in range(200)]
    assert max(daily) <= 16.0
    assert min(daily) >= 16.0 * 0.85
    assert len(set(daily)) > 50


def test_scheduler_daily_cap_resamples_per_day_not_per_session():
    """Daily cap is stable within a day (no upward order-statistic drift)."""
    import random

    from travian_api.stealth.scheduler import ActivityScheduler

    random.seed(9)
    s = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0)
    first = s._effective_daily_hours

    # Many same-day sessions keep the daily cap fixed.
    for _ in range(20):
        s.start_session()
        assert s._effective_daily_hours == first

    # A day boundary triggers a resample.
    s._daily_cap_day = "1970-01-01"
    s.start_session()
    assert s._daily_cap_day != "1970-01-01"


def test_scheduler_rejects_nonfinite_persisted_caps(tmp_path):
    """A corrupt state file with nan/inf caps must not disable the safety gate."""
    import json
    import math

    from travian_api.stealth.scheduler import ActivityScheduler

    state_file = tmp_path / ".scheduler_state.json"
    state_file.write_text(
        json.dumps(
            {
                "hourly_buckets": {},
                "session_seconds": 0.0,
                "effective_continuous_hours": float("nan"),
                "effective_daily_hours": float("inf"),
                "last_saved": 0,
            }
        ),
        encoding="utf-8",
    )

    s = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0, state_file=state_file)
    # Fell back to the freshly sampled in-band defaults, not nan/inf.
    assert math.isfinite(s._effective_continuous_hours)
    assert 0.0 < s._effective_continuous_hours <= 6.0
    assert math.isfinite(s._effective_daily_hours)
    assert 0.0 < s._effective_daily_hours <= 16.0


def test_scheduler_caps_survive_persistence(tmp_path):
    """Jittered caps persist so a same-day restart stays consistent."""
    import random

    from travian_api.stealth.scheduler import ActivityScheduler

    state_file = tmp_path / ".scheduler_state.json"

    random.seed(11)
    a = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0, state_file=state_file)
    a.start_session()
    a.log_activity(60.0)  # forces a state write
    a._save_state_force()
    saved_cont = a._effective_continuous_hours
    saved_daily = a._effective_daily_hours

    b = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0, state_file=state_file)
    assert b._effective_continuous_hours == saved_cont
    assert b._effective_daily_hours == saved_daily

    # A lowered config clamps a stale persisted cap to the new hard ceiling.
    c = ActivityScheduler(max_continuous_hours=3.0, max_daily_hours=8.0, state_file=state_file)
    assert c._effective_continuous_hours <= 3.0
    assert c._effective_daily_hours <= 8.0


class _RecordingHttp:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        self.urls.append(url)
        return "<html></html>"


def test_warmup_transition_matrix_is_persona_stable_and_distinct():
    """The warm-up Markov matrix must be stable per account, distinct across.

    A fixed skeleton (or pure per-call randomness, which makes every account's
    transition distribution identical) is clusterable by transition-count
    chi-square. A persona-seeded matrix gives each account a stable-but-distinct
    chain. Each row is a valid probability distribution (sums to 1, includes a
    stop outcome and a small self-loop — a hard-zero diagonal is itself a tell).
    """
    from travian_api.stealth.human_delay import HumanDelay
    from travian_api.stealth.navigator import _WARMUP_MAX_STEPS, _WARMUP_PAGES, PageNavigator

    def nav_for(identity: str) -> PageNavigator:
        nav = PageNavigator(_RecordingHttp(), HumanDelay(enabled=False), enabled=True)
        nav.seed_routes(identity)
        return nav

    a = "Chrome/133|en-US|https://ts2.x1.europe.travian.com|saltAAAA"
    b = "Chrome/131|de-DE|https://ts1.x1.travian.de|saltBBBB"

    assert nav_for(a)._route_transitions == nav_for(a)._route_transitions  # stable
    assert nav_for(a)._route_transitions != nav_for(b)._route_transitions  # distinct

    nav = nav_for(a)
    for frm in _WARMUP_PAGES:
        row = nav._route_transitions[frm]
        assert abs(sum(row.values()) - 1.0) < 1e-9  # valid distribution
        assert None in row  # a stop outcome exists
        # Self-loop is present (no structural zero) but a minority outcome.
        assert frm in row and 0.0 <= row[frm] < 0.5
    # Per-account browse-length cap is within the absolute bound.
    assert 4 <= nav._route_max_steps <= _WARMUP_MAX_STEPS


def test_warmup_route_is_varied_bounded_and_coherent():
    """Warm-up must vary, stay bounded, and never make an impossible jump."""
    import random

    from travian_api.stealth.human_delay import HumanDelay
    from travian_api.stealth.navigator import _WARMUP_MAX_STEPS, PageNavigator

    allowed = {"/dorf1.php", "/dorf2.php", "/statistiken.php", "/spieler.php", "/karte.php"}

    random.seed(2024)
    sequences = []
    dorf2_present = 0
    n = 300
    for _ in range(n):
        http = _RecordingHttp()
        nav = PageNavigator(http, HumanDelay(enabled=False), enabled=True)
        nav.seed_routes("Chrome/133|en-US|https://ts2.x1.europe.travian.com|saltAAAA")
        asyncio.run(nav.warm_up(village_id=7))

        seq = tuple(http.urls)
        sequences.append(seq)
        # Always lands on dorf1 first (no login -> immediate API blast).
        assert seq[0].startswith("/dorf1.php")
        # Bounded: initial dorf1 + at most _WARMUP_MAX_STEPS more pages.
        assert 1 <= len(seq) <= 1 + _WARMUP_MAX_STEPS
        # Every visited page is a coherent top-level page (no impossible jump).
        for url in seq:
            assert url.split("?")[0] in allowed
        if any(u.startswith("/dorf2.php") for u in seq):
            dorf2_present += 1

    # The skeleton is no longer fixed — many distinct sequences appear.
    assert len(set(sequences)) > 10
    # dorf2 is visited often but NOT every time.
    assert 0 < dorf2_present < n
