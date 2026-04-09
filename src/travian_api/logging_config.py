"""Logging configuration for Travian API."""

import logging
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


def setup_logging(level: Optional[str] = None) -> None:
    """
    Configure logging for the application.

    Args:
        level: Log level override. If None, uses settings.log_level
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

    # Configure third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    if settings.debug:
        logging.getLogger("travian_api").setLevel(logging.DEBUG)

        # Apply sensitive data filter — only redacts actual credential values
        sensitive_filter = SensitiveDataFilter()
        for handler in root_logger.handlers:
            handler.addFilter(sensitive_filter)


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(name)
