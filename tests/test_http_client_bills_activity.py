"""Every game request is billed to the activity ceiling, exactly once.

`log_activity` had four call sites in the whole tree
(`trade_route_service`, `farm_list_service`, `build_queue_service`,
`scout_ws`), and `post_json`/`post_form`/`get_html` never billed at all. So
`daily_hours_used` accrued from the distribution planner, farm-list sends and
the scout WS and from nothing else: a day of farm-builder runs, build-queue
upgrades, oasis sweeps and video claims moved the ceiling by zero, and the
NEXT farm send was then told it could continue. The stealth model's central
budget was measuring a minority of the traffic.

Measured before the fix, per writer: farm-list edit 15 writes -> 0 bills,
farm builder 6 writes -> 0, troop dispatch 13 form POSTs -> 0, oasis raider 4
-> 0, video rewards ~8 -> 0, reports/analyzer N -> 0. The build queue billed a
constant `poll_interval_s` per loop iteration whatever happened in it, and not
from a `finally`, so an exception billed nothing.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
import tenacity

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.exceptions import NetworkError


def _client() -> tuple[HttpClient, list[float]]:
    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
    )
    # Billing is what is under test, not stealth pacing or the curl transport.
    client._stealth_enabled = False
    client._use_curl = False
    bills: list[float] = []
    client._activity_scheduler = SimpleNamespace(log_activity=bills.append)
    return client, bills


def _ok(text: str = '{"ok": true}') -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        text=text,
        headers={},
        json=lambda: {"ok": True},
        url="https://ts2.x1.europe.travian.com/api/v1/x",
    )


def _all_ok(monkeypatch, client):
    async def ok(*args, **kwargs):
        return _ok()

    for verb in ("post", "get", "delete", "put", "request"):
        monkeypatch.setattr(client.client, verb, ok, raising=False)


REQUESTS = [
    ("post_json", lambda c: c.post_json("/api/v1/farm-list/slot", {"slots": []})),
    ("delete_json", lambda c: c.delete_json("/api/v1/farm-list/slot", data={"slots": [1]})),
    ("put_json", lambda c: c.put_json("/api/v1/trade-routes", {"routes": []})),
    ("post_form", lambda c: c.post_form("/build.php?gid=16&tt=2", {"ok": "ok"})),
    ("get_html", lambda c: c.get_html("/dorf1.php")),
]


@pytest.mark.parametrize("name,issue", REQUESTS, ids=[n for n, _ in REQUESTS])
def test_one_request_is_billed_once(monkeypatch, name, issue):
    client, bills = _client()
    _all_ok(monkeypatch, client)
    asyncio.run(issue(client))
    assert len(bills) == 1, f"{name} billed {len(bills)} times"
    assert bills[0] >= 0.0


def test_n_requests_of_mixed_kinds_are_billed_n_times(monkeypatch):
    client, bills = _client()
    _all_ok(monkeypatch, client)

    async def _mixed():
        for _name, issue in REQUESTS:
            await issue(client)

    asyncio.run(_mixed())
    assert len(bills) == len(REQUESTS)


@pytest.mark.parametrize("name,issue", REQUESTS, ids=[n for n, _ in REQUESTS])
def test_a_failed_request_is_billed_too(monkeypatch, name, issue):
    """The request went out and consumed a throttler gap either way."""
    client, bills = _client()

    async def boom(*args, **kwargs):
        raise httpx.ConnectError("connection dropped")

    for verb in ("post", "get", "delete", "put", "request"):
        monkeypatch.setattr(client.client, verb, boom, raising=False)

    with pytest.raises((NetworkError, httpx.ConnectError, tenacity.RetryError)):
        asyncio.run(issue(client))
    assert len(bills) >= 1, f"{name} billed nothing on the except path"


def test_an_http_error_is_billed(monkeypatch):
    client, bills = _client()

    async def refused(*args, **kwargs):
        return SimpleNamespace(
            status_code=400,
            text='{"error":"errorRaidListSlotLimit"}',
            headers={},
            json=lambda: {"error": "errorRaidListSlotLimit"},
            url="https://ts2.x1.europe.travian.com/api/v1/farm-list/slot",
        )

    monkeypatch.setattr(client.client, "post", refused)
    with pytest.raises(NetworkError):
        asyncio.run(client.post_json("/api/v1/farm-list/slot", {"slots": []}))
    assert len(bills) == 1


def test_billing_failures_never_break_a_request_that_went_out(monkeypatch):
    """Accounting must never break a write the game already accepted."""
    client, _bills = _client()
    _all_ok(monkeypatch, client)

    def explode(seconds):
        raise RuntimeError("scheduler state file is gone")

    client._activity_scheduler = SimpleNamespace(log_activity=explode)
    assert asyncio.run(client.post_json("/api/v1/x", {})) == {"ok": True}
