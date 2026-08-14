"""The background-account credentials are process-global shared state.

Any authenticated user being able to read the recon username or rotate/clear
the credentials lets one web user silently break every other user's recon
setup (Codex review P1). Management is therefore restricted to the instance
operator — the earliest-registered user.
"""

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.services.recon_account import recon_account_manager
from travian_api.web.routes.recon import (
    _status,
    clear_recon_credentials,
    get_instance_operator,
    set_recon_credentials,
    test_recon_credentials,
)


class _Db:
    """Answers the min-user-id query and nothing else."""

    def __init__(self, first_id):
        self.first_id = first_id

    async def execute(self, _query):
        return SimpleNamespace(scalar=lambda: self.first_id)


def test_the_first_registered_user_is_the_operator():
    user = SimpleNamespace(id=1)

    assert asyncio.run(get_instance_operator(user, _Db(first_id=1))) is user


def test_other_users_are_rejected_with_403():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(get_instance_operator(SimpleNamespace(id=2), _Db(first_id=1)))

    assert exc.value.status_code == 403


def test_every_mutating_recon_route_requires_the_operator():
    for route in (set_recon_credentials, clear_recon_credentials, test_recon_credentials):
        dependencies = [
            getattr(parameter.default, "dependency", None)
            for parameter in inspect.signature(route).parameters.values()
        ]
        assert get_instance_operator in dependencies, route.__name__


def test_the_operator_check_runs_before_the_session_dependency():
    """FastAPI resolves dependencies in signature order: if the Travian session
    comes first, a non-operator without a live session triggers a real
    auto-reconnect login (or gets the wrong 403) before authorization runs."""
    from travian_api.web.sessions import get_travian_session

    dependencies = [
        getattr(parameter.default, "dependency", None)
        for parameter in inspect.signature(test_recon_credentials).parameters.values()
    ]

    assert get_instance_operator in dependencies
    assert dependencies.index(get_instance_operator) < dependencies.index(get_travian_session)


def test_status_hides_the_recon_username_from_non_operators():
    recon_account_manager.set_credentials("recon-user", "recon-pass")
    try:
        assert _status(manageable=True).username == "recon-user"
        assert _status(manageable=False).username is None
        assert _status(manageable=False).configured is True
    finally:
        recon_account_manager.clear_credentials()
