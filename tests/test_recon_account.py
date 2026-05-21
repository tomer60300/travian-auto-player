"""Tests for the background recon-account feature.

The feature routes AutoScout READ operations (map_position scans,
tile-details fetches, profile-page fetches) through a disposable
Travian login so bot-detection / rate-limit pressure stays off the
user's primary account. Write operations (sending scouts, querying
the user's rally point) MUST stay on the primary because the recon
account has no villages of its own.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from travian_api.services.auto_scout_service import AutoScoutService
from travian_api.services.recon_account import (
    ReconAccountManager,
    _server_slug,
)


def _stub_client(name: str) -> MagicMock:
    """Build a minimal HttpClient-like mock for routing tests.

    We only care about post_json / get_html being called on the
    *correct* client. Each mock is tagged with .__client_name__ so
    test assertions can distinguish them without identity checks.
    """
    client = MagicMock(name=name)
    client.__client_name__ = name
    client.post_json = AsyncMock(return_value={"tiles": []})
    client.get_html = AsyncMock(return_value="<html></html>")
    # Stealth helpers some read paths consult.
    client.navigator = MagicMock(enabled=False)
    return client


# ─────────────────────── _read_client routing ────────────────────


def test_read_client_returns_primary_when_recon_unset() -> None:
    primary = _stub_client("primary")
    svc = AutoScoutService(primary)
    assert svc._read_client() is primary


def test_read_client_returns_recon_when_recon_set() -> None:
    primary = _stub_client("primary")
    recon = _stub_client("recon")
    svc = AutoScoutService(primary)
    svc.recon_http_client = recon
    assert svc._read_client() is recon


def test_read_client_falls_back_to_primary_when_recon_cleared() -> None:
    primary = _stub_client("primary")
    recon = _stub_client("recon")
    svc = AutoScoutService(primary)
    svc.recon_http_client = recon
    svc.recon_http_client = None
    assert svc._read_client() is primary


@pytest.mark.asyncio
async def test_with_recon_client_context_manager_scopes_routing() -> None:
    """`with_recon_client` injects a recon HttpClient for the
    duration of one coroutine via a ContextVar, then unwinds. This
    is the race-safe alternative to the mutable attribute pattern
    when concurrent scout-scan + auto-scout operations share the
    same AutoScoutService instance."""
    primary = _stub_client("primary")
    recon = _stub_client("recon")
    svc = AutoScoutService(primary)
    # Outside the context: primary.
    assert svc._read_client() is primary
    async with svc.with_recon_client(recon):
        # Inside the context: recon.
        assert svc._read_client() is recon
    # After exit: back to primary, even though attribute wasn't touched.
    assert svc._read_client() is primary


@pytest.mark.asyncio
async def test_with_recon_client_context_manager_isolates_concurrent_tasks() -> None:
    """Each asyncio task carries its own ContextVar context, so a
    concurrent scan running with recon-A cannot have its read
    routing clobbered by a concurrent scan running with recon-B (or
    with no recon at all). The mutable-attribute pattern fails this
    test; the ContextVar pattern passes."""
    import asyncio
    primary = _stub_client("primary")
    recon_a = _stub_client("recon_a")
    recon_b = _stub_client("recon_b")
    svc = AutoScoutService(primary)

    sentinels: dict[str, object] = {}

    async def task_a() -> None:
        async with svc.with_recon_client(recon_a):
            # Yield to give task_b a chance to mutate the slot
            # before we observe — proves isolation.
            await asyncio.sleep(0)
            sentinels["a"] = svc._read_client()

    async def task_b() -> None:
        async with svc.with_recon_client(recon_b):
            await asyncio.sleep(0)
            sentinels["b"] = svc._read_client()

    await asyncio.gather(task_a(), task_b())
    assert sentinels["a"] is recon_a, (
        f"Task A's recon was clobbered by Task B; got {sentinels['a']!r}"
    )
    assert sentinels["b"] is recon_b, (
        f"Task B's recon was clobbered by Task A; got {sentinels['b']!r}"
    )


@pytest.mark.asyncio
async def test_with_recon_client_none_falls_through_to_attribute() -> None:
    """When the context manager's value is None, the legacy
    attribute is consulted next (then primary). This is the
    backward-compat fallback for test paths that pre-date the
    context manager."""
    primary = _stub_client("primary")
    legacy_recon = _stub_client("legacy_recon")
    svc = AutoScoutService(primary)
    svc.recon_http_client = legacy_recon
    async with svc.with_recon_client(None):
        # ContextVar default (None) → fall through to attribute.
        assert svc._read_client() is legacy_recon


def test_write_paths_never_use_recon() -> None:
    """Construction of TargetResolver/MilitaryService inside AutoScout
    send-scout code paths must pass the PRIMARY http_client. This is
    a structural assertion against the source — recon would silently
    break write ops because the recon account has no villages."""
    import re
    import pathlib
    src = pathlib.Path(
        "src/travian_api/services/auto_scout_service.py"
    ).read_text(encoding="utf-8")
    # MilitaryService and TargetResolver must always be constructed
    # against self.http_client (the primary), never against
    # self._read_client() or self.recon_http_client.
    for ctor in ("MilitaryService", "TargetResolver"):
        for m in re.finditer(
            rf"\b{ctor}\(\s*self\.([a-z_]+)", src
        ):
            assert m.group(1) == "http_client", (
                f"{ctor} must always be constructed with self.http_client "
                f"(the primary account) — found self.{m.group(1)} which "
                f"would route writes through recon."
            )


# ─────────────────── get_tile_details / scan / profile ──────────


@pytest.mark.asyncio
async def test_get_tile_details_uses_recon_when_set() -> None:
    primary = _stub_client("primary")
    recon = _stub_client("recon")
    recon.post_json = AsyncMock(return_value={"html": "<html></html>"})
    svc = AutoScoutService(primary)
    svc.recon_http_client = recon
    await svc.get_tile_details(10, 91)
    recon.post_json.assert_awaited_once()
    primary.post_json.assert_not_called()


@pytest.mark.asyncio
async def test_get_tile_details_uses_primary_when_recon_unset() -> None:
    primary = _stub_client("primary")
    primary.post_json = AsyncMock(return_value={"html": "<html></html>"})
    svc = AutoScoutService(primary)
    await svc.get_tile_details(10, 91)
    primary.post_json.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_player_profile_info_uses_recon_when_set() -> None:
    primary = _stub_client("primary")
    recon = _stub_client("recon")
    recon.get_html = AsyncMock(return_value="")
    svc = AutoScoutService(primary)
    svc.recon_http_client = recon
    await svc.get_player_profile_info(player_id=4893)
    recon.get_html.assert_awaited_once()
    primary.get_html.assert_not_called()


# ─────────────────── ReconAccountManager behavior ───────────────


def test_is_configured_false_when_creds_blank() -> None:
    """Without env vars set the manager reports unconfigured."""
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings"
    ) as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = ""
        instance.recon_password = ""
        assert mgr.is_configured() is False


def test_is_configured_true_when_creds_present() -> None:
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings"
    ) as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = "recon@example.com"
        instance.recon_password = "secret"
        assert mgr.is_configured() is True


@pytest.mark.asyncio
async def test_get_or_create_returns_none_when_unconfigured() -> None:
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings"
    ) as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = ""
        instance.recon_password = ""
        result = await mgr.get_or_create_client(
            "https://ts2.x1.europe.travian.com"
        )
        assert result is None


def test_server_slug_handles_typical_url() -> None:
    assert (
        _server_slug("https://ts2.x1.europe.travian.com")
        == "ts2_x1_europe_travian_com"
    )


def test_server_slug_handles_trailing_slash() -> None:
    assert (
        _server_slug("https://ts2.x1.europe.travian.com/")
        == "ts2_x1_europe_travian_com"
    )


def test_server_slug_handles_unknown_host() -> None:
    """Defensive: a malformed URL shouldn't crash slug generation."""
    assert _server_slug("not a url") == "unknown"


def test_recon_proxy_routing_completeness_scout_ws() -> None:
    """Regression guard: every account-INDEPENDENT read endpoint in
    scout_ws.py (map_position POSTs, tile-details POSTs, profile
    GETs) must route through ``svc._read_client()`` or
    ``scan_client`` (the local read-client alias), NOT through
    ``svc.http_client`` / ``session.http_client`` directly. Without
    this assertion, a refactor that duplicates the map-scan loop
    inline (as happened pre-recon) would silently leak the bulk of
    the scan's traffic onto the user's primary account again."""
    import pathlib, re
    src = pathlib.Path(
        "src/travian_api/web/ws/scout_ws.py"
    ).read_text(encoding="utf-8")
    # Strip comments so commentary mentioning "/api/v1/map/position"
    # doesn't false-positive.
    src_no_comments = re.sub(r"#[^\n]*", "", src)
    # Pattern: any call to .post_json or .get_html that targets one
    # of the account-independent endpoints.
    pattern = re.compile(
        r"(?P<receiver>[\w.]+)\.(?:post_json|get_html)\s*\(\s*[^)]*?"
        r"(?:/api/v1/map/position|/api/v1/map/tile-details|/profile/)",
        re.DOTALL,
    )
    leaks: list[str] = []
    for m in pattern.finditer(src_no_comments):
        receiver = m.group("receiver")
        # _read_client() returns the appropriate client; any local
        # variable assigned from it (scan_client, read_client, etc.)
        # is also acceptable. svc._read_client() and self._read_client()
        # are the canonical access patterns.
        if "_read_client" in receiver:
            continue
        if receiver in {"scan_client", "read_client"}:
            continue
        leaks.append(receiver)
    assert not leaks, (
        f"Recon-bypass leak in scout_ws.py: account-independent reads "
        f"are calling primary http_client directly via receiver(s) "
        f"{leaks!r}. Route them through `svc._read_client()` or a "
        f"local alias assigned from it."
    )


def test_recon_proxy_routing_completeness_service() -> None:
    """Same regression guard for auto_scout_service.py. The service
    has tighter rules: read endpoints must call ``self._read_client()``
    or use a local ``read_client = self._read_client()`` alias."""
    import pathlib, re
    src = pathlib.Path(
        "src/travian_api/services/auto_scout_service.py"
    ).read_text(encoding="utf-8")
    src_no_comments = re.sub(r"#[^\n]*", "", src)
    pattern = re.compile(
        r"(?P<receiver>self\.\w+|\w+)\.(?:post_json|get_html)\s*\(\s*[^)]*?"
        r"(?:/api/v1/map/position|/api/v1/map/tile-details|/profile/)",
        re.DOTALL,
    )
    leaks: list[str] = []
    for m in pattern.finditer(src_no_comments):
        receiver = m.group("receiver")
        if "_read_client" in receiver:
            continue
        if receiver in {"read_client", "scan_client"}:
            continue
        # Bare self.http_client on a read endpoint is the leak shape.
        leaks.append(receiver)
    assert not leaks, (
        f"Recon-bypass leak in auto_scout_service.py: receiver(s) "
        f"{leaks!r} fire account-independent reads directly through "
        f"the primary http_client. Route them via self._read_client()."
    )


def test_scout_ws_lazy_imports_resolve() -> None:
    """Regression guard: scout_ws.py has inline-lazy imports of the
    recon_account module inside both scan coroutines. The lazy form
    means a wrong dotted path (e.g. `..services` vs `...services`)
    wouldn't surface at module load — only at runtime when the user
    actually starts a scan, producing
    `No module named 'travian_api.web.services'` mid-operation.

    Walk the source for every `from ...recon_account import ...` line
    and exercise the resolution by compiling + executing the import
    statement out of context.
    """
    import pathlib, re
    src_path = pathlib.Path(
        "src/travian_api/web/ws/scout_ws.py"
    ).read_text(encoding="utf-8")
    # All recon_account imports in scout_ws.py must resolve to the
    # actual module. We extract each one as written and use importlib
    # to verify it's reachable from the scout_ws.py package context.
    import importlib
    pattern = re.compile(
        r"^\s*from\s+(\.+)([\w.]*)\s+import\s+(\w[\w, ]*)$",
        re.MULTILINE,
    )
    found_any = False
    for m in pattern.finditer(src_path):
        dots, mod, names = m.group(1), m.group(2), m.group(3)
        if "recon_account" not in (mod + " " + names):
            continue
        found_any = True
        # Reconstruct the absolute module path. scout_ws.py lives at
        # `travian_api.web.ws.scout_ws`. One leading dot = parent of
        # scout_ws.py's package, etc.
        parts = "travian_api.web.ws.scout_ws".split(".")
        # Each dot strips one level from the right.
        levels_up = len(dots)
        if levels_up > len(parts):
            raise AssertionError(
                f"Too many dots in {m.group(0)!r}: scout_ws.py only "
                f"sits {len(parts)} levels deep."
            )
        anchor = ".".join(parts[: len(parts) - levels_up])
        full = f"{anchor}.{mod}" if mod else anchor
        # Strip the import names — only the module portion needs to load.
        importlib.import_module(full)
    assert found_any, "No recon_account imports found in scout_ws.py"


def test_get_proxy_username_returns_configured_username() -> None:
    """The proxy-username accessor exposes the recon login that
    operators (and users via the UI) should see in the proxy
    activation message — otherwise "background account" is opaque
    and the user can't reason about WHICH disposable account is
    fronting their reads."""
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings"
    ) as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = "throwaway@example.com"
        instance.recon_password = "secret"
        assert mgr.get_proxy_username() == "throwaway@example.com"


def test_get_proxy_username_returns_none_when_unconfigured() -> None:
    mgr = ReconAccountManager()
    with patch(
        "travian_api.services.recon_account._get_settings"
    ) as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = ""
        instance.recon_password = ""
        assert mgr.get_proxy_username() is None


@pytest.mark.asyncio
async def test_ensure_authed_retries_after_window_elapsed() -> None:
    """Codex flagged that sticky False would permanently disable the
    proxy after a single network blip. New behavior: cache the failure
    for ~30 min, then retry. This test verifies the time-window logic."""
    from travian_api.services.recon_account import ReconAccount

    # Construct a ReconAccount bypassing real HttpClient creation by
    # patching at import time.
    with patch(
        "travian_api.services.recon_account.HttpClient"
    ), patch(
        "travian_api.services.recon_account.AuthService"
    ) as mock_auth_service_cls:
        mock_auth = mock_auth_service_cls.return_value

        account = ReconAccount(
            "https://ts2.x1.europe.travian.com",
            "user@example.com",
            "pw",
        )

        # First call: login raises, we cache failure.
        mock_auth.login = AsyncMock(side_effect=RuntimeError("transient"))
        ok = await account.ensure_authed()
        assert ok is False
        assert account._authed is False
        first_failure_at = account._last_failure_at

        # Second call WITHIN the window: skip the login attempt entirely.
        mock_auth.login.reset_mock()
        ok = await account.ensure_authed()
        assert ok is False
        mock_auth.login.assert_not_called()
        # Last-failure timestamp unchanged.
        assert account._last_failure_at == first_failure_at

        # Third call AFTER the window: retry the login. Simulate elapsed
        # by rewinding _last_failure_at past the retry threshold.
        from travian_api.services import recon_account as recon_module
        account._last_failure_at -= (
            recon_module._RECON_AUTH_RETRY_AFTER_S + 1
        )
        mock_auth.login = AsyncMock(return_value=None)
        ok = await account.ensure_authed()
        assert ok is True
        assert account._authed is True
        mock_auth.login.assert_awaited_once()
