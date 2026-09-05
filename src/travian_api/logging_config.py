"""Logging configuration for Travian API."""

import logging
import re
import sys
from typing import Optional

from .config import settings

# `secret=`/`token=`/... take the rest of the word; `Authorization:` takes the
# rest of the line, because the value there is `<scheme> <credential>` and
# stopping at the first space would leave the credential behind.
_KEYED_VALUE = re.compile(r"(?i)\b(password|jwt|token|secret)=\S+")
_AUTH_HEADER = re.compile(r"(?i)(Authorization:)\s*.*")


def redact_sensitive(text: str) -> str:
    """Replace credential values in *text*, surgically -- the value goes, the
    message stays.

    Module-level because a log record is not the only thing this app writes
    down: ``debug_dump`` puts whole authenticated game pages on disk and runs
    them through this same function, so there is one definition of "what counts
    as a credential" rather than one per sink.
    """
    text = _KEYED_VALUE.sub(r"\1=[REDACTED]", text)
    return _AUTH_HEADER.sub(r"\1 [REDACTED]", text)


class SensitiveDataFilter(logging.Filter):
    """Redact actual credential values while preserving operational messages.

    Scrubs ``record.msg`` AND every string in ``record.args``, because the args
    are where credentials actually arrive: a call like
    ``logger.warning("Auto-reconnect failed for user %s: %s", user.id, exc)``
    has a format string that matches no pattern at all, while the exception's
    ``__str__`` in the args carries whatever it carried. Inspecting only the
    format string caught nothing but a credential a developer had typed into a
    literal.

    Redaction is surgical -- the value goes, the message stays -- so a redacted
    record still says what failed. Attached UNCONDITIONALLY by
    :func:`setup_logging`, like its sibling below: it used to be installed only
    under ``settings.debug``, which defaults False, so the default configuration
    (the one both servers run) had no credential redaction at all.
    """

    def _scrub(self, value: str) -> str:
        return redact_sensitive(value)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._scrub(v) if isinstance(v, str) else v) for k, v in record.args.items()
                }
        return True


class QueryStringRedactionFilter(logging.Filter):
    """Redact secret query params anywhere they surface in a log record.

    uvicorn's access logger renders the request line (path + query) into
    record.args at INFO, so a `?token=<jwt>` on a WebSocket upgrade would
    otherwise write a reusable bearer token to the logs. This scrubs the
    value while leaving the parameter name visible, and runs UNCONDITIONALLY
    (not only in debug) so production logs are covered. It mutates both
    record.msg and each string in record.args in place.
    """

    _SECRET_QUERY = re.compile(r"(?i)([?&](?:token|jwt|access_token|password|secret)=)[^&\s\"']+")

    def _scrub(self, value: str) -> str:
        return self._SECRET_QUERY.sub(r"\1[REDACTED]", value)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str) and "=" in record.msg:
            record.msg = self._scrub(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    self._scrub(a) if isinstance(a, str) else a for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: (self._scrub(v) if isinstance(v, str) else v) for k, v in record.args.items()
                }
        return True


def setup_logging(level: Optional[str] = None, *, attach_broadcast: bool = False) -> None:
    """
    Configure logging for the application.

    Args:
        level: Log level override. If None, uses settings.log_level
        attach_broadcast: If True, attach the web LogBroadcastHandler to the root logger
    """
    log_level = level or settings.log_level

    # Create formatter
    formatter = logging.Formatter(
        fmt="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Attach broadcast handler for web UI log streaming
    if attach_broadcast:
        try:
            from travian_api.web.log_broadcast import log_broadcast_handler

            log_broadcast_handler.setFormatter(formatter)
            root_logger.addHandler(log_broadcast_handler)
        except ImportError:
            pass

    # Configure third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Redact secret query params everywhere, always. Attached to our handlers
    # and directly to uvicorn's access logger, which renders the request line
    # (path + query) into its own records and would otherwise log bearer
    # tokens from any legacy `?token=` WebSocket upgrade.
    query_filter = QueryStringRedactionFilter()
    for handler in root_logger.handlers:
        handler.addFilter(query_filter)
    logging.getLogger("uvicorn.access").addFilter(query_filter)

    # Credential values, redacted everywhere, always. This used to sit inside
    # `if settings.debug:` -- and debug defaults False -- so the configuration
    # both servers actually run had no credential redaction at all, while the
    # query-string filter one line up was correctly unconditional.
    sensitive_filter = SensitiveDataFilter()
    for handler in root_logger.handlers:
        handler.addFilter(sensitive_filter)
    logging.getLogger("uvicorn.access").addFilter(sensitive_filter)

    if settings.debug:
        logging.getLogger("travian_api").setLevel(logging.DEBUG)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)
