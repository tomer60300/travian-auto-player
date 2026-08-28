"""The test suite must be incapable of reaching a live Travian server.

A test that hits the game server spends real requests, feeds the anti-bot
fingerprint the stealth layer exists to hide, and — since the trade-route
payload was verified — can create or toggle real trade routes on the account.
The guard lives in ``tests/conftest.py``; these are the tests that prove it
actually bites, for every transport the client can use, and that it does not
break loopback traffic or leak real credentials into a client.
"""

from __future__ import annotations

import asyncio
import os
import socket

import httpx
import pytest

from tests.conftest import RealNetworkBlocked

TRAVIAN_HOST = "ts1.x1.europe.travian.com"
TRAVIAN_URL = f"https://{TRAVIAN_HOST}/dorf1.php"


class TestOutboundIsBlocked:
    def test_a_raw_socket_to_a_travian_host_is_refused(self):
        sock = socket.socket()
        try:
            with pytest.raises(RealNetworkBlocked) as exc:
                sock.connect((TRAVIAN_HOST, 443))
        finally:
            sock.close()
        # Naming the host is the point: a silent stub would turn "this test
        # hits the network" into "this test passes for the wrong reason".
        assert TRAVIAN_HOST in str(exc.value)

    def test_connect_ex_is_refused_too(self):
        sock = socket.socket()
        try:
            with pytest.raises(RealNetworkBlocked):
                sock.connect_ex((TRAVIAN_HOST, 443))
        finally:
            sock.close()

    def test_httpx_sync_cannot_reach_a_travian_host(self):
        with httpx.Client() as client, pytest.raises(RealNetworkBlocked):
            client.get(TRAVIAN_URL)

    async def test_httpx_async_cannot_reach_a_travian_host(self):
        async with httpx.AsyncClient() as client:
            with pytest.raises(RealNetworkBlocked):
                await client.get(TRAVIAN_URL)

    async def test_curl_cffi_cannot_reach_a_travian_host(self):
        """curl_cffi resolves and connects inside libcurl, so a socket-level
        patch alone would leave the stealth transport wide open."""
        curl_cffi = pytest.importorskip("curl_cffi.requests")

        async with curl_cffi.AsyncSession() as session:
            with pytest.raises(RealNetworkBlocked) as exc:
                await session.get(TRAVIAN_URL)
        assert TRAVIAN_HOST in str(exc.value)

    def test_a_schemeless_url_through_curl_is_refused(self):
        """Regression: the guard used to allow a URL with no hostname, and
        libcurl guesses one out of the path — "api/v1/x" resolves host "api",
        a real DNS lookup. "No hostname" has to count as egress."""
        curl_cffi = pytest.importorskip("curl_cffi")

        handle = curl_cffi.Curl()
        try:
            with pytest.raises(RealNetworkBlocked):
                handle.setopt(curl_cffi.CurlOpt.URL, b"api/v1/trade-routes")
        finally:
            handle.close()

    async def test_a_bare_ip_over_asyncio_is_refused(self):
        """Numeric addresses skip DNS; the connection chokepoint must still
        catch them (the Windows proactor loop never calls socket.connect)."""
        with pytest.raises(RealNetworkBlocked):
            await asyncio.open_connection("93.184.216.34", 443)


class TestTheRealClientIsBlocked:
    """The guard has to hold for the actual production client, not just for a
    hand-rolled socket."""

    def _client(self):
        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        return HttpClient(
            Settings(
                base_url=f"https://{TRAVIAN_HOST}",
                username="u@example.com",
                password="pw",
                stealth=False,  # keep the test fast: no human-delay pacing
            )
        )

    async def test_get_html_raises_instead_of_reaching_the_server(self):
        client = self._client()
        try:
            with pytest.raises(RealNetworkBlocked) as exc:
                await client.get_html("/dorf1.php")
        finally:
            await client.close()
        assert TRAVIAN_HOST in str(exc.value)

    async def test_the_curl_transport_with_an_empty_base_url_is_blocked(self):
        """The shape a credential-scrubbed client actually takes: base_url ""
        leaves every URL relative, and the stealth transport hands those
        straight to libcurl, which resolves the first path segment as a host."""
        from travian_api.clients.http_client import HttpClient
        from travian_api.config import Settings

        client = HttpClient(Settings())
        client._stealth_enabled = False  # no human-delay pacing in a test
        assert client._use_curl is True, "the curl transport is what is under test"
        try:
            with pytest.raises(RealNetworkBlocked):
                await client.get_html("/api/v1/trade-routes")
        finally:
            await client.close()

    async def test_post_json_raises_instead_of_reaching_the_server(self):
        client = self._client()
        try:
            with pytest.raises(RealNetworkBlocked):
                await client.post_json("/api/v1/trade-routes", {})
        finally:
            await client.close()


class TestLoopbackStillWorks:
    """ASGI/localhost plumbing (and asyncio's own self-pipe) must keep
    working, otherwise the guard breaks the suite it protects."""

    def test_loopback_connect_is_not_blocked(self):
        sock = socket.socket()
        try:
            # Port 1 is almost certainly closed: a refusal proves the guard let
            # the attempt through to the OS instead of rejecting it itself.
            assert sock.connect_ex(("127.0.0.1", 1)) != 0
        finally:
            sock.close()

    async def test_loopback_asyncio_connect_is_not_blocked(self):
        with pytest.raises(OSError) as exc:
            await asyncio.open_connection("127.0.0.1", 1)
        assert not isinstance(exc.value, RealNetworkBlocked)

    def test_a_socketpair_still_works(self):
        left, right = socket.socketpair()
        try:
            left.send(b"ok")
            assert right.recv(2) == b"ok"
        finally:
            left.close()
            right.close()


class TestCredentialsCannotReachAClient:
    def test_a_default_settings_carries_no_identity(self):
        """`TravianSession` and `ReconAccount` both build a bare Settings();
        with .env or TRAVIAN_* live that is a real login one call away."""
        from travian_api.config import Settings

        s = Settings()

        assert s.base_url == ""
        assert s.username == ""
        assert s.password == ""
        assert s.recon_username == ""
        assert s.recon_password == ""

    def test_no_travian_identity_variables_survive_in_the_environment(self):
        leaked = sorted(
            k
            for k in os.environ
            if k.upper().startswith("TRAVIAN_")
            and k.upper()
            not in {
                "TRAVIAN_DB_PATH",
                "TRAVIAN_DEV",
                "TRAVIAN_DIST_DIAG",
                # The suite's own temp path for cookie/persona/scheduler state.
                # Plumbing, not identity -- and it must be PRESENT, or that
                # state falls back to the repo-relative default where parallel
                # workers race for the same files (and, run serially, mixed
                # suite activity into the operator's real stealth accounting).
                "TRAVIAN_COOKIE_FILE",
                # Present BY the scrub, pinned to "false": with live writes
                # defaulting on in production, absence would mean armed.
                "TRAVIAN_TRADE_ROUTE_LIVE",
            }
        )
        assert leaked == [], f"credential/config env vars reachable from tests: {leaked}"
        assert "travian-test-stealth-" in os.environ.get("TRAVIAN_COOKIE_FILE", ""), (
            "stealth state is not isolated -- cookie, persona and scheduler files "
            "would land in the repo and be shared across workers"
        )
        assert os.environ.get("TRAVIAN_TRADE_ROUTE_LIVE") == "false", (
            "the suite's disarm pin is missing -- sessions built in tests could "
            "write to a real account"
        )

    def test_settings_does_not_read_a_dotenv_file(self):
        from travian_api.config import Settings

        assert Settings.model_config["env_file"] is None


class TestLiveTradeRouteWritesStayDisarmed:
    """The trade-route writes are the one path that MUTATES the account."""

    def test_live_is_off_by_default(self):
        from types import SimpleNamespace

        from travian_api.services.trade_route_service import TradeRouteService

        assert TradeRouteService(http_client=SimpleNamespace()).live_enabled is False

    def test_a_session_never_arms_live_writes(self):
        from travian_api.web.sessions import TravianSession

        session = TravianSession(1, f"https://{TRAVIAN_HOST}", "alice", "pw-a")

        assert session.trade_route_service.live_enabled is False

    def test_a_live_flag_in_the_environment_is_scrubbed(self, monkeypatch):
        """A developer with the live flag set in .env or their shell must not
        thereby arm real writes inside the suite.

        Since live now defaults ON in production, absence is armed -- so the
        scrub must leave the flag PRESENT and false, not merely delete it."""
        from tests.conftest import _scrub_travian_credentials

        monkeypatch.setenv("TRAVIAN_TRADE_ROUTE_LIVE", "true")
        monkeypatch.setenv("TRAVIAN_PASSWORD", "real-account-password")

        _scrub_travian_credentials()

        assert os.environ.get("TRAVIAN_TRADE_ROUTE_LIVE") == "false"
        assert "TRAVIAN_PASSWORD" not in os.environ


class TestTheDatabaseIsIsolated:
    """The suite must not touch the live database.

    ``web/models/db.py`` defaults to ``~/.travian/travian_web.db`` -- the file
    both running servers hold open, holding real users and their encrypted
    Travian credentials. Before this was isolated, any test calling init_db() or
    taking a session wrote to production data, and a run could collide with a
    live server mid-write.
    """

    def test_the_resolved_database_is_not_the_live_one(self):
        from pathlib import Path

        from travian_api.web.models.db import DATABASE_URL

        live = (Path.home() / ".travian" / "travian_web.db").resolve()
        assert str(live) not in DATABASE_URL.replace("/", "\\"), (
            f"the suite resolved the LIVE database: {DATABASE_URL}"
        )

    def test_the_path_is_exported_so_subprocess_tests_inherit_it(self):
        import os

        # tests/test_web_entry.py boots the app in a subprocess, which re-reads
        # the environment rather than any in-process patch, so the isolation has
        # to live in os.environ to reach it.
        assert os.environ.get("TRAVIAN_DB_PATH"), "isolation must be inheritable"
