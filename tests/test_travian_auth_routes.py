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


def test_connect_succeeds_even_if_the_last_connected_stamp_fails(monkeypatch):
    """The timestamp is bookkeeping: a DB hiccup after the Travian login
    succeeded must not report the connect as failed while the live session
    sits installed in the manager."""
    live = SimpleNamespace(
        server_url="https://ts1.x1.europe.travian.com",
        player_name="Chieftain",
        tribe_id=1,
        active_village_id=20003,
        auth_state=SimpleNamespace(
            villages=[SimpleNamespace(id=20003, name="03", x=23, y=88, is_main_village=True)]
        ),
    )

    async def ok_connect(**kwargs):
        return live

    monkeypatch.setattr(auth_routes.session_manager, "connect", ok_connect)

    class _BrokenDb:
        async def execute(self, _query):
            raise RuntimeError("database is locked")

    body = TravianConnectRequest(
        server_url="https://ts1.x1.europe.travian.com", username="u", password="p"
    )

    res = asyncio.run(auth_routes.connect(body, SimpleNamespace(id=1), _BrokenDb()))

    assert res.connected is True


def test_status_attempts_a_saved_credential_restore(monkeypatch):
    """After a backend restart the frontend's first call is /status; answering
    connected=false without trying the saved-credential restore sends users
    back to /connect despite the advertised seamless recovery."""
    restored = SimpleNamespace(
        server_url="https://ts1.x1.europe.travian.com",
        player_name="Chieftain",
        tribe_id=1,
        active_village_id=20003,
        auth_state=SimpleNamespace(
            villages=[SimpleNamespace(id=20003, name="03", x=23, y=88, is_main_village=True)]
        ),
    )

    monkeypatch.setattr(auth_routes.session_manager, "get", lambda user_id: None)

    async def fake_restore(user_id):
        return restored

    monkeypatch.setattr(auth_routes, "try_restore_session", fake_restore, raising=False)

    res = asyncio.run(auth_routes.get_status(SimpleNamespace(id=1)))

    assert res.connected is True
    assert res.active_village_id == 20003


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
