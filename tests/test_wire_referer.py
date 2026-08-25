"""What Referer actually leaves HttpClient, captured off a socket.

`BrowserHeaders` tracks ONE "last page visited" per account, and every page GET
in the app overwrites it. Each request also waits out a throttler gap (1.5-3s,
longer on the tail) BEFORE its headers are built -- so a farm loop or queue poll
firing in that window takes the Referer with it, and the request that was going
out referred from the marketplace goes out referred from /dorf1.php instead.

`post_json`/`put_json`/`delete_json` could already pin their own Referer;
`get_html` could not, so the marketplace navigation had no way to state where it
was coming from. These assert on the bytes a real socket received, because the
header dict we build is not what curl-impersonate finally sends -- it applies its
own Chrome default block AFTER ours.

The second thing proved here is the pair that must never appear together:
`Sec-Fetch-Site: none` means "there was no referring context", so emitting it
next to a Referer is a combination Chrome is structurally incapable of
producing. A page load derives that header from the client-wide last page, which
is exactly the value a pinned Referer overrides.
"""

import asyncio
import socket
import threading

import pytest

from travian_api.clients.http_client import HttpClient
from travian_api.config import Settings

pytest.importorskip("curl_cffi.requests", reason="curl_cffi is optional")

PAGE = b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 13\r\n\r\n<html></html>"
JSON = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"


def _accept_one(server: socket.socket, out: list[str], body: bytes) -> None:
    """Capture one whole request -- head AND body, since the query is in the body."""
    try:
        conn, _ = server.accept()
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        while len(rest) < length and len(data) < 1_048_576:
            chunk = conn.recv(4096)
            if not chunk:
                break
            rest += chunk
        out.append((head + b"\r\n\r\n" + rest).decode("latin-1"))
        conn.sendall(body)
        conn.close()
    finally:
        server.close()


def _capture(drive, tmp_path, response: bytes = PAGE) -> tuple[dict[str, str], str]:
    """Run *drive(client, base_url)* and return (wire headers, raw request)."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    captured: list[str] = []
    listener = threading.Thread(target=_accept_one, args=(server, captured, response), daemon=True)
    listener.start()

    base_url = f"http://127.0.0.1:{port}"
    # A fresh client per capture: the throttler's first request is instant, so
    # this is also what keeps the suite from paying a 1.5-3s gap per assertion.
    client = HttpClient(
        Settings(base_url=base_url, username="test@example.com", password="test123"),
        cookie_file=tmp_path / ".travian_cookies.json",
    )

    async def send() -> None:
        try:
            await drive(client, base_url)
        except Exception:
            pass  # the stub is not a full server; the bytes are what matter
        finally:
            await client.close()

    asyncio.run(send())
    listener.join(timeout=10)
    assert captured, "nothing reached the socket"

    headers: dict[str, str] = {}
    for line in captured[0].splitlines()[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers, captured[0]


class TestAPageLoadCanPinItsOwnReferer:
    def test_the_pinned_referer_is_what_reaches_the_wire(self, tmp_path):
        async def drive(client, base_url):
            await client.get_html("/build.php?gid=17&t=3", referer=f"{base_url}/dorf2.php")

        headers, _ = _capture(drive, tmp_path)
        assert headers.get("referer", "").endswith("/dorf2.php")

    def test_the_pin_beats_the_account_wide_last_page(self, tmp_path):
        # The whole point. A concurrent GET during the throttler gap moves the
        # shared "last page"; the pinned request must be unmoved by it.
        async def drive(client, base_url):
            client.browser_headers.update_last_page(f"{base_url}/dorf1.php")
            await client.get_html("/build.php?gid=17&t=3", referer=f"{base_url}/dorf2.php")

        headers, _ = _capture(drive, tmp_path)
        assert headers["referer"].endswith("/dorf2.php"), (
            "the pin lost to the client-wide last page, which is the exact race it exists to close"
        )

    def test_it_never_claims_there_was_no_referring_context(self, tmp_path):
        # Sec-Fetch-Site is derived from the last page, so a pinned Referer on a
        # never-navigated client would otherwise ship `none` alongside it.
        async def drive(client, base_url):
            await client.get_html("/build.php?gid=17&t=3", referer=f"{base_url}/dorf2.php")

        headers, _ = _capture(drive, tmp_path)
        assert headers.get("sec-fetch-site") == "same-origin", (
            "`none` next to a Referer is a pair Chrome cannot produce"
        )

    def test_pinning_does_not_stop_it_being_a_navigation(self, tmp_path):
        async def drive(client, base_url):
            await client.get_html("/build.php?gid=17&t=3", referer=f"{base_url}/dorf2.php")

        headers, _ = _capture(drive, tmp_path)
        assert headers.get("sec-fetch-mode") == "navigate"
        assert headers.get("sec-fetch-dest") == "document"
        assert headers.get("upgrade-insecure-requests") == "1"

    def test_an_unpinned_first_page_load_still_sends_no_referer(self, tmp_path):
        # The pin is opt-in. A browser opening its first page has nothing to
        # refer to, and that must stay true.
        async def drive(client, _base_url):
            await client.get_html("/dorf1.php")

        headers, _ = _capture(drive, tmp_path)
        assert "referer" not in headers
        assert headers.get("sec-fetch-site") == "none"


class TestTheTradeRouteReadBackLeavesTheProcessCorrectly:
    """End to end through the real client: the query bytes AND the Referer."""

    def _confirm(self, tmp_path):
        from travian_api.services.trade_route_service import TradeRouteService

        async def drive(client, base_url):
            service = TradeRouteService(client, live_enabled=True, reconciler_verified=True)
            # Pre-seed the pin the navigation would have established, so this
            # capture is the one request under test.
            service._marketplace_referer[20003] = f"{base_url}/build.php?gid=17&t=3&newdid=20003"
            await service.confirm_routes(20003)

        return _capture(drive, tmp_path, response=JSON)

    def test_it_is_referred_from_the_marketplace_tab(self, tmp_path):
        headers, _ = self._confirm(tmp_path)
        assert headers["referer"].endswith("/build.php?gid=17&t=3&newdid=20003"), (
            "an API request never advances page context, so this one has to be "
            "given a Referer or it inherits whatever a concurrent loop touched"
        )

    def test_it_goes_to_the_graphql_endpoint_as_a_post(self, tmp_path):
        _, raw = self._confirm(tmp_path)
        assert raw.splitlines()[0].startswith("POST /api/v1/graphql ")

    def test_the_query_on_the_wire_is_the_bundles_query(self, tmp_path):
        from travian_api.services.trade_route_service import MARKETPLACE_READBACK_QUERY

        _, raw = self._confirm(tmp_path)
        # The query is pure names and punctuators, so it survives JSON encoding
        # unescaped: what is asserted here is literally the bytes on the wire.
        assert MARKETPLACE_READBACK_QUERY in raw

    def test_it_carries_no_variables(self, tmp_path):
        _, raw = self._confirm(tmp_path)
        assert "variables" not in raw
