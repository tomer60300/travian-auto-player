"""The WS bearer token must travel by subprotocol, and never reach the logs.

Two layers, tested independently:
- the server reads the JWT from the `travian-jwt` subprotocol (so it stays out
  of the URL uvicorn logs), with the legacy query param still honored;
- a logging filter scrubs any secret query param that still reaches a record,
  so even the fallback path cannot write a reusable token to the logs.
"""

import logging
from types import SimpleNamespace

from travian_api.logging_config import QueryStringRedactionFilter
from travian_api.web.ws.manager import _token_from_subprotocol


def _ws(header: str | None = None, query: dict | None = None) -> SimpleNamespace:
    headers = {"sec-websocket-protocol": header} if header is not None else {}
    return SimpleNamespace(headers=headers, query_params=query or {})


class TestSubprotocolToken:
    def test_token_is_read_from_the_subprotocol(self):
        ws = _ws(header="travian-jwt, header.payload.sig")
        assert _token_from_subprotocol(ws) == "header.payload.sig"

    def test_absent_marker_returns_none_for_query_fallback(self):
        assert _token_from_subprotocol(_ws(header="some-other-proto")) is None
        assert _token_from_subprotocol(_ws()) is None

    def test_marker_without_a_following_value_returns_none(self):
        assert _token_from_subprotocol(_ws(header="travian-jwt")) is None


class TestQueryRedactionFilter:
    def _record(self, msg, args):
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg=msg,
            args=args,
            exc_info=None,
        )

    def test_a_token_in_the_uvicorn_access_request_line_is_redacted(self):
        flt = QueryStringRedactionFilter()
        # uvicorn.access shape: msg is a format string, args carry the fields.
        record = self._record(
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/ws/logs?token=header.payload.sig&level=INFO", "1.1", 200),
        )
        flt.filter(record)
        rendered = record.getMessage()
        assert "header.payload.sig" not in rendered
        assert "token=[REDACTED]" in rendered
        assert "level=INFO" in rendered, "non-secret params must survive"

    def test_a_websocket_upgrade_line_is_redacted(self):
        flt = QueryStringRedactionFilter()
        record = self._record(
            '%s - "%s %s" [accepted]', ("127.0.0.1:1", "WebSocket", "/ws/logs?token=abc.def.ghi")
        )
        flt.filter(record)
        assert "abc.def.ghi" not in record.getMessage()

    def test_a_clean_line_is_untouched(self):
        flt = QueryStringRedactionFilter()
        record = self._record(
            '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", "/api/status", "1.1", 200)
        )
        flt.filter(record)
        assert record.getMessage().endswith('"GET /api/status HTTP/1.1" 200')
