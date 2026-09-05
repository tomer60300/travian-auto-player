"""A token for a user who no longer exists is rejected on sockets too.

``get_current_user`` (the HTTP dependency) decodes the JWT and then looks the
``User`` row up, 401ing when it is gone. ``ConnectionManager.authenticate``
decoded the same token and returned ``payload["user_id"]`` with no lookup at
all -- so one deleted account was rejected on every HTTP route and accepted on
all ten WebSocket endpoints, for the remaining life of its 24-hour token. There
is no revocation anywhere in this app, so deleting the row is the only way an
operator has to end a session; on sockets it did nothing.

The lookup opens its own db session, the way ``try_restore_session`` in
``sessions.py`` already does for the same reason: a socket has no
request-scoped dependency to borrow one from.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.web.auth import create_access_token
from travian_api.web.ws.manager import ConnectionManager


class _FakeSocket:
    """Enough WebSocket for authenticate(): headers, query params, accept, close."""

    def __init__(self, token: str) -> None:
        self.headers = {"sec-websocket-protocol": f"travian-jwt, {token}"}
        self.query_params: dict[str, str] = {}
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self, *_args, **_kwargs) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


class _Row:
    """A stand-in for the SQLAlchemy Result of `select(User).where(...)`."""

    def __init__(self, user):
        self._user = user

    def scalar_one_or_none(self):
        return self._user


class _Db:
    def __init__(self, user):
        self._user = user
        self.queried = False

    async def execute(self, _stmt):
        self.queried = True
        return _Row(self._user)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def patched_db(monkeypatch):
    """Point the manager's user lookup at a db that returns what we say."""
    holder = SimpleNamespace(db=None)

    def factory(user):
        holder.db = _Db(user)
        monkeypatch.setattr(
            "travian_api.web.models.db.async_session_factory",
            lambda: holder.db,
        )
        return holder

    return factory


def _authenticate(token: str):
    socket = _FakeSocket(token)
    user_id = asyncio.run(ConnectionManager().authenticate(socket, require_travian_session=False))
    return user_id, socket


def test_a_token_for_a_deleted_user_is_refused(patched_db):
    patched_db(None)
    user_id, socket = _authenticate(
        create_access_token(user_id=42, username="ghost", token_version=0)
    )

    assert user_id is None
    assert socket.accepted, "ASGI requires accept() before close()"
    assert socket.closed is not None
    assert socket.closed[0] == 1008


def test_a_token_for_a_live_user_is_accepted(patched_db):
    holder = patched_db(SimpleNamespace(id=42, username="me", token_version=0))
    user_id, socket = _authenticate(create_access_token(user_id=42, username="me", token_version=0))

    assert user_id == 42
    assert socket.closed is None
    assert holder.db.queried, "the row must actually be looked up, not assumed"


def test_an_undecodable_token_never_reaches_the_database(patched_db):
    holder = patched_db(SimpleNamespace(id=42, username="me", token_version=0))
    user_id, socket = _authenticate("not-a-jwt")

    assert user_id is None
    assert socket.closed is not None
    assert not holder.db.queried
