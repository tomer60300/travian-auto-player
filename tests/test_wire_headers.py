"""What curl-impersonate actually puts on the wire, not what we build.

`curl_easy_impersonate` applies its Chrome default header block AFTER
`CurlOpt.HTTPHEADER`, so our dict can override a value but never remove a key.
That made three navigation-only headers invisible from Python while going out on
every API request: `Upgrade-Insecure-Requests: 1`, `Sec-Fetch-User: ?1`, and a
document-priority `Priority: u=0, i`.

`Sec-Fetch-Mode: cors` + `Sec-Fetch-Dest: empty` + `Sec-Fetch-User: ?1` is a
combination Chrome is structurally incapable of producing, so any one of them is
sufficient to prove the client is not a browser. Asserting on the dict we build
cannot catch this class of bug — only a real socket can.
"""

import asyncio
import socket
import threading

import pytest

from travian_api.stealth.headers import BrowserHeaders
from travian_api.stealth.user_agents import UserAgentRotator

curl_requests = pytest.importorskip("curl_cffi.requests", reason="curl_cffi is optional")

# Correct for a document navigation, impossible on a fetch or XHR.
NAVIGATION_ONLY = ("upgrade-insecure-requests", "sec-fetch-user")


def _capture_one_request(port: int, out: list[str]) -> None:
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    try:
        conn, _ = server.accept()
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        out.append(data.decode("latin-1"))
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
        conn.close()
    finally:
        server.close()


def _wire_headers(shape: str, port: int) -> dict[str, str]:
    """Send one request through curl_cffi and return what the socket received."""
    captured: list[str] = []
    listener = threading.Thread(target=_capture_one_request, args=(port, captured), daemon=True)
    listener.start()

    headers = getattr(BrowserHeaders(UserAgentRotator(), base_url="http://127.0.0.1"), shape)()

    async def send() -> None:
        async with curl_requests.AsyncSession(impersonate="chrome136") as session:
            try:
                await session.post(
                    f"http://127.0.0.1:{port}/api/v1/probe",
                    json={"probe": 1},
                    headers=headers,
                    timeout=5,
                )
            except Exception:
                pass  # the stub server is not a full HTTP implementation

    asyncio.run(send())
    listener.join(timeout=5)
    assert captured, "nothing reached the socket"

    on_wire: dict[str, str] = {}
    for line in captured[0].splitlines()[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        on_wire[name.strip().lower()] = value.strip()
    return on_wire


# for_form_post is deliberately absent: a form POST is a document navigation
# (Travian answers it with a PRG redirect), so the navigation headers belong on
# it. TestFormPostStaysANavigation below pins that distinction.
@pytest.mark.parametrize("shape,port", [("for_xhr", 8931), ("for_json_post", 8932)])
class TestNoNavigationHeadersOnSubresources:
    def test_navigation_only_headers_never_reach_the_wire(self, shape, port):
        on_wire = _wire_headers(shape, port)
        leaked = [h for h in NAVIGATION_ONLY if h in on_wire]
        assert not leaked, (
            f"{shape} put {leaked} on the wire; Chrome cannot send these on a "
            f"fetch/XHR, and each alone identifies the client as not a browser"
        )

    def test_the_priority_is_the_subresource_one(self, shape, port):
        on_wire = _wire_headers(shape, port)
        # curl-impersonate's default is the DOCUMENT priority u=0; a captured
        # real client request carried u=1.
        assert on_wire.get("priority") == "u=1, i", (
            f"{shape} sent priority {on_wire.get('priority')!r}; a real client sent 'u=1, i'"
        )

    def test_the_fetch_metadata_still_says_subresource(self, shape, port):
        # The suppression must not take the legitimate Sec-Fetch-* with it.
        on_wire = _wire_headers(shape, port)
        assert on_wire.get("sec-fetch-dest") == "empty"
        assert on_wire.get("sec-fetch-mode") in {"cors", "same-origin"}


class TestFormPostStaysANavigation:
    """The suppression must not be applied to a request that really is one."""

    def test_a_form_post_keeps_its_navigation_shape(self):
        on_wire = _wire_headers("for_form_post", 8934)
        assert on_wire.get("sec-fetch-dest") == "document", (
            "a form POST answers with a PRG redirect to a page; stripping its "
            "navigation headers would make a real navigation look like a fetch"
        )
        assert "upgrade-insecure-requests" in on_wire
