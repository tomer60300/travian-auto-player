"""Registration bootstraps the first account, then closes.

``POST /api/users/register`` had no invite code, no first-user gate, no enable
switch and no allowlist -- only ``auth_limiter`` (10 calls / 60 s per IP). Both
servers bind ``0.0.0.0``, so anyone who could reach :80 or :8001 could mint
themselves a valid 24-hour JWT.

What that token does *not* buy is the game account: the new user has no
``TravianCredential`` row, so ``get_travian_session`` 403s on every game route.
The blast radius is the unauthenticated-but-registered surface, not the Travian
account -- which is why this is a P1 and not a catastrophe, and why the fix can
be as small as it is.

The gate has to bootstrap, or a fresh install has no way to create its first
account. So: registration is open while the ``users`` table is empty and closed
once it is not, unless ``TRAVIAN_ALLOW_REGISTRATION`` says otherwise. That can
never lock out an operator who already has an account -- they are on the closed
side of the gate by definition -- and it closes the window the moment the first
account exists.
"""

import pytest
from fastapi import HTTPException

from travian_api.web.routes.users import UserCreate, register


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Db:
    """Answers the duplicate-name check and the bootstrap count."""

    def __init__(self, *, existing_users: int):
        self._existing = existing_users
        self.added = []
        self.committed = False

    async def execute(self, statement):
        text = str(statement).lower()
        if "count" in text:
            return _Result(self._existing)
        return _Result(None)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        obj.id = 1


def _call(db, settings=None):
    import asyncio

    return asyncio.run(
        register(
            UserCreate(username="operator", password="hunter2000"),
            request=None,
            db=db,
            _rate=None,
        )
    )


@pytest.fixture(autouse=True)
def registration_closed_by_default(monkeypatch):
    """The setting defaults off; each test says what it wants."""
    import travian_api.web.routes.users as users_module

    monkeypatch.setattr(users_module.settings, "allow_registration", False)


def test_the_first_account_can_always_be_created():
    db = _Db(existing_users=0)
    token = _call(db)

    assert token.access_token
    assert db.committed


def test_a_second_account_is_refused_once_one_exists():
    db = _Db(existing_users=1)

    with pytest.raises(HTTPException) as exc:
        _call(db)

    assert exc.value.status_code == 403
    assert "TRAVIAN_ALLOW_REGISTRATION" in exc.value.detail
    assert not db.committed, "nothing may be written by a refused registration"


def test_the_operator_can_reopen_it_deliberately(monkeypatch):
    import travian_api.web.routes.users as users_module

    monkeypatch.setattr(users_module.settings, "allow_registration", True)
    db = _Db(existing_users=3)

    assert _call(db).access_token
    assert db.committed


def test_the_setting_defaults_off():
    from travian_api.config import Settings

    assert Settings(base_url="https://example.invalid", _env_file=None).allow_registration is False
