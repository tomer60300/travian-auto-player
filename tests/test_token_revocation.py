"""A leaked token can be ended before its 24 hours are up.

P2-4 from the config audit: ``ALGORITHM = "HS256"``,
``ACCESS_TOKEN_EXPIRE_HOURS = 24``, and a payload of ``user_id`` + ``username``
+ ``exp``. No ``jti``, no version, no refresh, no logout, and
``git grep -niE "refresh_token|revoke|blocklist|jti|/logout"`` over
``src/travian_api/web/`` returned nothing relevant. Logout was client-side only
-- ``localStorage.removeItem('token')`` -- which does not invalidate anything
server-side. The only lever an operator had was deleting the user row.

The fix the audit names: a ``token_version`` on ``users``, embedded in the JWT
as ``tv`` and compared on every authenticated request. One bump ends every
token for that user, everywhere, at once.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import jwt
import pytest
from fastapi import HTTPException

from travian_api.web import auth
from travian_api.web.models.db import User


class TestTheTokenCarriesItsVersion:
    def test_the_claim_is_in_the_payload(self):
        token = auth.create_access_token(user_id=1, username="me", token_version=7)

        payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])

        assert payload["tv"] == 7

    def test_decode_hands_the_version_back(self):
        token = auth.create_access_token(user_id=1, username="me", token_version=7)

        assert auth.decode_access_token(token)["token_version"] == 7

    def test_a_token_minted_before_versions_existed_is_not_valid(self):
        """Rejected rather than read as version 0. A token that predates
        revocation is precisely a token revocation cannot reach."""
        legacy = jwt.encode(
            {"user_id": 1, "username": "me"}, auth.SECRET_KEY, algorithm=auth.ALGORITHM
        )

        with pytest.raises(jwt.InvalidTokenError):
            auth.decode_access_token(legacy)


class _Result:
    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _Db:
    def __init__(self, user):
        self._user = user

    async def execute(self, _stmt):
        return _Result(self._user)


def _get_current_user(user, token: str):
    request = SimpleNamespace(state=SimpleNamespace())
    return asyncio.run(auth.get_current_user(request, token=token, db=_Db(user)))


class TestTheHttpDependencyChecksIt:
    def test_a_matching_version_authenticates(self):
        user = User(id=1, username="me", password_hash="x", token_version=3)
        token = auth.create_access_token(user_id=1, username="me", token_version=3)

        assert _get_current_user(user, token) is user

    def test_a_stale_version_is_401(self):
        user = User(id=1, username="me", password_hash="x", token_version=4)
        token = auth.create_access_token(user_id=1, username="me", token_version=3)

        with pytest.raises(HTTPException) as raised:
            _get_current_user(user, token)

        assert raised.value.status_code == 401


class _FakeSocket:
    def __init__(self, token: str) -> None:
        self.headers = {"sec-websocket-protocol": f"travian-jwt, {token}"}
        self.query_params: dict[str, str] = {}
        self.closed: tuple[int, str] | None = None

    async def accept(self, *_args, **_kwargs) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _SessionContext:
    def __init__(self, user):
        self._db = _Db(user)

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


class TestTheSocketsCheckItToo:
    """Sockets are where an unrevokable token was worth the most: ten endpoints
    that hold a live game session, and no per-request 401 to notice."""

    def _authenticate(self, user, token, monkeypatch):
        monkeypatch.setattr(
            "travian_api.web.models.db.async_session_factory",
            lambda: _SessionContext(user),
        )
        from travian_api.web.ws.manager import ConnectionManager

        socket = _FakeSocket(token)
        user_id = asyncio.run(
            ConnectionManager().authenticate(socket, require_travian_session=False)
        )
        return user_id, socket

    def test_a_matching_version_authenticates(self, monkeypatch):
        user = SimpleNamespace(id=1, username="me", token_version=3)
        token = auth.create_access_token(user_id=1, username="me", token_version=3)

        user_id, socket = self._authenticate(user, token, monkeypatch)

        assert user_id == 1
        assert socket.closed is None

    def test_a_stale_version_is_refused(self, monkeypatch):
        user = SimpleNamespace(id=1, username="me", token_version=4)
        token = auth.create_access_token(user_id=1, username="me", token_version=3)

        user_id, socket = self._authenticate(user, token, monkeypatch)

        assert user_id is None
        assert socket.closed is not None and socket.closed[0] == 1008


class TestLogoutIsTheLever:
    def test_it_bumps_the_version_and_commits(self):
        from travian_api.web.routes.users import logout

        user = User(id=1, username="me", password_hash="x", token_version=3)
        db = SimpleNamespace(committed=False)

        async def commit():
            db.committed = True

        db.commit = commit

        asyncio.run(logout(user=user, db=db))

        assert user.token_version == 4
        assert db.committed

    def test_the_token_that_called_it_stops_working(self):
        from travian_api.web.routes.users import logout

        user = User(id=1, username="me", password_hash="x", token_version=3)
        token = auth.create_access_token(user_id=1, username="me", token_version=3)

        db = SimpleNamespace(commit=_noop)
        asyncio.run(logout(user=user, db=db))

        with pytest.raises(HTTPException):
            _get_current_user(user, token)


async def _noop() -> None:
    return None


class TestTheColumnShipsWithItsBackfill:
    def test_the_model_defaults_to_zero(self):
        assert User(username="me", password_hash="x").token_version in (0, None)

    def test_an_existing_database_gets_the_column_added(self):
        """create_all() never ALTERs an existing table, so a new column that is
        not in _COLUMN_BACKFILLS is `no such column` on every query naming it."""
        from travian_api.web.models.db import _COLUMN_BACKFILLS

        assert "token_version" in _COLUMN_BACKFILLS.get("users", {})
