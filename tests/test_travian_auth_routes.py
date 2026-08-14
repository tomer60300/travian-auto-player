"""Connect routes must pass through control-flow HTTPExceptions.

SessionManager raises a 409 when a reconnect would tear a live session out
from under running operations; the blanket `except Exception` in the connect
endpoints used to rewrap it as a 502 backend error, hiding an operator action
the user can actually resolve.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import travian_api.web.routes.travian_auth as auth_routes
from travian_api.web.routes.travian_auth import TravianConnectRequest


def _conflict(**_kwargs):
    raise HTTPException(status_code=409, detail="Cannot reconnect while operations are running")


async def _raise_conflict(**kwargs):
    _conflict(**kwargs)


def test_connect_preserves_the_409_from_the_session_manager(monkeypatch):
    monkeypatch.setattr(auth_routes.session_manager, "connect", _raise_conflict)

    body = TravianConnectRequest(
        server_url="https://ts1.x1.europe.travian.com", username="u", password="p"
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_routes.connect(body, SimpleNamespace(id=1), None))

    assert exc.value.status_code == 409


def test_reconnect_preserves_the_409_from_the_session_manager(monkeypatch):
    live = SimpleNamespace(
        server_url="https://ts1.x1.europe.travian.com",
        settings=SimpleNamespace(username="u", password="p"),
    )
    monkeypatch.setattr(auth_routes.session_manager, "get", lambda user_id: live)
    monkeypatch.setattr(auth_routes.session_manager, "connect", _raise_conflict)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(auth_routes.reconnect(SimpleNamespace(id=1), None))

    assert exc.value.status_code == 409
