"""The post-login redirect must stay on the server the user connected to.

AuthService.login follows the login host's `redirectTo` through get_html,
which accepts absolute URLs. A malicious or compromised server could point it
at a loopback or private service, making authenticated login an SSRF vector
past the connect-time URL guard. These tests pin that off-host redirects are
refused and same-host / relative ones pass through.
"""

import pytest

from travian_api.config import Settings
from travian_api.exceptions import AuthError
from travian_api.services.auth_service import AuthService


class _StubClient:
    """Enough of HttpClient for AuthService.__init__; never called otherwise."""

    def set_auth_callback(self, _cb) -> None:
        pass


def _service() -> AuthService:
    settings = Settings(
        base_url="https://ts20.x2.europe.travian.com",
        username="u@example.com",
        password="pw",
    )
    # No network happens in _safe_login_redirect; the client is never touched.
    return AuthService(http_client=_StubClient(), settings=settings)


class TestRedirectGuard:
    def test_a_loopback_redirect_is_refused(self):
        svc = _service()
        with pytest.raises(AuthError, match="off-server"):
            svc._safe_login_redirect("http://127.0.0.1:8000/steal")

    def test_a_private_host_redirect_is_refused(self):
        svc = _service()
        with pytest.raises(AuthError, match="off-server"):
            svc._safe_login_redirect("https://192.168.1.10/admin")

    def test_a_different_public_host_is_refused(self):
        svc = _service()
        with pytest.raises(AuthError, match="off-server"):
            svc._safe_login_redirect("https://evil.example.com/x")

    def test_plain_http_same_host_is_refused(self):
        svc = _service()
        with pytest.raises(AuthError, match="off-server"):
            svc._safe_login_redirect("http://ts20.x2.europe.travian.com/x")

    def test_same_host_https_passes(self):
        svc = _service()
        url = "https://ts20.x2.europe.travian.com/api/v1/auth/redirect?code=abc"
        assert svc._safe_login_redirect(url) == url

    def test_a_relative_redirect_passes(self):
        svc = _service()
        assert svc._safe_login_redirect("/api/v1/auth/redirect?code=abc") == (
            "/api/v1/auth/redirect?code=abc"
        )
