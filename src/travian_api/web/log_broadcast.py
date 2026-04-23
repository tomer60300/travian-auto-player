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
    """Manages per-subscriber queues and a shared ring buffer.

    Subscribers are tagged with a user_id so that log entries are only
    delivered to the user that generated them. This prevents cross-user
    log leakage when multiple users are connected simultaneously.
    """

    def __init__(self, buffer_size: int = RING_BUFFER_SIZE) -> None:
        self._buffer: deque[dict] = deque(maxlen=buffer_size)
        # {subscriber_id: (queue, user_id)}
        self._subscribers: dict[int, tuple[asyncio.Queue, int | None]] = {}
        self._lock = Lock()
        self._min_level = logging.INFO

    @property
    def min_level(self) -> int:
        return self._min_level

    @min_level.setter
    def min_level(self, value: int) -> None:
        self._min_level = value

    def push(self, entry: dict) -> None:
        """Push a log entry to the ring buffer and matching subscribers.

        Delivery rules: an entry is sent to a subscriber when:
        - Both entry and subscriber have the same ``user_id``, OR
        - The subscriber has no ``user_id`` filter (admin/system viewer).
        Entries without ``user_id`` (generic Python logger output) are
        only delivered to unscoped subscribers — never to user-scoped
        ones — to prevent cross-user log leakage.
        """
        entry_user = entry.get("user_id")
        with self._lock:
            self._buffer.append(entry)
            for sub_id, (q, sub_user) in self._subscribers.items():
                # Scoped subscriber only sees entries tagged with their user_id
                if sub_user is not None and (entry_user is None or entry_user != sub_user):
                    continue
                # Deliver: unscoped subscriber sees everything, or user_id matches
                try:
                    q.put_nowait(entry)
                except asyncio.QueueFull:
                    try:
                        q.get_nowait()
                        q.put_nowait(entry)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def subscribe(self, subscriber_id: int, user_id: int | None = None) -> asyncio.Queue:
        """Register a subscriber and return its queue.

        Args:
            subscriber_id: Unique ID for this subscription (e.g., id(websocket)).
            user_id: If set, only receive logs tagged with this user_id
                (plus system logs that have no user_id).

        Returns:
            An asyncio.Queue that will receive matching log dicts.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers[subscriber_id] = (q, user_id)
        return q

    def unsubscribe(self, subscriber_id: int) -> None:
        """Remove a subscriber."""
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def get_history(self, count: int = 100, user_id: int | None = None) -> list[dict]:
        """Return the last *count* entries from the ring buffer.

        Args:
            count: Maximum number of entries to return.
            user_id: If set, only return entries for that user (plus
                system entries that have no user_id).
        """
        with self._lock:
            items = list(self._buffer)
        if user_id is not None:
            # Only return entries explicitly tagged with this user_id (no cross-user leakage)
            items = [e for e in items if e.get("user_id") == user_id]
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
