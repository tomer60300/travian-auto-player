"""Concurrent registration must resolve deterministically.

The duplicate check is read-then-insert while users.username is also unique at
the database level: two simultaneous /register calls for one name can both pass
the SELECT, and the loser used to surface the IntegrityError as a 500.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from travian_api.web.routes.users import UserCreate, register


class _RacingDb:
    """Passes the duplicate SELECT but loses the insert race on commit."""

    async def execute(self, _query):
        return SimpleNamespace(scalar_one_or_none=lambda: None)

    def add(self, _user):
        pass

    async def commit(self):
        raise IntegrityError("INSERT INTO users", {}, Exception("UNIQUE constraint failed"))

    async def rollback(self):
        pass


def test_a_long_password_is_rejected_with_422_not_a_500():
    """bcrypt 5 raises ValueError for passwords over 72 BYTES. Without a
    byte-aware limit on the model, a long (or emoji-heavy: 4 bytes each)
    password sails through validation and detonates inside hash_password(),
    turning a user mistake into a server error."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        UserCreate(username="longpass", password="x" * 73)

    # Byte-aware, not character-aware: 19 four-byte emoji = 76 bytes.
    with pytest.raises(pydantic.ValidationError):
        UserCreate(username="emoji", password="\U0001f600" * 19)

    # 72 bytes exactly is bcrypt's limit and must still be accepted.
    UserCreate(username="edge", password="x" * 72)


def test_login_with_an_overlong_password_is_401_not_500():
    """An existing user typing garbage past 72 bytes must get the normal
    invalid-credentials answer, not a traceback from bcrypt.checkpw."""
    from travian_api.web.auth import hash_password, verify_password

    hashed = hash_password("correct-password")

    assert verify_password("x" * 100, hashed) is False


def test_losing_a_registration_race_returns_409_not_500():
    # Called directly (not through FastAPI), so the Depends-injected limiter
    # never runs; a stub Request satisfies the added positional parameter.
    request = SimpleNamespace(state=SimpleNamespace(), client=SimpleNamespace(host="127.0.0.1"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(register(UserCreate(username="dup", password="hunter2"), request, _RacingDb()))

    assert exc.value.status_code == 409


def test_auth_endpoints_are_rate_limited_by_ip():
    """A flood of unauthenticated register/login calls from one IP must be
    throttled: each runs bcrypt, so an unlimited burst is both a DoS on the
    event loop and a brute-force channel."""
    from travian_api.web.rate_limit import RateLimiter

    limiter = RateLimiter(max_calls=3, window_seconds=60)
    request = SimpleNamespace(state=SimpleNamespace(), client=SimpleNamespace(host="10.1.1.1"))

    async def hammer():
        for _ in range(3):
            await limiter(request)  # within budget
        await limiter(request)  # the fourth trips it

    with pytest.raises(HTTPException) as exc:
        asyncio.run(hammer())
    assert exc.value.status_code == 429
