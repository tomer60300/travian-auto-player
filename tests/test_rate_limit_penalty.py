"""A 429 is backed off whatever the pacing switch says.

``stealth=False`` is the operator's pacing preference: no inter-request gap, no
burst cooldown, no browser-shaped headers. It used to remove the 429 backoff as
well, because the same flag guarded every ``add_penalty`` call in the request
helpers AND the throttler's penalty gate sat behind ``enabled`` -- so with
pacing off a rate-limited loop had no brake at all and simply kept hammering.

Off and un-backed-off is worse than off alone. A server telling us to slow down
is not a stealth preference, so the penalty is decoupled from the flag: it is
always recorded, and always served before the next request. What the flag still
turns off is the PACING -- the gap and the burst cooldown -- and that half is
pinned here too, because it is deliberate.

No request reaches a real server here: the transport is faked at the httpx
client, and tests/conftest.py blocks egress besides.
"""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.exceptions import NetworkError
from travian_api.stealth.throttler import RequestThrottler

URL = "https://ts2.x1.europe.travian.com/api/v1/x"

# A rate-limit body carrying a game marker ("village"), so the bot-detection
# pass recognises it as game traffic and leaves it alone. The explicit 429
# handler under test is then the only thing that can add a penalty, which is
# what makes the assertions below about that handler and nothing else.
RATE_LIMITED_BODY = '{"error":"rate limited","village":42}'


def _client(*, stealth: bool) -> HttpClient:
    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
            stealth=stealth,
            # A first request never waits a gap (there is no previous request to
            # be spaced from), but the band is pinned to zero so nothing on the
            # paced path can sleep here either.
            stealth_min_gap=0.0,
            stealth_max_gap=0.0,
        )
    )
    # Plain httpx in both modes: the 429 handling under test is the same code
    # either way, and curl_cffi would need a real socket.
    client._use_curl = False
    return client


def _rate_limited() -> SimpleNamespace:
    return SimpleNamespace(
        status_code=429,
        text=RATE_LIMITED_BODY,
        headers={},
        json=lambda: {"error": "rate limited"},
        url=URL,
    )


def _ok() -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        text='{"ok":true}',
        headers={},
        json=lambda: {"ok": True},
        url=URL,
    )


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("GET", URL)
    response = httpx.Response(429, text=RATE_LIMITED_BODY, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


# helper name -> (httpx method it goes out through, how to invoke it)
_HELPERS = {
    "post_json": ("post", lambda c: c.post_json("/api/v1/x", {"a": 1})),
    "put_json": ("request", lambda c: c.put_json("/api/v1/x", {"a": 1})),
    "delete_json": ("delete", lambda c: c.delete_json("/api/v1/x")),
    "post_form": ("post", lambda c: c.post_form("/build.php", {"a": "1"})),
    "get_html": ("get", lambda c: c.get_html("/dorf1.php")),
}


def _penalty_after(client: HttpClient, coro) -> float:
    """Seconds of rate-limit penalty still pending once *coro* has raised.

    Measured from now, not as a delta on ``_penalty_until``: that field is a
    monotonic DEADLINE and starts at 0, so a difference against its initial
    value is a clock reading rather than a duration.
    """
    try:
        with pytest.raises(NetworkError):
            asyncio.run(coro)
    finally:
        asyncio.run(client.close())
    return max(0.0, client.throttler._penalty_until - time.monotonic())


class TestEveryRequestHelperBacksOffOnA429:
    """All five helpers, both 429 shapes, with pacing off as well as on.

    Ten call sites guarded the penalty on ``_stealth_enabled``: the
    status-code check inside each helper's success path, and the
    ``httpx.HTTPStatusError`` branch of each helper's except block.
    """

    @pytest.mark.parametrize("helper", sorted(_HELPERS))
    @pytest.mark.parametrize("stealth", [False, True], ids=["pacing-off", "pacing-on"])
    def test_a_429_response_adds_a_penalty(self, monkeypatch, helper, stealth):
        method, invoke = _HELPERS[helper]
        client = _client(stealth=stealth)

        async def serve(*_args, **_kwargs):
            return _rate_limited()

        monkeypatch.setattr(client.client, method, serve)

        assert _penalty_after(client, invoke(client)) > 0.0, (
            f"{helper} left a 429 un-backed-off; the loop's next request goes out "
            f"with no brake at all"
        )

    @pytest.mark.parametrize("helper", sorted(_HELPERS))
    @pytest.mark.parametrize("stealth", [False, True], ids=["pacing-off", "pacing-on"])
    def test_a_raised_429_adds_a_penalty(self, monkeypatch, helper, stealth):
        method, invoke = _HELPERS[helper]
        client = _client(stealth=stealth)

        async def raise_429(*_args, **_kwargs):
            raise _status_error()

        monkeypatch.setattr(client.client, method, raise_429)

        assert _penalty_after(client, invoke(client)) > 0.0, (
            f"{helper} ignored a raised 429; same server instruction, same duty"
        )

    def test_the_penalty_is_the_jittered_two_minute_band(self, monkeypatch):
        # Sanity on the amount, so "a penalty was added" cannot be satisfied by
        # a token one. 120s +/-15%, per _jitter_penalty.
        client = _client(stealth=False)

        async def serve(*_args, **_kwargs):
            return _rate_limited()

        monkeypatch.setattr(client.client, "post", serve)

        added = _penalty_after(client, client.post_json("/api/v1/x", {"a": 1}))
        assert 102.0 <= added <= 138.0


class TestThePenaltyIsServedWithPacingOff:
    """Recording a penalty nobody waits out would be theatre.

    With pacing off the request helpers never reach ``throttler.wait``, so the
    unpaced path has to serve the penalty itself.
    """

    def test_a_pending_penalty_delays_the_next_request(self, monkeypatch):
        client = _client(stealth=False)

        async def serve(*_args, **_kwargs):
            return _ok()

        monkeypatch.setattr(client.client, "post", serve)
        client.throttler.add_penalty(0.4)

        started = time.monotonic()
        try:
            asyncio.run(client.post_json("/api/v1/x", {"a": 1}))
        finally:
            asyncio.run(client.close())

        assert time.monotonic() - started >= 0.35, (
            "the request went out during a rate-limit penalty with pacing off"
        )

    def test_an_unpenalised_request_is_not_delayed_and_stays_unpaced(self, monkeypatch):
        # The other half of the contract: pacing off still means no pacing. No
        # gap is served and no burst timestamp is recorded, so the switch keeps
        # doing exactly what the operator set it for.
        client = _client(stealth=False)

        async def serve(*_args, **_kwargs):
            return _ok()

        monkeypatch.setattr(client.client, "post", serve)

        try:
            asyncio.run(client.post_json("/api/v1/x", {"a": 1}))
            asyncio.run(client.post_json("/api/v1/x", {"a": 1}))
        finally:
            asyncio.run(client.close())

        assert client.throttler._last_request_time == 0
        assert client.throttler.requests_in_window == 0


class TestTheThrottlerSeparatesPacingFromPenalties:
    def test_a_disabled_throttler_paces_nothing(self):
        # Two 5s-floor gaps would be unmistakable; neither is served.
        throttler = RequestThrottler(min_gap_s=5.0, max_gap_s=9.0, enabled=False)

        started = time.monotonic()
        assert asyncio.run(throttler.wait()) == 0.0
        assert asyncio.run(throttler.wait()) == 0.0

        assert time.monotonic() - started < 1.0
        assert throttler.requests_in_window == 0

    def test_a_disabled_throttler_still_serves_a_penalty(self):
        throttler = RequestThrottler(min_gap_s=5.0, max_gap_s=9.0, enabled=False)

        # The penalty is a DEADLINE, so what it owes shrinks by however long the
        # caller took to reach the wait. Measuring from before `add_penalty`
        # asserts the deadline itself, which load can only push out, never pull
        # in -- measuring the returned figure asserted the machine was idle.
        started = time.monotonic()
        throttler.add_penalty(0.4)
        served = asyncio.run(throttler.wait_for_penalty())

        assert served > 0.0
        assert time.monotonic() - started >= 0.35

    def test_a_served_penalty_is_not_served_twice(self):
        throttler = RequestThrottler(enabled=False)
        throttler.add_penalty(0.1)

        assert asyncio.run(throttler.wait_for_penalty()) > 0.0
        assert asyncio.run(throttler.wait_for_penalty()) == 0.0

    def test_an_enabled_throttler_serves_the_penalty_through_wait(self):
        # The paced path keeps folding the penalty into its own gate, so a 429
        # is not waited out twice.
        throttler = RequestThrottler(min_gap_s=0.0, max_gap_s=0.0, enabled=True)

        started = time.monotonic()
        throttler.add_penalty(0.4)
        waited = asyncio.run(throttler.wait())

        assert waited > 0.0
        assert time.monotonic() - started >= 0.35
        assert asyncio.run(throttler.wait_for_penalty()) == 0.0
