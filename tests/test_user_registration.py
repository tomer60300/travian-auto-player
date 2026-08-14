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


def test_losing_a_registration_race_returns_409_not_500():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(register(UserCreate(username="dup", password="hunter2"), _RacingDb()))

    assert exc.value.status_code == 409
