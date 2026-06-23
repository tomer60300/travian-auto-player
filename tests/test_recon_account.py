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


# ─────────────── strict-mode enforcement (_read_client) ──────────


def test_read_client_raises_in_strict_mode_when_recon_unavailable() -> None:
    """Bulletproof guarantee: under strict background-account mode,
    ``_read_client`` REFUSES to return the primary client — it raises
    ``ReconStrictViolation`` instead, so no account-independent read can
    ever physically execute on the user's primary account. This is the
    load-bearing enforcement; the per-coroutine entry guard is just a
    nicer up-front abort on top of it."""
    from travian_api.exceptions import ReconStrictViolation
    from travian_api.services.auto_scout_service import _recon_strict_context

    primary = _stub_client("primary")
    svc = AutoScoutService(primary)
    token = _recon_strict_context.set(True)
    try:
        with pytest.raises(ReconStrictViolation):
            svc._read_client()
    finally:
        _recon_strict_context.reset(token)


def test_read_client_strict_does_not_trust_legacy_attr() -> None:
    """Under strict mode the task-local ContextVar is the SOLE accepted
    authority. The legacy shared ``recon_http_client`` attribute is
    mutable state another concurrent operation could have populated, so
    strict mode must NOT ride it — with only the attr set (no scoped
    ContextVar) ``_read_client`` raises rather than returning it. This
    closes the cross-operation stale-client hole."""
    from travian_api.exceptions import ReconStrictViolation
    from travian_api.services.auto_scout_service import _recon_strict_context

    primary = _stub_client("primary")
    recon = _stub_client("recon")
    svc = AutoScoutService(primary)
    svc.recon_http_client = recon  # legacy attr only — no scoped ContextVar
    token = _recon_strict_context.set(True)
    try:
        with pytest.raises(ReconStrictViolation):
            svc._read_client()
    finally:
        _recon_strict_context.reset(token)


def test_read_client_returns_recon_in_strict_mode_via_context() -> None:
    """The scoped (ContextVar) recon client is honored under strict mode
    too — the strict gate sits AFTER both recon sources are consulted."""
    from travian_api.services.auto_scout_service import (
        _recon_context,
        _recon_strict_context,
    )

    primary = _stub_client("primary")
    recon = _stub_client("recon")
    svc = AutoScoutService(primary)
    strict_token = _recon_strict_context.set(True)
    try:
        recon_token = _recon_context.set(recon)
        try:
            assert svc._read_client() is recon
        finally:
            _recon_context.reset(recon_token)
    finally:
        _recon_strict_context.reset(strict_token)


def test_read_client_returns_primary_when_not_strict() -> None:
    """Regression: with strict OFF (the default), the no-recon fallback
    to the primary client is preserved. Strict mode is strictly opt-in —
    it must never change behavior for users who didn't ask for it."""
    primary = _stub_client("primary")
    svc = AutoScoutService(primary)
    assert svc._read_client() is primary


def test_read_client_strict_rejects_scoped_primary() -> None:
    """Defense-in-depth: even when the scoped ContextVar is set, if it
    resolved to the PRIMARY client (a recon-manager bug), strict mode
    refuses rather than reading on the user's own account."""
    from travian_api.exceptions import ReconStrictViolation
    from travian_api.services.auto_scout_service import (
        _recon_context,
        _recon_strict_context,
    )

    primary = _stub_client("primary")
    svc = AutoScoutService(primary)
    strict_token = _recon_strict_context.set(True)
    try:
        # Scoped client IS the primary — the exact corruption we guard.
        ctx_token = _recon_context.set(primary)
        try:
            with pytest.raises(ReconStrictViolation):
                svc._read_client()
        finally:
            _recon_context.reset(ctx_token)
    finally:
        _recon_strict_context.reset(strict_token)


@pytest.mark.asyncio
async def test_strict_mode_isolates_concurrent_tasks() -> None:
    """Regression guard locking the architectural invariant: strict mode
    is task-local. A strict task routed through its own recon client must
    not bleed strict enforcement into a concurrent non-strict task, and a
    non-strict task with no recon must still get the primary (not raise).
    Guards against a future refactor that introduces gather/create_task
    in the read path."""
    import asyncio

    from travian_api.services.auto_scout_service import (
        _recon_context,
        _recon_strict_context,
    )

    primary = _stub_client("primary")
    recon = _stub_client("recon")
    svc = AutoScoutService(primary)
    results: dict[str, object] = {}

    async def strict_task() -> None:
        st = _recon_strict_context.set(True)
        ct = _recon_context.set(recon)
        try:
            await asyncio.sleep(0)  # yield so the loose task interleaves
            results["strict"] = svc._read_client()
        finally:
            _recon_context.reset(ct)
            _recon_strict_context.reset(st)

    async def loose_task() -> None:
        await asyncio.sleep(0)
        results["loose"] = svc._read_client()

    await asyncio.gather(strict_task(), loose_task())
    assert results["strict"] is recon, "strict task lost its recon routing"
    assert results["loose"] is primary, "loose task wrongly affected by concurrent strict task"


def test_strict_abort_not_gated_on_is_configured() -> None:
    """Regression guard for the exact reported bug: the strict-mode
    abort in scout_ws.py must NOT additionally require
    ``recon_account_manager.is_configured()``. When it did, enabling
    'Require background account' with no recon credentials set silently
    became a no-op and the scan ran on the user's primary account — the
    precise leak strict mode exists to prevent.

    We assert that no abort condition combining a strict flag with
    ``recon_client is None`` also references ``is_configured`` — whether
    written single-line (``if strict_recon and recon_client is None:``)
    or multi-line (``if ( ... ):``)."""
    import pathlib
    import re

    src = pathlib.Path("src/travian_api/web/ws/scout_ws.py").read_text(encoding="utf-8")
    conds: list[str] = []
    # Single-line guards.
    conds += re.findall(r"if\s+([^\n]*recon_client is None[^\n]*):", src)
    # Multi-line `if ( ... ):` guards.
    for m in re.finditer(r"if\s*\((?P<cond>.*?)\):", src, re.DOTALL):
        if "recon_client is None" in m.group("cond"):
            conds.append(m.group("cond"))
    assert conds, (
        "Could not locate any strict-recon abort guard referencing "
        "'recon_client is None' in scout_ws.py — the regression guard is "
        "no longer anchored to real code."
    )
    for cond in conds:
        assert "is_configured" not in cond, (
            "Strict-recon abort condition is gated on is_configured() — "
            "this re-introduces the silent-fallback bug: 'Require "
            "background account' becomes a no-op when no recon creds "
            "are configured. The abort must fire on (strict and "
            "recon_client is None) alone."
        )


# ──────────── _resolve_recon_flags (strict implies recon) ────────


def test_resolve_recon_flags_strict_implies_acquire() -> None:
    """The BLOCKER fix: a request that REQUIRES the background account
    but unchecks 'use background account' (use_recon=False,
    recon_strict=True) must still acquire AND enforce recon. Otherwise it
    would skip recon acquisition, sail past the entry guard, and route
    every read onto the primary — the exact leak strict mode forbids.
    Strict wins over the opt-out flag."""
    from travian_api.web.ws.scout_ws import _resolve_recon_flags

    assert _resolve_recon_flags(use_recon=False, recon_strict=True) == (True, True)


def test_resolve_recon_flags_normal_cases() -> None:
    from travian_api.web.ws.scout_ws import _resolve_recon_flags

    assert _resolve_recon_flags(True, False) == (True, False)  # use, not strict
    assert _resolve_recon_flags(True, True) == (True, True)  # use + strict
    assert _resolve_recon_flags(False, False) == (False, False)  # fully opted out


# ──────── strict violation propagates through swallow sites ──────


@pytest.mark.asyncio
async def test_enrich_tiles_propagates_strict_violation() -> None:
    """The enrichment loop's broad ``except Exception`` must NOT swallow
    a strict violation into a per-tile 'enrich failed' and continue — it
    must propagate so the operation aborts. Also proves the primary
    client is never touched."""
    from travian_api.exceptions import ReconStrictViolation
    from travian_api.models.farm_list import MapTileInfo
    from travian_api.services.auto_scout_service import _recon_strict_context

    primary = _stub_client("primary")
    svc = AutoScoutService(primary)
    tile = MapTileInfo(x=10, y=91, distance=1.0)
    token = _recon_strict_context.set(True)
    try:
        with pytest.raises(ReconStrictViolation):
            await svc.enrich_tiles([tile])
    finally:
        _recon_strict_context.reset(token)
    primary.post_json.assert_not_called()


@pytest.mark.asyncio
async def test_get_player_profile_info_propagates_strict_violation() -> None:
    """``get_player_profile_info`` swallows network/parse errors into a
    default dict; it must NOT swallow a strict violation that way (which
    would corrupt population/capital data AND hide the breach)."""
    from travian_api.exceptions import ReconStrictViolation
    from travian_api.services.auto_scout_service import _recon_strict_context

    primary = _stub_client("primary")
    svc = AutoScoutService(primary)
    token = _recon_strict_context.set(True)
    try:
        with pytest.raises(ReconStrictViolation):
            await svc.get_player_profile_info(4893)
    finally:
        _recon_strict_context.reset(token)
    primary.get_html.assert_not_called()


def test_write_paths_never_use_recon() -> None:
    """Construction of TargetResolver/MilitaryService inside AutoScout
    send-scout code paths must pass the PRIMARY http_client. This is
    a structural assertion against the source — recon would silently
    break write ops because the recon account has no villages."""
    import pathlib
    import re

    src = pathlib.Path("src/travian_api/services/auto_scout_service.py").read_text(encoding="utf-8")
    # MilitaryService and TargetResolver must always be constructed
    # against self.http_client (the primary), never against
    # self._read_client() or self.recon_http_client.
    for ctor in ("MilitaryService", "TargetResolver"):
        for m in re.finditer(rf"\b{ctor}\(\s*self\.([a-z_]+)", src):
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
    with patch("travian_api.services.recon_account._get_settings") as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = ""
        instance.recon_password = ""
        assert mgr.is_configured() is False


def test_is_configured_true_when_creds_present() -> None:
    mgr = ReconAccountManager()
    with patch("travian_api.services.recon_account._get_settings") as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = "recon@example.com"
        instance.recon_password = "secret"
        assert mgr.is_configured() is True


@pytest.mark.asyncio
async def test_get_or_create_returns_none_when_unconfigured() -> None:
    mgr = ReconAccountManager()
    with patch("travian_api.services.recon_account._get_settings") as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = ""
        instance.recon_password = ""
        result = await mgr.get_or_create_client("https://ts2.x1.europe.travian.com")
        assert result is None


def test_server_slug_handles_typical_url() -> None:
    assert _server_slug("https://ts2.x1.europe.travian.com") == "ts2_x1_europe_travian_com"


def test_server_slug_handles_trailing_slash() -> None:
    assert _server_slug("https://ts2.x1.europe.travian.com/") == "ts2_x1_europe_travian_com"


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
    import pathlib
    import re

    src = pathlib.Path("src/travian_api/web/ws/scout_ws.py").read_text(encoding="utf-8")
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
    import pathlib
    import re

    src = pathlib.Path("src/travian_api/services/auto_scout_service.py").read_text(encoding="utf-8")
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
    import pathlib
    import re

    src_path = pathlib.Path("src/travian_api/web/ws/scout_ws.py").read_text(encoding="utf-8")
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
        parts = ["travian_api", "web", "ws", "scout_ws"]
        # Each dot strips one level from the right.
        levels_up = len(dots)
        if levels_up > len(parts):
            raise AssertionError(
                f"Too many dots in {m.group(0)!r}: scout_ws.py only sits {len(parts)} levels deep."
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
    with patch("travian_api.services.recon_account._get_settings") as mock_get_settings:
        instance = mock_get_settings.return_value
        instance.recon_username = "throwaway@example.com"
        instance.recon_password = "secret"
        assert mgr.get_proxy_username() == "throwaway@example.com"


def test_get_proxy_username_returns_none_when_unconfigured() -> None:
    mgr = ReconAccountManager()
    with patch("travian_api.services.recon_account._get_settings") as mock_get_settings:
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
    with (
        patch("travian_api.services.recon_account.HttpClient"),
        patch("travian_api.services.recon_account.AuthService") as mock_auth_service_cls,
    ):
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

        account._last_failure_at -= recon_module._RECON_AUTH_RETRY_AFTER_S + 1
        mock_auth.login = AsyncMock(return_value=None)
        ok = await account.ensure_authed()
        assert ok is True
        assert account._authed is True
        mock_auth.login.assert_awaited_once()
