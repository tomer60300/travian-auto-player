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


def test_saved_server_connect_succeeds_even_if_the_stamp_fails(monkeypatch):
    """Same contract as /connect: the timestamp is bookkeeping and must not
    turn a successful saved-server login into a 500."""
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

    cred = SimpleNamespace(
        server_url="https://ts1.x1.europe.travian.com",
        travian_username="alice",
        encrypted_password="sealed",
        last_connected=None,
    )
    monkeypatch.setattr(auth_routes, "decrypt_credential", lambda _s: "pw")

    class _StampFailsDb:
        async def execute(self, _query):
            return SimpleNamespace(scalar_one_or_none=lambda: cred)

        async def commit(self):
            raise RuntimeError("database is locked")

    res = asyncio.run(auth_routes.connect_saved_server(1, SimpleNamespace(id=1), _StampFailsDb()))

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


def test_saving_the_same_server_twice_updates_instead_of_duplicating(monkeypatch):
    """Duplicate (user, server, username) rows break last_connected stamping
    (MultipleResultsFound) and let auto-restore pick an older row with an
    outdated password. Saving again must update the existing row."""
    monkeypatch.setattr(auth_routes, "encrypt_credential", lambda p: f"enc:{p}")
    existing = SimpleNamespace(
        id=5,
        user_id=1,
        server_url="https://ts1.x1.europe.travian.com",
        travian_username="alice",
        encrypted_password="enc:old",
        label=None,
        last_connected=None,
    )
    added = []

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [existing]))

        def add(self, obj):
            added.append(obj)

        async def commit(self):
            pass

        async def refresh(self, _obj):
            pass

    body = auth_routes.SavedServerRequest(
        server_url="https://ts1.x1.europe.travian.com",
        username="alice",
        password="rotated",
        label="main",
    )

    res = asyncio.run(auth_routes.save_server(body, SimpleNamespace(id=1), _Db()))

    assert added == []
    assert existing.encrypted_password == "enc:rotated"
    assert existing.label == "main"
    assert res.id == 5


def test_resaving_repairs_legacy_duplicate_rows(monkeypatch):
    """Legacy databases can hold duplicate rows for one account. Resaving must
    update the newest row AND delete the stale ones: auto-restore sorts by
    last_connected, so a more recently stamped older row would otherwise keep
    shadowing the rotated password forever."""
    monkeypatch.setattr(auth_routes, "encrypt_credential", lambda p: f"enc:{p}")

    def row(row_id, stamped):
        return SimpleNamespace(
            id=row_id,
            user_id=1,
            server_url="https://ts1.x1.europe.travian.com",
            travian_username="alice",
            encrypted_password="enc:old",
            label=None,
            last_connected=stamped,
        )

    newest, stale = row(9, None), row(3, "2026-01-01")
    deleted = []

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [newest, stale]))

        def add(self, _obj):
            raise AssertionError("must update the newest duplicate, not insert another")

        async def delete(self, obj):
            deleted.append(obj)

        async def commit(self):
            pass

        async def refresh(self, _obj):
            pass

    body = auth_routes.SavedServerRequest(
        server_url="https://ts1.x1.europe.travian.com",
        username="alice",
        password="rotated",
        label=None,
    )

    res = asyncio.run(auth_routes.save_server(body, SimpleNamespace(id=1), _Db()))

    assert newest.encrypted_password == "enc:rotated"
    assert deleted == [stale], "stale duplicates must be removed so restore cannot pick them"
    assert res.id == 9


def test_resaving_with_a_trailing_slash_updates_the_same_row(monkeypatch):
    """The runtime identity normalizes server URLs with rstrip('/'); the saved
    rows must dedupe the same way or a trailing slash forks a second row and
    auto-restore can pick the stale password."""
    monkeypatch.setattr(auth_routes, "encrypt_credential", lambda p: f"enc:{p}")
    existing = SimpleNamespace(
        id=5,
        user_id=1,
        server_url="https://ts1.x1.europe.travian.com/",
        travian_username="alice",
        encrypted_password="enc:old",
        label=None,
        last_connected=None,
    )
    added = []

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [existing]))

        def add(self, obj):
            added.append(obj)

        async def commit(self):
            pass

        async def refresh(self, _obj):
            pass

    body = auth_routes.SavedServerRequest(
        server_url="https://ts1.x1.europe.travian.com",  # no trailing slash
        username="alice",
        password="rotated",
        label=None,
    )

    res = asyncio.run(auth_routes.save_server(body, SimpleNamespace(id=1), _Db()))

    assert added == []
    assert existing.encrypted_password == "enc:rotated"
    assert existing.server_url == "https://ts1.x1.europe.travian.com"  # repaired in place
    assert res.id == 5


def test_stamping_matches_across_trailing_slashes():
    cred = SimpleNamespace(
        server_url="https://ts1.x1.europe.travian.com/",
        travian_username="alice",
        last_connected=None,
    )

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [cred]))

        async def commit(self):
            pass

    asyncio.run(
        auth_routes._update_last_connected(_Db(), 1, "https://ts1.x1.europe.travian.com", "alice")
    )

    assert cred.last_connected is not None


def test_stamping_prefers_the_row_whose_password_actually_logged_in(monkeypatch):
    """Legacy duplicates can hold different passwords; bumping last_connected
    on the newest row regardless keeps auto-restore pinned to the stale
    password even though the user just proved which one works."""
    monkeypatch.setattr(auth_routes, "decrypt_credential", lambda s: s.removeprefix("enc:"))

    def row(row_id, password):
        return SimpleNamespace(
            id=row_id,
            server_url="https://ts1.x1.europe.travian.com",
            travian_username="alice",
            encrypted_password=f"enc:{password}",
            last_connected=None,
        )

    newest_stale, older_correct = row(9, "old-pw"), row(3, "current-pw")

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [newest_stale, older_correct])
            )

        async def commit(self):
            pass

    asyncio.run(
        auth_routes._update_last_connected(
            _Db(),
            1,
            "https://ts1.x1.europe.travian.com",
            "alice",
            password="current-pw",
        )
    )

    assert older_correct.last_connected is not None
    assert newest_stale.last_connected is None


def test_stamping_tolerates_duplicate_rows_from_older_databases():
    """Existing databases may already hold duplicates; stamping must update
    the newest matching row instead of erroring out (the old
    scalar_one_or_none raised MultipleResultsFound, silently swallowed by the
    best-effort guard, so last_connected never advanced)."""

    def row(row_id):
        return SimpleNamespace(
            id=row_id,
            server_url="https://ts1.x1.europe.travian.com",
            travian_username="alice",
            last_connected=None,
        )

    newest, stale = row(9), row(3)

    class _Db:
        async def execute(self, _query):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [newest, stale]))

        async def commit(self):
            pass

    asyncio.run(
        auth_routes._update_last_connected(_Db(), 1, "https://ts1.x1.europe.travian.com", "alice")
    )

    assert newest.last_connected is not None
    assert stale.last_connected is None


def test_reconnect_restores_from_saved_credentials_when_no_live_session(monkeypatch):
    """The endpoint advertises reconnecting from the current OR LAST session;
    after a backend restart there is no in-memory session, which is exactly
    when a reconnect is needed most."""
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

    monkeypatch.setattr(auth_routes, "try_restore_session", fake_restore)

    res = asyncio.run(auth_routes.reconnect(SimpleNamespace(id=1), None))

    assert res.connected is True
    assert res.player_name == "Chieftain"


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
