"""The JWT cache must actually restore a session the server recognises.

Cookie names are case-sensitive (RFC 6265) and the game issues ``JWT`` on code
exchange (docs/19-authentication-full.md:82). Restoring it under any other name
leaves the session unauthenticated, which makes the whole cache dead weight:
the liveness probe in login() then fails and a full credential login runs anyway.
"""

import asyncio
import base64
import json
import time

from travian_api.config import Settings
from travian_api.services.auth_service import AuthService

SERVER_COOKIE = "JWT"


def _make_jwt(subject: str = "cached", ttl: int = 3600) -> str:
    """A structurally real three-segment JWT with a comfortably future expiry."""

    def seg(payload: dict) -> str:
        return base64.b64encode(json.dumps(payload).encode()).decode()

    header = seg({"alg": "HS256", "typ": "JWT"})
    body = seg(
        {
            "exp": int(time.time()) + ttl,
            "sub": subject,
            "properties": {"did": 20001},
        }
    )
    return f"{header}.{body}.signature"


class _FakeHttp:
    """Models the one rule that matters: only the ``JWT`` cookie authenticates."""

    def __init__(self) -> None:
        self.cookies: dict[str, str] = {}
        self.requests: list[str] = []
        self.auth_callback = None

    # ── surface AuthService touches ──────────────────────────────────
    def set_auth_callback(self, callback) -> None:
        self.auth_callback = callback

    def set_cookie(self, name: str, value: str) -> None:
        self.cookies[name] = value

    def clear_cookies(self) -> None:
        self.cookies.clear()

    def get_cookies(self) -> dict[str, str]:
        return dict(self.cookies)

    async def try_resolve_x_version(self) -> None:
        return None

    async def post_json(self, path: str, payload=None, skip_reauth: bool = False, **kwargs):
        self.requests.append(f"POST {path}")
        if path == "/api/v1/graphql":
            if SERVER_COOKIE not in self.cookies:
                return {}  # server sees no session
            return {
                "data": {
                    "ownPlayer": {
                        "name": "FreshLogin",
                        "tribeId": 2,
                        "villages": [
                            {"id": 999, "name": "Settled", "x": 5, "y": 7, "isMainVillage": True}
                        ],
                    }
                }
            }
        if path == "/api/v1/auth/login":
            return {
                "redirectTo": "/api/v1/auth?code=abc&response_type=redirect",
                "code": "abc",
            }
        return {}

    async def get_html(
        self, url: str, follow_redirects: bool = True, skip_reauth: bool = False, **kwargs
    ) -> str:
        self.requests.append(f"GET {url}")
        if url.startswith("/api/v1/auth?code="):
            # Code exchange is what actually issues the session cookie.
            self.cookies[SERVER_COOKIE] = _make_jwt("server-issued")
        return "<html></html>"


def _service(http: _FakeHttp, tmp_path, token: str) -> AuthService:
    cache = tmp_path / "jwt_cache.json"
    cache.write_text(
        json.dumps(
            {
                "jwt": token,  # on-disk key, deliberately unrelated to the cookie name
                "expires_at": int(time.time()) + 3600,
                "player_name": "CachedPlayer",
                "tribe_id": 2,
                "village_id": 20001,
                "villages": [],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(
        base_url="https://ts2.x1.europe.travian.com",
        username="user@example.com",
        password="secret",
        jwt_cache_path=str(cache),
    )
    return AuthService(http, settings)


def test_cached_jwt_is_restored_under_the_name_the_server_issues(tmp_path):
    http = _FakeHttp()
    token = _make_jwt()
    service = _service(http, tmp_path, token)

    asyncio.run(service._load_cached_jwt())

    assert http.cookies == {SERVER_COOKIE: token}


def test_a_valid_cache_avoids_a_credential_login(tmp_path):
    """The point of the cache: no password on the wire when a live token exists."""
    http = _FakeHttp()
    service = _service(http, tmp_path, _make_jwt())

    state = asyncio.run(service.login())

    assert not any("auth/login" in request for request in http.requests), (
        f"credential login still performed: {http.requests}"
    )
    # The liveness probe already carries the current identity, so a resumed
    # session must not keep serving the cache file's snapshot.
    assert state.player_name == "FreshLogin"


def test_a_cached_resume_refreshes_the_village_list(tmp_path):
    """Villages settle, get chiefed, or get renamed while a JWT stays valid;
    resuming from cache must not serve the stale list — or a village_id that
    is no longer on the account."""
    http = _FakeHttp()
    service = _service(http, tmp_path, _make_jwt())

    state = asyncio.run(service.login())

    assert [v.id for v in state.villages] == [999]
    assert state.village_id == 999  # cached 20001 no longer exists


def test_get_jwt_prefers_the_server_cookie_over_a_stale_lowercase_one(tmp_path):
    http = _FakeHttp()
    service = _service(http, tmp_path, _make_jwt())
    http.cookies["jwt"] = "stale.stale.signature"
    http.cookies[SERVER_COOKIE] = "fresh.fresh.signature"

    assert asyncio.run(service.get_jwt()) == "fresh.fresh.signature"
