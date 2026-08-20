"""Shared test plumbing.

Two session-wide guarantees are installed from ``pytest_configure``, i.e.
before collection, so even module-level code in a test file is covered:

1. **No real network.** This repo drives a live Travian account behind an
   anti-bot layer. A test that reaches the game server spends real requests,
   feeds the very fingerprint the stealth code exists to hide, and — for the
   trade-route writes — can mutate the account. Outbound connections to
   anything but the local machine therefore raise ``RealNetworkBlocked``
   naming the host. The block sits at the transport chokepoints rather than on
   one client class, so httpx, curl_cffi, and anything added later are all
   covered.

2. **No real credentials.** ``Settings()`` with no overrides reads ``.env``
   and ``TRAVIAN_*`` environment variables, both of which hold a real account
   on a developer machine. Several code paths build a default ``Settings``
   (``TravianSession``, ``ReconAccount``), so the suite scrubs both sources:
   a test can construct a client, but never one holding live credentials, and
   never one with a live-write flag armed from the developer's environment.

The URL guard resolves hostnames through real DNS in production. Tests must
never depend on the network, so DNS is stubbed here to "resolves publicly"
for every test; guard-specific tests override the stub per-case to exercise
private/loopback/unresolvable outcomes.
"""

from __future__ import annotations

import asyncio
import atexit
import ipaddress
import os
import shutil
import socket
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import pytest


class RealNetworkBlocked(RuntimeError):
    """A test tried to open a connection to a host off the local machine."""


_LOCAL_HOSTNAMES = frozenset(
    {"", "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)

# TRAVIAN_* variables the suite keeps: plumbing, not identity. Everything else
# under the prefix feeds Settings (base_url/username/password/recon/live flags)
# and is removed, so a new secret added to Settings later is scrubbed by
# default instead of silently becoming reachable.
_KEEP_ENV = frozenset({"TRAVIAN_DB_PATH", "TRAVIAN_DEV", "TRAVIAN_DIST_DIAG"})

_AF_UNIX = getattr(socket, "AF_UNIX", None)

_PATCHES: list[tuple[Any, str, Any]] = []


def _is_local(host: object) -> bool:
    """True when *host* names this machine (loopback, unspecified, or empty)."""
    if not isinstance(host, (str, bytes)):
        return False
    text = host.decode("utf-8", "replace") if isinstance(host, bytes) else host
    text = text.strip().strip("[]").lower()
    if text in _LOCAL_HOSTNAMES:
        return True
    try:
        address = ipaddress.ip_address(text.split("%")[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _blocked(target: str, via: str) -> RealNetworkBlocked:
    return RealNetworkBlocked(
        f"Real network access blocked in the test suite: {via} -> {target}. "
        "A test must never reach a live Travian server: it spends real game "
        "requests, feeds the anti-bot fingerprint, and can mutate the account. "
        "Stub the transport instead of driving a real client."
    )


def _egress_target(sock: socket.socket, address: object) -> str | None:
    """``host:port`` when *address* leaves this machine, else None."""
    if _AF_UNIX is not None and sock.family == _AF_UNIX:
        return None
    if not isinstance(address, tuple) or not address:
        return None  # AF_UNIX path or an exotic family — not IP egress
    host = address[0]
    if _is_local(host):
        return None
    port = address[1] if len(address) > 1 else "?"
    return f"{host}:{port}"


def _guard_socket_method(real: Any) -> Callable[..., Any]:
    """Wrap ``socket.connect``/``connect_ex``: covers stdlib, httpx sync,
    urllib, and the asyncio selector loop."""

    def guarded(self: socket.socket, address: object, *args: Any, **kwargs: Any) -> Any:
        target = _egress_target(self, address)
        if target is not None:
            raise _blocked(target, f"socket.{real.__name__}")
        return real(self, address, *args, **kwargs)

    return guarded


def _guard_getaddrinfo(real: Any) -> Callable[..., Any]:
    """Wrap ``socket.getaddrinfo``.

    Resolution is the first step of every hostname-based connection, including
    ``loop.getaddrinfo``, so blocking here reports the offending host directly
    instead of buried inside the anyio task group httpx connects through.
    """

    def guarded(host: Any, port: Any = None, *args: Any, **kwargs: Any) -> Any:
        if host is not None and not _is_local(host):
            raise _blocked(f"{host}:{port}", "socket.getaddrinfo")
        return real(host, port, *args, **kwargs)

    return guarded


def _guard_loop_create_connection(real: Any) -> Callable[..., Any]:
    """Wrap ``BaseEventLoop.create_connection``.

    Every asyncio TCP connection funnels through it, which is what the socket
    patch above cannot see on Windows: the proactor loop connects with
    overlapped ``ConnectEx``, never ``socket.connect``.
    """

    async def guarded(
        self: asyncio.AbstractEventLoop,
        protocol_factory: Any,
        host: Any = None,
        port: Any = None,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if host is not None and not _is_local(host):
            raise _blocked(f"{host}:{port}", "asyncio create_connection")
        return await real(self, protocol_factory, host, port, *args, **kwargs)

    return guarded


def _guard_curl_setopt(real: Any) -> Callable[..., Any]:
    """Wrap ``curl_cffi.Curl.setopt``.

    libcurl resolves and connects in C, so a curl_cffi request makes no Python
    socket call at all and the patches above miss it entirely. Setting
    ``CurlOpt.URL`` is the one chokepoint every curl_cffi request — sync,
    async, or websocket — passes through.

    This one fails closed: only an explicitly local hostname is let through.
    libcurl guesses a host out of a schemeless URL ("api/v1/x" resolves host
    "api"), so "no hostname" has to count as egress, not as a local call.
    """
    from curl_cffi import CurlOpt

    def guarded(self: Any, option: Any, value: Any, *args: Any, **kwargs: Any) -> Any:
        if option == CurlOpt.URL:
            raw = (
                value.decode("utf-8", "replace")
                if isinstance(value, (bytes, bytearray))
                else str(value)
            )
            if not _is_local(urlsplit(raw).hostname):
                raise _blocked(raw, "curl_cffi request")
        return real(self, option, value, *args, **kwargs)

    return guarded


def _patch(owner: Any, name: str, factory: Callable[[Any], Any]) -> None:
    real = getattr(owner, name)
    _PATCHES.append((owner, name, real))
    setattr(owner, name, factory(real))


def _install_network_block() -> None:
    if _PATCHES:
        return  # the repo-root conftest re-exports the hook; install once
    _patch(socket, "getaddrinfo", _guard_getaddrinfo)
    _patch(socket.socket, "connect", _guard_socket_method)
    _patch(socket.socket, "connect_ex", _guard_socket_method)
    _patch(asyncio.base_events.BaseEventLoop, "create_connection", _guard_loop_create_connection)
    try:
        from curl_cffi import Curl
    except ImportError:  # curl_cffi is an optional stealth dependency
        return
    _patch(Curl, "setopt", _guard_curl_setopt)


def _scrub_travian_credentials() -> None:
    for key in [k for k in os.environ if k.upper().startswith("TRAVIAN_")]:
        if key.upper() not in _KEEP_ENV:
            del os.environ[key]

    import travian_api.config as config

    # A default Settings() must not read the developer's .env either. The
    # module-level `settings` was built during the import above, while .env was
    # still live, so it is rebuilt clean before anything binds to it.
    config.Settings.model_config["env_file"] = None
    config.settings = config.Settings()


def _isolate_the_database() -> None:
    """Point the suite at a throwaway SQLite file, never the live one.

    ``web/models/db.py`` resolves TRAVIAN_DB_PATH at IMPORT time, defaulting to
    ``~/.travian/travian_web.db`` -- the database both running servers hold open
    and the one that holds real users and encrypted Travian credentials. Any
    test that calls ``init_db()`` or takes a session was therefore writing to
    production data, and a run could collide with a live server mid-write.

    Set before the block below imports anything, so nothing has resolved the
    path yet. An explicit TRAVIAN_DB_PATH is respected: a developer who points
    the suite somewhere deliberately keeps that choice.
    """
    if os.environ.get("TRAVIAN_DB_PATH"):
        return
    directory = tempfile.mkdtemp(prefix="travian-test-db-")
    os.environ["TRAVIAN_DB_PATH"] = str(Path(directory) / "travian_web.db")
    atexit.register(shutil.rmtree, directory, True)


def pytest_configure(config: pytest.Config) -> None:
    _isolate_the_database()
    _scrub_travian_credentials()
    _install_network_block()


def pytest_unconfigure(config: pytest.Config) -> None:
    for owner, name, real in reversed(_PATCHES):
        setattr(owner, name, real)
    _PATCHES.clear()


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    import travian_api.web.url_guard as url_guard

    async def resolves_public(host: str) -> list[str]:
        return ["8.8.8.8"]

    monkeypatch.setattr(url_guard, "_resolve_host", resolves_public)
