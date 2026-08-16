"""Shared test plumbing.

The URL guard resolves hostnames through real DNS in production. Tests must
never depend on the network, so DNS is stubbed here to "resolves publicly"
for every test; guard-specific tests override the stub per-case to exercise
private/loopback/unresolvable outcomes.
"""

import pytest

import travian_api.web.url_guard as url_guard


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    async def resolves_public(host: str) -> list[str]:
        return ["8.8.8.8"]

    monkeypatch.setattr(url_guard, "_resolve_host", resolves_public)
