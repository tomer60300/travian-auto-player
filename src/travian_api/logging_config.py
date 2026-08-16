"""Logging configuration for Travian API."""

import logging
import re
import sys
from typing import Optional

from .config import settings


class SensitiveDataFilter(logging.Filter):
    """Redact actual credential values while preserving operational messages."""

    # Only redact log records whose message contains actual credential patterns
    _REDACT_PATTERNS = ("password=", "jwt=", "token=", "secret=", "Authorization:")

    def filter(self, record: logging.LogRecord) -> bool:
        if hasattr(record, "msg"):
            msg = str(record.msg)
            if any(p in msg for p in self._REDACT_PATTERNS):
                record.msg = "[REDACTED - Sensitive data filtered]"
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

    if settings.debug:
        logging.getLogger("travian_api").setLevel(logging.DEBUG)

        # Apply sensitive data filter — only redacts actual credential values
        sensitive_filter = SensitiveDataFilter()
        for handler in root_logger.handlers:
            handler.addFilter(sensitive_filter)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)
