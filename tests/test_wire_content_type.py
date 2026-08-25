"""What Content-Type actually leaves HttpClient on a JSON write.

The game's own API helper is a SINGLE fetch() used for every verb:

    Travian.api = function(e, t={}, i="POST") {
      return fetch("/api/v1/"+e, {method: i, body: JSON.stringify(t.data),
        headers: {...t.headers, "content-type":"application/json; charset=UTF-8"}})

So POST, PUT and DELETE all carry the charset. This app carried it only on the
`xhr` shape and left the `fetch` shape to the transport's bare
"application/json" -- so one account emitted two different spellings of the same
header to /api/v1/*, where the real client has one code path and one value. A
single-header exact match is the cheapest rejection rule there is.

Asserted on a real socket and through the real HttpClient, because that is the
only place the answer lives: `tests/test_wire_headers.py` drives BrowserHeaders
straight into curl and therefore cannot see this at all, and the header dict we
build is not what curl-impersonate finally sends.
"""

import asyncio
import socket
import threading

import pytest

from travian_api.clients.http_client import JSON_CONTENT_TYPE, HttpClient
from travian_api.config import Settings

pytest.importorskip("curl_cffi.requests", reason="curl_cffi is optional")


def _accept_one(server: socket.socket, out: list[str]) -> None:
    try:
        conn, _ = server.accept()
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        out.append(data.decode("latin-1"))
        conn.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
        )
        conn.close()
    finally:
        server.close()


def _capture(verb: str, request_type: str) -> dict[str, str]:
    """Send one real write through HttpClient and return the headers received."""
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    captured: list[str] = []
    listener = threading.Thread(target=_accept_one, args=(server, captured), daemon=True)
    listener.start()

    client = HttpClient(
        Settings(
            base_url=f"http://127.0.0.1:{port}",
            username="test@example.com",
            password="test123",
        )
    )

    async def send() -> None:
        try:
            if verb == "POST":
                await client.post_json(
                    "/api/v1/probe", {"a": 1}, request_type=request_type, safe_to_retry=False
                )
            elif verb == "PUT":
                await client.put_json(
                    "/api/v1/probe", {"a": 1}, request_type=request_type, safe_to_retry=False
                )
            else:
                await client.delete_json(
                    "/api/v1/probe",
                    data={"a": 1},
                    request_type=request_type,
                    safe_to_retry=False,
                )
        except Exception:
            pass  # the stub is not a full server; the bytes are what matter
        finally:
            await client.close()

    asyncio.run(send())
    listener.join(timeout=10)
    assert captured, f"{verb}/{request_type}: nothing reached the socket"

    headers: dict[str, str] = {}
    for line in captured[0].splitlines()[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


class TestEveryJsonWriteCarriesTheCharset:
    @pytest.mark.parametrize("verb", ["POST", "PUT", "DELETE"])
    def test_the_fetch_shape_matches_the_client(self, verb):
        assert _capture(verb, "fetch").get("content-type") == JSON_CONTENT_TYPE

    @pytest.mark.parametrize("verb", ["POST", "PUT", "DELETE"])
    def test_the_xhr_shape_still_matches(self, verb):
        assert _capture(verb, "xhr").get("content-type") == JSON_CONTENT_TYPE

    def test_the_two_shapes_do_not_disagree(self):
        # The finding was not either spelling on its own -- it was ONE account
        # sending two, which the real client's single code path cannot do.
        assert _capture("POST", "fetch").get("content-type") == _capture("POST", "xhr").get(
            "content-type"
        )

    def test_the_constant_is_the_value_the_client_sends(self):
        assert JSON_CONTENT_TYPE == "application/json; charset=UTF-8"
