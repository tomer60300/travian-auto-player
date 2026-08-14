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


class TestSessionCacheIsolation:
    def test_cache_files_are_keyed_by_travian_identity_not_just_web_user(self):
        """A web user reconnecting to the same world with DIFFERENT Travian
        credentials must not resume the previous account's cached JWT/cookies —
        that silently authenticates the wrong player until the cache expires."""
        from travian_api.web.sessions import TravianSession

        server = "https://ts1.x1.europe.travian.com"
        alice = TravianSession(1, server, "alice", "pw-a")
        bob = TravianSession(1, server, "bob", "pw-b")
        alice_again = TravianSession(1, server, "alice", "pw-a")

        assert alice.settings.jwt_cache_path != bob.settings.jwt_cache_path
        assert alice._cookie_file != bob._cookie_file
        # Same identity still shares its cache, so stealth session resume works.
        assert alice.settings.jwt_cache_path == alice_again.settings.jwt_cache_path


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


class TestDisconnectVsReconnectRace:
    def test_an_explicit_disconnect_is_not_undone_by_an_inflight_reconnect(self):
        """A logout that lands while an auto-reconnect is mid-login must win:
        the user pressed disconnect, so the session installed by the racing
        login has to come down, not linger as a silent re-connect."""

        async def scenario():
            manager = SessionManager()
            lock = manager.get_reconnect_lock(1)
            login_started = asyncio.Event()
            finish_login = asyncio.Event()

            class _Restored:
                disconnected = False
                server_url = "https://ts1.x1.europe.travian.com"

                async def disconnect(self):
                    self.disconnected = True

            restored = _Restored()

            async def reconnect():
                # Mirrors get_travian_session/try_restore_session: the lock is
                # held across the whole login + install.
                async with lock:
                    login_started.set()
                    await finish_login.wait()
                    async with manager._lock:
                        manager._sessions[1] = restored

            async def logout():
                await login_started.wait()
                disconnect_task = asyncio.create_task(manager.disconnect(1))
                # Give the disconnect every chance to (wrongly) complete while
                # the login is still in flight.
                await asyncio.sleep(0.05)
                finish_login.set()
                await disconnect_task

            await asyncio.gather(reconnect(), logout())
            return manager.get(1), restored.disconnected

        session_after, torn_down = asyncio.run(scenario())

        assert session_after is None
        assert torn_down is True

    def test_manual_connect_serializes_with_the_reconnect_lock(self, monkeypatch):
        """/api/travian/connect and /reconnect call SessionManager.connect
        directly; without the reconnect lock a logout that returned while that
        login was in flight is undone when the new session installs."""
        import travian_api.web.sessions as sessions_module

        monkeypatch.setattr(sessions_module, "TravianSession", _StubSession)

        async def scenario():
            manager = SessionManager()
            lock = manager.get_reconnect_lock(1)
            await lock.acquire()
            task = asyncio.create_task(
                manager.connect(1, "https://ts1.x1.europe.travian.com", "u", "p")
            )
            await asyncio.sleep(0.05)
            finished_while_lock_held = task.done()
            lock.release()
            await task
            return finished_while_lock_held

        assert asyncio.run(scenario()) is False

    def test_the_reconnect_lock_identity_survives_a_disconnect(self):
        """Removing the lock entry on disconnect hands later reconnects a
        brand-new lock while earlier waiters still hold the old one — two
        locks means two concurrent reconnects, the exact race again."""

        async def scenario():
            manager = SessionManager()
            lock = manager.get_reconnect_lock(1)
            await manager.disconnect(1)
            return lock is manager.get_reconnect_lock(1)

        assert asyncio.run(scenario()) is True


class TestRestoreSessionBookkeeping:
    def test_restore_returns_the_live_session_even_if_the_stamp_commit_fails(self, monkeypatch):
        """A failed last_connected commit is bookkeeping, not a login failure:
        returning None here closes the WebSocket with 'No active Travian
        session' while a live session sits installed in the manager."""
        import travian_api.web.auth as auth_module
        import travian_api.web.models.db as db_module
        import travian_api.web.sessions as sessions_module

        fresh = sessions_module.SessionManager()
        monkeypatch.setattr(sessions_module, "session_manager", fresh)

        live = SimpleNamespace(server_url="https://ts1.x1.europe.travian.com")

        async def fake_connect(**kwargs):
            fresh._sessions[7] = live
            return live

        monkeypatch.setattr(fresh, "connect", fake_connect)
        monkeypatch.setattr(fresh, "_connect_locked", fake_connect, raising=False)

        cred = SimpleNamespace(
            server_url="https://ts1.x1.europe.travian.com",
            travian_username="alice",
            encrypted_password="sealed",
            last_connected=None,
        )

        class _FakeDb:
            async def execute(self, _query):
                return SimpleNamespace(scalar_one_or_none=lambda: cred)

            async def commit(self):
                raise RuntimeError("database is locked")

        class _FakeFactory:
            def __call__(self):
                return self

            async def __aenter__(self):
                return _FakeDb()

            async def __aexit__(self, *args):
                return False

        monkeypatch.setattr(db_module, "async_session_factory", _FakeFactory())
        monkeypatch.setattr(auth_module, "decrypt_credential", lambda _s: "pw")

        assert asyncio.run(sessions_module.try_restore_session(7)) is live

    def test_http_restore_returns_the_session_even_if_the_stamp_commit_fails(self, monkeypatch):
        """Same contract for the HTTP dependency: a failed last_connected
        commit after a successful login must not 403 the request while the
        live session sits installed."""
        import travian_api.web.auth as auth_module
        import travian_api.web.sessions as sessions_module

        fresh = sessions_module.SessionManager()
        monkeypatch.setattr(sessions_module, "session_manager", fresh)

        live = SimpleNamespace(server_url="https://ts1.x1.europe.travian.com")

        async def fake_connect(**kwargs):
            fresh._sessions[7] = live
            return live

        monkeypatch.setattr(fresh, "connect", fake_connect)
        monkeypatch.setattr(fresh, "_connect_locked", fake_connect, raising=False)
        monkeypatch.setattr(auth_module, "decrypt_credential", lambda _s: "pw")

        cred = SimpleNamespace(
            server_url="https://ts1.x1.europe.travian.com",
            travian_username="alice",
            encrypted_password="sealed",
            last_connected=None,
        )

        class _FakeDb:
            async def execute(self, _query):
                return SimpleNamespace(scalar_one_or_none=lambda: cred)

            async def commit(self):
                raise RuntimeError("database is locked")

        user = SimpleNamespace(id=7)

        result = asyncio.run(sessions_module.get_travian_session(user, _FakeDb()))

        assert result is live


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
