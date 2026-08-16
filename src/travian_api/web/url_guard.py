"""Guard for user-supplied Travian server URLs.

Any registered user can point the backend at a URL, and the backend then sends
a login POST and follows the target's authentication redirect. On a shared or
LAN deployment that is an authenticated SSRF primitive: a loopback or
private-network service can be probed with the server's own network position.

The rules are deliberately those of a real Travian world and nothing wider:
https on the default port, a resolvable public hostname, never a bare IP.
Every resolved address must be globally routable -- a public DNS name pointed
at 127.0.0.1 is rejected the same as a literal.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from fastapi import HTTPException, status


async def _resolve_host(host: str) -> list[str]:
    """All addresses *host* resolves to. Split out so tests can stub DNS."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def ensure_safe_server_url(server_url: str) -> None:
    """Raise a 400 unless *server_url* is a plausible public Travian server."""

    def _reject(reason: str) -> None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"server_url {reason}",
        )

    try:
        parsed = urlsplit(server_url)
    except ValueError:
        _reject("is not a valid URL")
    if parsed.scheme != "https":
        _reject("must use https")
    host = parsed.hostname
    if not host:
        _reject("has no hostname")
    if parsed.username or parsed.password:
        _reject("must not embed credentials")
    try:
        if parsed.port not in (None, 443):
            _reject("must use the default https port")
    except ValueError:
        _reject("has an invalid port")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # a hostname, as expected
    else:
        _reject("must be a hostname, not an IP address")

    try:
        addresses = await _resolve_host(host)
    except socket.gaierror:
        _reject("could not be resolved")
    for address in addresses:
        # IPv6 addresses may carry a zone index ("%eth0"); the address part
        # is what routes.
        ip = ipaddress.ip_address(address.split("%")[0])
        if not ip.is_global:
            _reject("resolves to a private or local address")
