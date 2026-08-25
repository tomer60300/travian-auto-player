"""Transient-failure retries on the write helpers.

post_json/delete_json/post_form all contain `raise  # Let tenacity see the
original exception and retry` -- but only get_html actually carried the
tenacity decorator, so a single transient ConnectError aborted eligible
requests on the first failure. These tests pin that the retry machinery is
really attached and that safe_to_retry=False still fails fast.
"""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from tenacity import wait_fixed

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings
from travian_api.exceptions import NetworkError


def _client() -> HttpClient:
    client = HttpClient(
        Settings(
            base_url="https://ts2.x1.europe.travian.com",
            username="test@example.com",
            password="test123",
        )
    )
    # The retry contract is what is under test, not stealth pacing or the
    # curl transport: plain httpx with no throttler sleeps.
    client._stealth_enabled = False
    client._use_curl = False
    return client


def _ok_response(text: str = '{"ok": true}') -> SimpleNamespace:
    return SimpleNamespace(
        status_code=200,
        text=text,
        headers={},
        json=lambda: {"ok": True},
        url="https://ts2.x1.europe.travian.com/api/v1/x",
    )


def _no_wait(bound_method) -> None:
    """Zero out the tenacity wait so the test does not sleep."""
    bound_method.retry.wait = wait_fixed(0)


class TestTransientErrorsAreRetried:
    def test_post_json_survives_two_connect_errors(self, monkeypatch):
        client = _client()
        attempts = {"n": 0}

        async def flaky_post(url, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("connection dropped")
            return _ok_response()

        monkeypatch.setattr(client.client, "post", flaky_post)
        _no_wait(client.post_json)
        try:
            result = asyncio.run(client.post_json("/api/v1/x", {"a": 1}))
        finally:
            asyncio.run(client.close())

        assert result == {"ok": True}
        assert attempts["n"] == 3

    def test_delete_json_survives_two_connect_errors(self, monkeypatch):
        client = _client()
        attempts = {"n": 0}

        async def flaky_delete(url, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("connection dropped")
            return _ok_response()

        monkeypatch.setattr(client.client, "delete", flaky_delete)
        _no_wait(client.delete_json)
        try:
            result = asyncio.run(client.delete_json("/api/v1/x"))
        finally:
            asyncio.run(client.close())

        assert result == {"ok": True}
        assert attempts["n"] == 3

    def test_post_form_survives_two_connect_errors(self, monkeypatch):
        client = _client()
        attempts = {"n": 0}

        async def flaky_post(url, **kwargs):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("connection dropped")
            return _ok_response(text="<html>ok</html>")

        monkeypatch.setattr(client.client, "post", flaky_post)
        _no_wait(client.post_form)
        try:
            result = asyncio.run(client.post_form("/build.php", {"a": "1"}))
        finally:
            asyncio.run(client.close())

        assert result == "<html>ok</html>"
        assert attempts["n"] == 3


class TestNonRetryableFailsFast:
    def test_post_json_with_safe_to_retry_false_makes_one_attempt(self, monkeypatch):
        """Non-idempotent actions (raid sends) must never re-dispatch: the
        except block converts to NetworkError, which the retry machinery is
        deliberately blind to."""
        client = _client()
        attempts = {"n": 0}

        async def failing_post(url, **kwargs):
            attempts["n"] += 1
            raise httpx.ConnectError("connection dropped")

        monkeypatch.setattr(client.client, "post", failing_post)
        _no_wait(client.post_json)
        try:
            with pytest.raises(NetworkError, match="non-retryable"):
                asyncio.run(client.post_json("/api/v1/x", {"a": 1}, safe_to_retry=False))
        finally:
            asyncio.run(client.close())

        assert attempts["n"] == 1


class TestANonRetryableWriteIsNeverReSentAfterReauth:
    """`safe_to_retry=False` means what it says, including on the reauth path.

    That path re-fired the identical request the moment a response looked like a
    session redirect -- ignoring `safe_to_retry` entirely. A create whose
    response merely LOOKED like a redirect was instantly re-POSTed, and if the
    first had already committed, the account got a duplicate route. It was also
    the only genuinely un-paced back-to-back pair in the whole write flow.

    "I do not know whether it landed" is the honest outcome. The reconciler is
    idempotent, so a later run repairs a missed create; nothing repairs a
    duplicate.
    """

    def _client_returning_redirect(self):
        client = _client()
        sent: list[str] = []

        class _Resp:
            status_code = 302
            text = "redirectTo /dorf1.php"

            def json(self):
                return {}

        async def _fake(url, *a, **kw):
            sent.append(url)
            return _Resp()

        client._use_curl = True
        client._curl_post_json = lambda url, data, headers: _fake(url)
        client._curl_put_json = lambda url, headers, data: _fake(url)
        client._curl_delete_json = lambda url, headers, data=None: _fake(url)

        async def _never(*a, **kw):
            raise AssertionError("reauth must not be attempted for a non-retryable write")

        client._handle_session_expired = _never
        return client, sent

    def test_a_non_retryable_post_raises_instead_of_re_posting(self):
        client, sent = self._client_returning_redirect()
        with pytest.raises(NetworkError, match="may already have taken effect"):
            asyncio.run(client.post_json("/api/v1/trade-routes", {"a": 1}, safe_to_retry=False))
        assert len(sent) == 1, "the request must go out exactly once"

    def test_a_non_retryable_put_raises_instead_of_re_putting(self):
        client, sent = self._client_returning_redirect()
        with pytest.raises(NetworkError, match="may already have taken effect"):
            asyncio.run(client.put_json("/api/v1/trade-routes", {"a": 1}, safe_to_retry=False))
        assert len(sent) == 1

    def test_a_non_retryable_delete_raises_instead_of_re_deleting(self):
        client, sent = self._client_returning_redirect()
        with pytest.raises(NetworkError, match="may already have taken effect"):
            asyncio.run(
                client.delete_json("/api/v1/trade-routes", data={"a": 1}, safe_to_retry=False)
            )
        assert len(sent) == 1

    def test_a_retryable_write_still_reauths_and_retries(self):
        # The behaviour must survive for the idempotent callers that rely on it.
        client, sent = self._client_returning_redirect()
        reauthed = []

        async def _ok(*a, **kw):
            reauthed.append(True)

        client._handle_session_expired = _ok
        try:
            asyncio.run(client.post_json("/api/v1/probe", {"a": 1}, safe_to_retry=True))
        except NetworkError:
            pass  # the stub keeps returning 302; the point is the retry happened
        assert reauthed, "an idempotent write must still recover from an expired session"
        assert len(sent) == 2, "and it is re-sent exactly once"
