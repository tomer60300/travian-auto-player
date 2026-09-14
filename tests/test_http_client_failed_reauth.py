"""A re-auth that fails must raise, not hand back the login page as content.

`get_html` detects a login redirect by URL, calls `_handle_session_expired()`
and retries once -- and then never re-checks whether the retry ALSO landed on
login. The `skip_reauth=True` branch immediately below it does exactly that
check and raises `SessionExpiredError`; the re-auth path does neither, so a
failed re-auth returns login HTML to the caller as if it were the page.

What that costs, measured on the live account 2026-09-13: the account snapshot
came back **200 OK** with all 27 villages at `merchants_total: 0`,
`lumber/clay/iron_per_hour: 0.0` and `warehouse_capacity: null`, and the
endpoint blamed Travian Plus -- which the page itself disproves, stating
`travianPlus.isActive: true` outright. The session was simply dead, and every
layer above treated "not read" as "zero" because the parser was handed a login
page and found no villages in it.

The two readings are not interchangeable. "Zero merchants everywhere" is a
plan that moves nothing; "I could not read the account" is a refusal. Only one
of them is true, and the client is the only layer that still knows which.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.exceptions import SessionExpiredError

LOGIN_URL = "https://ts2.x1.europe.travian.com/login?msg=expired"
PAGE_URL = "/village/statistics/resources"
LOGIN_HTML = "<html><body><form id='loginForm'>Session expired</form></body></html>"


def _client() -> HttpClient:
    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
    )
    # Neither stealth pacing nor the curl transport is under test here.
    client._stealth_enabled = False
    client._use_curl = False
    return client


def _login_response() -> SimpleNamespace:
    return SimpleNamespace(status_code=200, text=LOGIN_HTML, headers={}, url=LOGIN_URL)


def _page_response(html: str) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        text=html,
        headers={},
        url="https://ts2.x1.europe.travian.com" + PAGE_URL,
    )


class TestAFailedReauthIsAnError:
    """The retry landed on login too. That is a refusal, not a page."""

    def test_login_html_is_not_returned_as_the_page(self, monkeypatch):
        client = _client()
        calls = {"n": 0, "reauth": 0}

        async def always_login(url, headers=None, **kwargs):
            calls["n"] += 1
            return _login_response()

        async def failed_reauth():
            """A re-auth that runs and does not restore the session.

            This is the real shape: `_handle_session_expired` swallows its own
            failure, so it returns normally whether or not the login worked.
            """
            calls["reauth"] += 1

        monkeypatch.setattr(client, "client", SimpleNamespace(get=always_login))
        monkeypatch.setattr(client, "_handle_session_expired", failed_reauth)

        with pytest.raises(SessionExpiredError) as exc:
            asyncio.run(client.get_html(PAGE_URL))

        assert calls["reauth"] == 1, "the re-auth is still attempted exactly once"
        assert calls["n"] == 2, "the page is fetched once, then once more after the re-auth"
        assert "login" in str(exc.value).lower()

    def test_a_reauth_that_works_still_returns_the_page(self, monkeypatch):
        """The guard must not fire on the case it exists to protect.

        A session that really is restored lands on the page the second time,
        and that page is returned exactly as it always was -- which is what
        keeps this a fix for the failure and not a change to the success.
        """
        client = _client()
        calls = {"n": 0}

        async def login_then_page(url, headers=None, **kwargs):
            calls["n"] += 1
            return _login_response() if calls["n"] == 1 else _page_response("<html>real</html>")

        async def ok_reauth():
            return None

        monkeypatch.setattr(client, "client", SimpleNamespace(get=login_then_page))
        monkeypatch.setattr(client, "_handle_session_expired", ok_reauth)

        assert asyncio.run(client.get_html(PAGE_URL)) == "<html>real</html>"
        assert calls["n"] == 2

    def test_a_page_that_never_redirects_is_untouched(self, monkeypatch):
        """No redirect, no re-auth, no guard -- the ordinary path is unchanged."""
        client = _client()
        calls = {"n": 0, "reauth": 0}

        async def straight_through(url, headers=None, **kwargs):
            calls["n"] += 1
            return _page_response("<html>fine</html>")

        async def never():  # pragma: no cover - asserted not to run
            calls["reauth"] += 1

        monkeypatch.setattr(client, "client", SimpleNamespace(get=straight_through))
        monkeypatch.setattr(client, "_handle_session_expired", never)

        assert asyncio.run(client.get_html(PAGE_URL)) == "<html>fine</html>"
        assert calls == {"n": 1, "reauth": 0}
