"""Village-context and session-recovery regressions (Codex review round 3).

Since /api/villages/switch became client-side-only, `session.active_village_id`
is pinned to the login village forever. Any action route without an explicit
`village_id` therefore executes against a village the UI is not showing.
Reconnects likewise must not destroy a working session on failure, and
WebSocket auth must be able to restore a session the way HTTP dependencies do.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.web.routes.buildings import ConstructRequest, UpgradeRequest
from travian_api.web.sessions import SessionManager, try_restore_session


class TestVillageContext:
    def test_upgrade_request_carries_the_target_village(self):
        body = UpgradeRequest(slot_id=1, village_id=20003)

        assert body.village_id == 20003

    def test_construct_request_carries_the_target_village(self):
        body = ConstructRequest(slot_id=20, building_name="Granary", village_id=20003)

        assert body.village_id == 20003

    def test_construct_request_accepts_gid_only(self):
        """building_gid is documented as the preferred identifier; requiring
        building_name anyway 422s the preferred calling convention."""
        body = ConstructRequest(slot_id=20, building_gid=17)

        assert body.building_gid == 17
        assert body.building_name is None

    def test_construct_with_neither_identifier_is_a_400(self):
        from fastapi import HTTPException

        from travian_api.web.routes.buildings import construct_building

        with pytest.raises(HTTPException) as exc:
            asyncio.run(construct_building(ConstructRequest(slot_id=20)))

        assert exc.value.status_code == 400


class _StubSession:
    """Stands in for TravianSession; login outcome is configurable."""

    instances: list = []

    def __init__(self, user_id, server_url, username, password):
        self.user_id = user_id
        self.server_url = server_url
        self.player_name = username
        self.fail_login = False
        self.disconnected = False
        _StubSession.instances.append(self)

    async def connect(self):
        if self.fail_login:
            raise RuntimeError("login failed")

    async def disconnect(self):
        self.disconnected = True


class TestNonDestructiveConnect:
    def test_a_failed_login_keeps_the_existing_session(self, monkeypatch):
        """Reconnect on a transient Travian failure must not leave the user
        fully disconnected when they had a working session."""
        import travian_api.web.sessions as sessions_module

        manager = SessionManager()
        old = SimpleNamespace(disconnect_called=False)

        async def old_disconnect():
            old.disconnect_called = True

        old.disconnect = old_disconnect
        manager._sessions[1] = old

        class _FailingSession(_StubSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.fail_login = True

        monkeypatch.setattr(sessions_module, "TravianSession", _FailingSession)

        with pytest.raises(RuntimeError):
            asyncio.run(manager.connect(1, "https://ts1.x1.europe.travian.com", "u", "p"))

        assert manager.get(1) is old
        assert old.disconnect_called is False

    def test_a_successful_login_replaces_and_disconnects_the_old_session(self, monkeypatch):
        import travian_api.web.sessions as sessions_module

        manager = SessionManager()
        old = SimpleNamespace(disconnect_called=False)

        async def old_disconnect():
            old.disconnect_called = True

        old.disconnect = old_disconnect
        manager._sessions[1] = old

        monkeypatch.setattr(sessions_module, "TravianSession", _StubSession)

        new = asyncio.run(manager.connect(1, "https://ts1.x1.europe.travian.com", "u", "p"))

        assert manager.get(1) is new
        assert old.disconnect_called is True


class _FakeWebSocket:
    def __init__(self, token="t"):
        self.query_params = {"token": token}
        self.closed_with = None

    async def accept(self):
        pass

    async def close(self, code=None, reason=None):
        self.closed_with = reason


class TestWebSocketReconnect:
    def test_ws_auth_restores_a_session_instead_of_rejecting(self, monkeypatch):
        """After a backend restart, HTTP recovers via saved credentials while
        sockets used to hard-fail with 'No active Travian session'."""
        import travian_api.web.ws.manager as ws_module

        manager = ws_module.ConnectionManager()
        monkeypatch.setattr(ws_module, "decode_access_token", lambda token: {"user_id": 7})
        monkeypatch.setattr(ws_module.session_manager, "get", lambda user_id: None)

        async def restored(user_id):
            return SimpleNamespace(user_id=user_id)

        monkeypatch.setattr(ws_module, "try_restore_session", restored)

        ws = _FakeWebSocket()
        assert asyncio.run(manager.authenticate(ws)) == 7
        assert ws.closed_with is None

    def test_ws_auth_still_rejects_when_restore_fails(self, monkeypatch):
        import travian_api.web.ws.manager as ws_module

        manager = ws_module.ConnectionManager()
        monkeypatch.setattr(ws_module, "decode_access_token", lambda token: {"user_id": 7})
        monkeypatch.setattr(ws_module.session_manager, "get", lambda user_id: None)

        async def not_restored(user_id):
            return None

        monkeypatch.setattr(ws_module, "try_restore_session", not_restored)

        ws = _FakeWebSocket()
        assert asyncio.run(manager.authenticate(ws)) is None
        assert "No active Travian session" in ws.closed_with


class TestTryRestoreSession:
    def test_returns_the_live_session_without_touching_the_db(self, monkeypatch):
        import travian_api.web.sessions as sessions_module

        live = SimpleNamespace()
        monkeypatch.setattr(sessions_module.session_manager, "get", lambda user_id: live)

        assert asyncio.run(try_restore_session(7)) is live
