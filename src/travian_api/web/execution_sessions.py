"""In-memory execution session registry for remote log mirroring.

Each long-running WebSocket task (build queue, farm loop, auto-scout) registers
an ExecutionSession on start.  All messages sent to the initiating client are
also pushed into the session's ring buffer and fanned out to any subscribers
(e.g. a second device watching the same session).

Sessions are kept for 24 hours after the task ends, then cleaned up by a
background asyncio task.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_SESSION_TTL = 86400  # 24 hours
_MAX_MESSAGES = 2000
_SUBSCRIBER_QUEUE_SIZE = 500
_CLEANUP_INTERVAL = 300  # 5 minutes


@dataclass
class ExecutionSession:
    id: str
    user_id: int
    session_type: str  # "queue" | "farm-run" | "farm-run-all" | "scout-auto" | "scout-scan"
    label: str
    status: str = "running"  # "running" | "disconnected"
    created_at: float = field(default_factory=time.time)
    disconnected_at: float | None = None
    messages: deque = field(default_factory=lambda: deque(maxlen=_MAX_MESSAGES))
    _subscribers: dict[int, asyncio.Queue] = field(default_factory=dict)


class ExecutionSessionManager:
    """Manages execution sessions with message buffering and live subscriber fan-out."""

    def __init__(self) -> None:
        self._sessions: dict[str, ExecutionSession] = {}
        self._lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task | None = None

    def create(self, user_id: int, session_type: str, label: str) -> ExecutionSession:
        """Create and register a new execution session."""
        session_id = os.urandom(4).hex()
        session = ExecutionSession(
            id=session_id,
            user_id=user_id,
            session_type=session_type,
            label=label,
        )
        self._sessions[session_id] = session
        logger.info(
            "Execution session created: id=%s type=%s user=%s label=%s",
            session_id,
            session_type,
            user_id,
            label,
        )
        return session

    def get(self, session_id: str) -> ExecutionSession | None:
        return self._sessions.get(session_id)

    def list_for_user(self, user_id: int) -> list[dict]:
        """Return running + disconnected-within-TTL sessions for a user."""
        now = time.time()
        result = []
        for s in self._sessions.values():
            if s.user_id != user_id:
                continue
            if (
                s.status == "disconnected"
                and s.disconnected_at
                and (now - s.disconnected_at) > _SESSION_TTL
            ):
                continue
            result.append(
                {
                    "id": s.id,
                    "session_type": s.session_type,
                    "label": s.label,
                    "status": s.status,
                    "created_at": s.created_at,
                    "disconnected_at": s.disconnected_at,
                    "message_count": len(s.messages),
                }
            )
        # Running first, then disconnected; within each group, newest first
        result.sort(key=lambda x: (0 if x["status"] == "running" else 1, -x["created_at"]))
        return result

    def push(self, session_id: str, data: dict) -> None:
        """Buffer a message and fan out to all live subscribers."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        # Add timestamp if not present
        if "ts" not in data:
            data = {**data, "ts": time.time()}
        session.messages.append(data)
        # Fan-out to subscribers (snapshot to avoid dict-changed-during-iteration)
        for sub_id, queue in list(session._subscribers.items()):
            try:
                queue.put_nowait(data)
            except asyncio.QueueFull:
                # Drop oldest to make room
                try:
                    queue.get_nowait()
                    queue.put_nowait(data)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def subscribe(self, session_id: str, sub_id: int) -> tuple[list[dict], asyncio.Queue] | None:
        """Subscribe to a session's live output.

        Returns (history_snapshot, live_queue) or None if session not found.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return None
        history = list(session.messages)
        queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_SIZE)
        session._subscribers[sub_id] = queue
        logger.info(
            "Session %s: subscriber %s added (%d existing)",
            session_id,
            sub_id,
            len(session._subscribers),
        )
        return history, queue

    def unsubscribe(self, session_id: str, sub_id: int) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        session._subscribers.pop(sub_id, None)
        logger.info("Session %s: subscriber %s removed", session_id, sub_id)

    def mark_disconnected(self, session_id: str) -> None:
        """Mark a session as disconnected and notify all subscribers."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        session.status = "disconnected"
        session.disconnected_at = time.time()
        # Push None sentinel to all subscribers so they break out of their stream loops
        for sub_id, queue in list(session._subscribers.items()):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                    queue.put_nowait(None)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        logger.info("Execution session disconnected: id=%s", session_id)

    def start_cleanup(self) -> None:
        """Start the background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info("Execution session cleanup task started")

    def stop_cleanup(self) -> None:
        """Stop the background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            logger.info("Execution session cleanup task stopped")

    async def _cleanup_loop(self) -> None:
        """Periodically remove expired disconnected sessions."""
        try:
            while True:
                await asyncio.sleep(_CLEANUP_INTERVAL)
                now = time.time()
                expired = [
                    sid
                    for sid, s in self._sessions.items()
                    if s.status == "disconnected"
                    and s.disconnected_at
                    and (now - s.disconnected_at) > _SESSION_TTL
                ]
                for sid in expired:
                    del self._sessions[sid]
                if expired:
                    logger.info("Cleaned up %d expired execution sessions", len(expired))
        except asyncio.CancelledError:
            pass


# Global singleton
exec_session_manager = ExecutionSessionManager()
