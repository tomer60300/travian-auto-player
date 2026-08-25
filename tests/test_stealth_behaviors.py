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

        browser_headers = None  # set below, once the instance exists

        async def get_html(self, url: str, skip_reauth: bool = True) -> str:
            self.urls.append(url)
            self.browser_headers.update(url)
            return "<html></html>"

    fake_http = FakeHttpClient()
    fake_http.browser_headers = _FakePageState()
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


class _FakePageState:
    """Models the ONE page field the real client keeps for the Referer.

    PageNavigator derives "where am I?" from this, exactly as production does, so
    a double that omitted it made every navigation chain look un-walked. Keeping
    the double faithful here is the point: the field is written by every page
    load, whoever triggered it.
    """

    def __init__(self):
        self.last_page_path = None

    def update(self, path):
        self.last_page_path = path


class _RecordingHttp:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.browser_headers = _FakePageState()

    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        self.urls.append(url)
        self.browser_headers.update(url)
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


def test_action_delay_is_lognormal_shaped_not_triangular():
    """Action delays must be floored, right-skewed, envelope-preserving, capped.

    Triangular has a hard min, linear ramps, and a hard max cutoff — a KS /
    Anderson-Darling test rejects it against human action-time data. The shifted
    log-normal keeps the tuned envelope (mode preserved, ~95th pct at the old
    max) while removing the hard max cutoff.
    """
    import random

    from travian_api.stealth.human_delay import HumanDelay

    min_s, mode_s, max_s = 0.8, 1.5, 4.0  # PAGE_LOAD profile

    random.seed(99)
    hd = HumanDelay(enabled=True)
    samples = [hd._action_delay(min_s, mode_s, max_s) for _ in range(40000)]
    n = len(samples)
    ordered = sorted(samples)
    mean = sum(samples) / n
    median = ordered[n // 2]

    # Soft floor: never below min_s.
    assert min(samples) >= min_s
    # Tail soft-capped at 4x the span.
    assert max(samples) <= min_s + (max_s - min_s) * 4.0
    # Right-skewed (lognormal): mean above median.
    assert mean > median
    # Envelope preserved: the old max is ~the 95th percentile (no hard cutoff).
    within = sum(1 for s in samples if s <= max_s) / n
    assert 0.88 <= within <= 0.99
    # Median sits between the mode and the old max.
    assert mode_s <= median <= max_s


def test_video_tick_delay_stays_tight(monkeypatch):
    """VIDEO_TICK must keep its tight triangular range (ATG ~3s requirement).

    Exercises the real wait() branch with sleep stubbed out.
    """
    import random

    from travian_api.stealth.human_delay import _TIMING_PROFILES, ActionType, HumanDelay

    async def _noop(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", _noop)

    min_s, _mode_s, max_s = _TIMING_PROFILES[ActionType.VIDEO_TICK]
    random.seed(3)
    hd = HumanDelay(enabled=True)
    # First 14 calls can't trigger the periodic think-pause (fires at an action
    # count that is a multiple of >=15) and VIDEO_TICK is excluded from
    # micro-pauses, so every delay must stay inside the tight profile band.
    for _ in range(14):
        d = asyncio.run(hd.wait(ActionType.VIDEO_TICK))
        assert min_s <= d <= max_s


def test_seed_delays_is_persona_stable_and_distinct():
    """The per-account delay-spread multiplier is stable, distinct, in-band."""
    from travian_api.stealth.human_delay import HumanDelay

    def mult(identity: str) -> float:
        hd = HumanDelay(enabled=True)
        hd.seed_delays(identity)
        return hd._delay_sigma_mult

    a = "Chrome/133|en-US|https://ts2.x1.europe.travian.com|saltAAAA"
    b = "Chrome/131|de-DE|https://ts1.x1.travian.de|saltBBBB"

    assert mult(a) == mult(a)
    assert mult(a) != mult(b)
    assert 0.92 <= mult(a) <= 1.12
    # The mode is invariant to the multiplier (tuned central tendency preserved).
    import random

    random.seed(1)
    hd_a = HumanDelay(enabled=True)
    hd_a.seed_delays(a)
    hd_b = HumanDelay(enabled=True)
    hd_b.seed_delays(b)
    # Both keep the floor and cap regardless of spread.
    for hd in (hd_a, hd_b):
        d = [hd._action_delay(0.8, 1.5, 4.0) for _ in range(2000)]
        assert min(d) >= 0.8
        assert max(d) <= 0.8 + (4.0 - 0.8) * 4.0


def test_action_delay_tail_mass_stays_bounded_across_personas():
    """Over-max tail mass must stay in a tight band across the persona range.

    The sigma multiplier shifts where the old max falls on the curve, so a wide
    band would let cross-account tail mass vary enough for a per-action-class
    likelihood-ratio test. The narrow [0.92, 1.12] band must keep P(delay>max)
    within a human-plausible window for every account.
    """
    import random

    from travian_api.stealth.human_delay import HumanDelay

    min_s, mode_s, max_s = 0.8, 1.5, 4.0
    random.seed(5)
    for mult in (0.92, 1.0, 1.12):
        hd = HumanDelay(enabled=True)
        hd._delay_sigma_mult = mult
        d = [hd._action_delay(min_s, mode_s, max_s) for _ in range(40000)]
        over_max = sum(1 for x in d if x > max_s) / len(d)
        assert 0.02 <= over_max <= 0.12


class _FixedTempo:
    """Test double: a SessionTempo that always returns a fixed multiplier."""

    def __init__(self, mult: float):
        self.mult = mult

    def current(self, now=None):
        return self.mult


def test_session_tempo_is_bounded_persona_stable_and_autocorrelated():
    """Tempo must stay bounded, be persona-stable, and drift (not be iid)."""
    import random

    from travian_api.stealth.session_tempo import SessionTempo

    # Persona-stable params: same identity -> same phi/sigma; distinct otherwise.
    a1 = SessionTempo("acct-A|salt1")
    a2 = SessionTempo("acct-A|salt1")
    b = SessionTempo("acct-B|salt2")
    assert (a1._phi, a1._noise_sigma) == (a2._phi, a2._noise_sigma)
    assert (a1._phi, a1._noise_sigma) != (b._phi, b._noise_sigma)

    # Neutral center: before any drift (z == 0) the multiplier is exactly 1.0.
    assert abs(SessionTempo("acct-A|salt1").current(now=0.0) - 1.0) < 1e-12

    # Within one step interval, the tempo is constant (no double-step when a
    # HumanDelay.wait and a throttler.wait fire close together).
    t = SessionTempo("acct-A|salt1", step_interval_s=30.0)
    v0 = t.current(now=1000.0)
    assert t.current(now=1001.0) == v0
    assert t.current(now=1029.0) == v0

    # Advance the walk on a 30s grid; collect a long series.
    random.seed(123)
    t = SessionTempo("acct-A|salt1", low=0.7, high=1.5, step_interval_s=30.0)
    series = [t.current(now=1000.0 + i * 30.0) for i in range(3000)]

    # Bounded multiplier — strictly interior (tanh squash, no clamp mass).
    assert min(series) > 0.7
    assert max(series) < 1.5
    # No boundary pile-up: few samples sit near either bound (a hard clamp
    # would create a sticky boundary regime an HMM could detect).
    near_bound = sum(1 for v in series if v <= 0.7 * 1.02 or v >= 1.5 * 0.98) / len(series)
    assert near_bound < 0.10

    # Positive lag-1 autocorrelation (the whole point — not iid).
    mean = sum(series) / len(series)
    num = sum((series[i] - mean) * (series[i + 1] - mean) for i in range(len(series) - 1))
    den = sum((x - mean) ** 2 for x in series)
    lag1 = num / den
    assert lag1 > 0.2


def test_human_delay_applies_tempo_except_video_tick(monkeypatch):
    """The action delay scales with the shared tempo; VIDEO_TICK does not."""
    import random

    from travian_api.stealth.human_delay import ActionType, HumanDelay

    async def _noop(_seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", _noop)

    # RAPID: first call, no micro/think pause -> delay == base * tempo. Same
    # seed gives the same base draw, so the ratio isolates the tempo factor.
    hd_plain = HumanDelay(enabled=True)
    random.seed(42)
    d_plain = asyncio.run(hd_plain.wait(ActionType.RAPID))

    hd_tempo = HumanDelay(enabled=True)
    hd_tempo.set_tempo(_FixedTempo(2.0))
    random.seed(42)
    d_tempo = asyncio.run(hd_tempo.wait(ActionType.RAPID))

    assert abs(d_tempo - 2.0 * d_plain) < 1e-9

    # VIDEO_TICK ignores tempo (functional ~3s cadence).
    hd_v = HumanDelay(enabled=True)
    random.seed(7)
    v_plain = asyncio.run(hd_v.wait(ActionType.VIDEO_TICK))
    hd_v2 = HumanDelay(enabled=True)
    hd_v2.set_tempo(_FixedTempo(2.0))
    random.seed(7)
    v_tempo = asyncio.run(hd_v2.wait(ActionType.VIDEO_TICK))
    assert v_tempo == v_plain


def test_throttler_tempo_scales_gap_but_keeps_floor(monkeypatch):
    """A low tempo must not push the inter-request gap below the hard floor."""
    from travian_api.stealth.throttler import RequestThrottler

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    thr = RequestThrottler(min_gap_s=1.0, max_gap_s=2.5, enabled=True)
    thr.set_tempo(_FixedTempo(0.05))  # would push the gap far below the floor

    asyncio.run(thr.wait())  # first call: records time, no gap enforced
    asyncio.run(thr.wait())  # second call: elapsed ~0 -> a gap is enforced

    # The enforced wait was floored at min_gap_s despite the tiny tempo.
    assert slept
    assert max(slept) >= 1.0 - 0.05


def test_tempo_scale_respects_stealth_flag_and_bounds():
    """tempo_scale modulates human-paced loop waits, and is a no-op when off."""
    from travian_api.clients.http_client import HttpClient
    from travian_api.config import Settings

    on = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="t@e.com",
            password="pw123456",
            stealth=True,
        )
    )
    try:
        scaled = on.tempo_scale(100.0)
        # Bounded by the SessionTempo multiplier range [0.7, 1.5].
        assert 0.7 * 100.0 <= scaled <= 1.5 * 100.0
    finally:
        asyncio.run(on.close())

    off = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="t@e.com",
            password="pw123456",
            stealth=False,
        )
    )
    try:
        assert off.tempo_scale(100.0) == 100.0  # no-op when stealth disabled
    finally:
        asyncio.run(off.close())


def test_idle_browse_is_persona_weighted_not_uniform():
    """Mid-session idle browsing must be persona-weighted, not fleet-uniform.

    A flat random.choice over the page list is identical across accounts — a
    visit-frequency chi-square clusters them. The persona page bias gives each
    account a distinct, stable idle distribution.
    """
    import random
    from collections import Counter

    from travian_api.stealth.human_delay import HumanDelay
    from travian_api.stealth.navigator import PageNavigator

    allowed = {"/dorf1.php", "/dorf2.php", "/statistiken.php", "/spieler.php", "/karte.php"}

    def freqs(identity: str) -> Counter:
        http = _RecordingHttp()
        nav = PageNavigator(http, HumanDelay(enabled=False), enabled=True)
        nav.seed_routes(identity)
        random.seed(0)  # same draw stream -> any difference is the persona weights

        async def draw() -> None:
            # One event loop for all 5,000 draws rather than one per draw.
            # asyncio.run costs ~1.2ms of loop construction against ~0.01ms of
            # actual work here, which was 12 of this test's 15 seconds. The
            # draw stream is unchanged: asyncio.run consumes no randomness.
            for _ in range(5000):
                await nav.idle_browse(village_id=5)

        asyncio.run(draw())
        for url in http.urls:
            assert url.split("?")[0] in allowed  # no impossible page
        return Counter(u.split("?")[0] for u in http.urls)

    a = "Chrome/133|en-US|https://ts2.x1.europe.travian.com|saltAAAA"
    b = "Chrome/131|de-DE|https://ts1.x1.travian.de|saltBBBB"

    # Persona page bias is stable per account, distinct across accounts.
    nav_a = PageNavigator(_RecordingHttp(), HumanDelay(enabled=False), enabled=True)
    nav_a.seed_routes(a)
    nav_a2 = PageNavigator(_RecordingHttp(), HumanDelay(enabled=False), enabled=True)
    nav_a2.seed_routes(a)
    nav_b = PageNavigator(_RecordingHttp(), HumanDelay(enabled=False), enabled=True)
    nav_b.seed_routes(b)
    assert nav_a._route_page_bias == nav_a2._route_page_bias
    assert nav_a._route_page_bias != nav_b._route_page_bias

    fa = freqs(a)
    total = sum(fa.values())
    # Not flat-uniform: the empirical idle distribution deviates from a flat
    # 0.20-each (idle_browse is now a persona Markov chain, so its stationary
    # distribution is flatter than the old marginal pick but still not uniform).
    l1_uniform = sum(abs(fa[p] / total - 0.20) for p in allowed)
    assert l1_uniform > 0.10
    # Two accounts produce materially different idle distributions.
    fb = freqs(b)
    tb = sum(fb.values())
    l1 = sum(abs(fa[p] / total - fb[p] / tb) for p in allowed)
    assert l1 > 0.10


def test_scheduler_break_durations_are_triangular_not_uniform():
    """Break durations must taper at the band edges, not be flat (uniform).

    A flat support is KS-rejectable; the file already uses triangular for the
    caps for exactly this reason. Verifies all three break branches stay in
    range and the short-break distribution is right-skewed (mode below mid).
    """
    import random

    from travian_api.stealth.scheduler import ActivityScheduler

    s = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0, min_break_minutes=10.0)

    random.seed(21)
    shorts = []
    for _ in range(20000):
        # Force the standard mid-session branch: daytime hour, low rolling use.
        # next_break_duration reads datetime.now().hour, which we can't pin
        # here, so sample the short-break formula directly via the same RNG.
        extra = random.triangular(0.0, 10.0, 3.0)
        shorts.append((10.0 + extra) * 60.0)
    n = len(shorts)
    mean = sum(shorts) / n
    median = sorted(shorts)[n // 2]
    # Right-skewed (mode below the midpoint): mean above median.
    assert mean > median
    # Bounded to [base, base+10] minutes -> [600, 1200] s.
    assert min(shorts) >= 600.0
    assert max(shorts) <= 1200.0

    # And the live method stays within each branch's band across many calls.
    random.seed(5)
    for _ in range(2000):
        d = s.next_break_duration()
        # night 6-9h, long 1-3h, or short 10-20min -> all within [600s, 9*3600s].
        assert 600.0 <= d <= 9.0 * 3600.0


def test_idle_browse_uses_markov_transition_from_current_page():
    """Idle browsing must take a first-order Markov step when on a known page.

    Reuses the warm_up transition matrix so idle transitions aren't memoryless
    (a structure a first-order-Markov likelihood-ratio test could exploit).
    """
    import random

    from travian_api.stealth.human_delay import HumanDelay
    from travian_api.stealth.navigator import _WARMUP_PAGES, PageNavigator

    nav = PageNavigator(_RecordingHttp(), HumanDelay(enabled=False), enabled=True)
    nav.seed_routes("Chrome/133|en-US|https://ts2.x1.europe.travian.com|saltAAAA")

    # _page_key maps paths back to page names (or None for non-top-level pages).
    assert nav._page_key("/dorf1.php?newdid=5") == "dorf1"
    assert nav._page_key("/statistiken.php") == "statistiken"
    assert nav._page_key("/build.php?id=12") is None
    assert nav._page_key(None) is None

    # _next_idle_page is a pure transition over pages (never returns stop/None),
    # and its distribution depends on the current page (Markov, not memoryless).
    random.seed(0)
    from collections import Counter

    after_dorf1 = Counter(nav._next_idle_page("dorf1") for _ in range(4000))
    after_spieler = Counter(nav._next_idle_page("spieler") for _ in range(4000))
    assert None not in after_dorf1
    assert set(after_dorf1) <= set(_WARMUP_PAGES)
    # dorf1 never self-loops to a *guaranteed* page; just assert the two
    # source pages induce different next-page distributions (Markov structure).
    da = {p: after_dorf1[p] / 4000 for p in _WARMUP_PAGES}
    ds = {p: after_spieler[p] / 4000 for p in _WARMUP_PAGES}
    l1 = sum(abs(da[p] - ds[p]) for p in _WARMUP_PAGES)
    assert l1 > 0.05


def test_scheduler_circadian_is_persona_seeded_and_distinct():
    """Night-rest phase + wake band must be per-account, not shared constants.

    The scheduler was the only stealth component not seeded with the behavioral
    identity, so every account on a host shared a synchronized night window and
    an identical wake-duration CDF — a cross-account clustering tell. seed_circadian
    binds them to the persona: stable per account, distinct across accounts,
    within sane bounds. Unseeded defaults preserve the legacy 23:00-06:00 window.
    """
    from travian_api.stealth.scheduler import ActivityScheduler

    def circadian(identity: str) -> tuple:
        s = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0)
        s.seed_circadian(identity)
        return (s._night_start_hour, s._night_end_hour, s._night_break_band)

    a = "Chrome/133|en-US|https://ts2.x1.europe.travian.com|saltAAAA"
    b = "Chrome/131|de-DE|https://ts1.x1.travian.de|saltBBBB"

    assert circadian(a) == circadian(a)  # stable per account
    assert circadian(a) != circadian(b)  # distinct across accounts

    start, end, (lo, hi, mode) = circadian(a)
    assert 22.0 <= start < 24.0
    assert 5.0 <= end < 8.0
    assert 5.5 <= lo <= 6.5
    assert 8.5 <= hi <= 9.5
    assert lo < mode < hi

    # Unseeded default preserves legacy behavior (23:00-06:00, (6,9,7) band).
    fresh = ActivityScheduler(max_continuous_hours=6.0, max_daily_hours=16.0)
    assert fresh._night_start_hour == 23.0
    assert fresh._night_end_hour == 6.0
    assert fresh._night_break_band == (6.0, 9.0, 7.0)


def test_http_client_penalty_jitter_band():
    """Throttle penalties must be jittered ±15%, not fixed point masses.

    A fixed post-error penalty (e.g. exactly 120s after every 429) is a point
    mass a KS / Anderson-Darling test on post-error inter-request gaps can flag.
    """
    import random

    from travian_api.clients.http_client import _jitter_penalty

    random.seed(0)
    for base in (120.0, 30.0, 90.0):
        samples = [_jitter_penalty(base) for _ in range(5000)]
        assert min(samples) >= base * 0.85
        assert max(samples) <= base * 1.15
        # Central tendency preserved (mean within ~1% of base).
        assert abs(sum(samples) / len(samples) - base) < base * 0.02
        # Actually varies (not a point mass).
        assert len(set(samples)) > 100
