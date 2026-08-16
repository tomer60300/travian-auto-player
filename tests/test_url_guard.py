"""SSRF guard for user-supplied server URLs.

The backend logs into whatever URL the user supplies and follows its
redirects. These tests pin that loopback, private and link-local targets are
refused in every disguise: wrong scheme, IP literal, custom port, and -- the
one format checks cannot catch -- a public DNS name resolving to a private
address.
"""

import asyncio

import pytest
from fastapi import HTTPException

import travian_api.web.url_guard as url_guard
from travian_api.web.url_guard import ensure_safe_server_url


def _check(url: str) -> None:
    asyncio.run(ensure_safe_server_url(url))


def _expect_reject(url: str, reason_fragment: str) -> None:
    with pytest.raises(HTTPException) as exc:
        _check(url)
    assert exc.value.status_code == 400
    assert reason_fragment in exc.value.detail, exc.value.detail


class TestFormatRules:
    def test_plain_http_is_refused(self):
        _expect_reject("http://ts1.x1.europe.travian.com", "https")

    def test_ip_literals_are_refused_even_public_ones(self):
        _expect_reject("https://127.0.0.1", "hostname, not an IP")
        _expect_reject("https://10.0.0.5", "hostname, not an IP")
        _expect_reject("https://[::1]", "hostname, not an IP")
        _expect_reject("https://8.8.8.8", "hostname, not an IP")

    def test_custom_ports_are_refused(self):
        _expect_reject("https://ts1.x1.europe.travian.com:8000", "default https port")

    def test_embedded_credentials_are_refused(self):
        _expect_reject("https://user:pw@ts1.x1.europe.travian.com", "credentials")

    def test_a_schemeless_string_is_refused(self):
        _expect_reject("ts1.x1.europe.travian.com", "https")


class TestHostAllowlist:
    def test_a_public_but_non_travian_host_is_refused(self):
        """The phishing shape: a plausible, publicly-resolvable host that is
        not Travian. The address checks would pass it; the allowlist must not,
        or the user's Travian credentials get POSTed to the attacker."""
        _expect_reject("https://travian-login.example.com", "not an allowed Travian host")

    def test_a_lookalike_suffix_is_refused(self):
        # endswith must be on a dot boundary: nottravian.com is not travian.com
        _expect_reject("https://nottravian.com", "not an allowed Travian host")

    def test_the_allowlist_is_configurable(self, monkeypatch):
        monkeypatch.setattr(
            url_guard.settings, "allowed_server_hosts", "travian.com,myfanserver.net"
        )

        async def resolves_public(host):
            return ["8.8.8.8"]

        monkeypatch.setattr(url_guard, "_resolve_host", resolves_public)
        _check("https://play.myfanserver.net")


class TestResolvedAddresses:
    def test_a_public_name_resolving_private_is_refused(self, monkeypatch):
        """The DNS-rebinding shape: innocent-looking public hostname, A record
        pointing into the LAN. Format checks pass; resolution must not."""

        async def resolves_private(host):
            return ["192.168.1.10"]

        monkeypatch.setattr(url_guard, "_resolve_host", resolves_private)
        _expect_reject("https://ts5.x1.internal.travian.com", "private or local")

    def test_loopback_and_linklocal_resolutions_are_refused(self, monkeypatch):
        for address in ["127.0.0.1", "169.254.1.1", "::1", "fe80::1%eth0"]:

            async def resolves(host, _address=address):
                return [_address]

            monkeypatch.setattr(url_guard, "_resolve_host", resolves)
            _expect_reject("https://ts5.x1.internal.travian.com", "private or local")

    def test_one_private_address_amongst_public_ones_is_refused(self, monkeypatch):
        async def resolves_mixed(host):
            return ["8.8.8.8", "10.0.0.1"]

        monkeypatch.setattr(url_guard, "_resolve_host", resolves_mixed)
        _expect_reject("https://ts5.x1.internal.travian.com", "private or local")

    def test_an_unresolvable_name_is_refused(self, monkeypatch):
        import socket

        async def cannot_resolve(host):
            raise socket.gaierror("no such host")

        monkeypatch.setattr(url_guard, "_resolve_host", cannot_resolve)
        _expect_reject("https://no-such-world.travian.com", "resolved")

    def test_a_real_public_travian_url_passes(self, monkeypatch):
        async def resolves_public(host):
            return ["8.8.8.8"]

        monkeypatch.setattr(url_guard, "_resolve_host", resolves_public)
        _check("https://ts20.x2.europe.travian.com")
        _check("https://ts20.x2.europe.travian.com/")


class TestConnectIsGuarded:
    def test_session_manager_refuses_a_private_target_before_logging_in(self):
        """The guard must run before any session object exists: a refused URL
        performs zero network actions against the target."""
        from travian_api.web.sessions import SessionManager

        manager = SessionManager()
        with pytest.raises(HTTPException) as exc:
            asyncio.run(manager.connect(1, "http://127.0.0.1:8000", "u", "p"))

        assert exc.value.status_code == 400
        assert manager.get(1) is None
