"""In-memory log broadcast system for streaming Python logs to the web UI.

Components
----------
LogBroadcastHandler : logging.Handler
    Captures log records and pushes them to subscribers.
LogStreamManager : singleton
    Manages per-user asyncio.Queues and a shared ring buffer for catch-up.
"""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from collections import deque
from threading import Lock
from typing import Any

# Mapping from Python log levels to frontend-friendly names
_LEVEL_MAP = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}

RING_BUFFER_SIZE = 500


def _format_record(record: logging.LogRecord) -> dict[str, Any]:
    """Convert a LogRecord into a JSON-serialisable dict."""
    detail = None
    if record.exc_info and record.exc_info[1]:
        detail = "".join(traceback.format_exception(*record.exc_info))

    return {
        "timestamp": record.created,
        "level": _LEVEL_MAP.get(record.levelno, "info"),
        "source": _source_from_logger(record.name),
        "message": record.getMessage(),
        "detail": detail,
        "logger": record.name,
    }


def _source_from_logger(name: str) -> str:
    """Derive a frontend-friendly source tag from the Python logger name."""
    n = name.lower()
    if "farm" in n:
        return "farm"
    if "scout" in n:
        return "scout"
    if "queue" in n or "build" in n:
        return "queue"
    if "military" in n or "troop" in n:
        return "military"
    if "video" in n or "reward" in n:
        return "video"
    if "report" in n or "raid" in n:
        return "reports"
    if "auth" in n or "session" in n or "user" in n:
        return "auth"
    if "http" in n or "client" in n:
        return "api"
    if "village" in n or "resource" in n:
        return "game"
    if "ws" in n or "websocket" in n:
        return "ws"
    return "server"


class LogStreamManager:
    """Manages per-subscriber queues and a shared ring buffer."""

    def __init__(self, buffer_size: int = RING_BUFFER_SIZE) -> None:
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        self._subscribers: dict[int, asyncio.Queue] = {}
        self._lock = Lock()
        self._min_level = logging.INFO

    @property
    def min_level(self) -> int:
        return self._min_level

    @min_level.setter
    def min_level(self, value: int) -> None:
        self._min_level = value

    def push(self, entry: dict) -> None:
        """Push a log entry to all subscribers (thread-safe)."""
        with self._lock:
            self._buffer.append(entry)
            for q in self._subscribers.values():
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    # Drop oldest if subscriber is slow
                    try:
                        q.get_nowait()
                        q.put_nowait(entry)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def subscribe(self, subscriber_id: int) -> asyncio.Queue:
        """Register a subscriber and return its queue."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers[subscriber_id] = q
        return q

    def unsubscribe(self, subscriber_id: int) -> None:
        """Remove a subscriber."""
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def get_history(self, count: int = 100) -> list[dict]:
        """Return the last *count* entries from the ring buffer."""
        with self._lock:
            items = list(self._buffer)
        return items[-count:]


class LogBroadcastHandler(logging.Handler):
    """A logging handler that pushes records to the LogStreamManager."""

    def __init__(self, manager: LogStreamManager, level: int = logging.DEBUG) -> None:
        super().__init__(level)
        self.manager = manager

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < self.manager.min_level:
            return
        try:
            entry = _format_record(record)
            self.manager.push(entry)
        except Exception:
            self.handleError(record)


# Global singleton
log_stream_manager = LogStreamManager()
log_broadcast_handler = LogBroadcastHandler(log_stream_manager)
