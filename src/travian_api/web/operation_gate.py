"""Per-user operation gate for long-running WS jobs.

Tracks active operations per user for visibility and captcha-stop
coordination.  Blocks concurrent operations of the SAME type for the
same user (prevents double-upgrade / double-raid race conditions from
multiple browser tabs).  Different operation types can still run in
parallel (e.g. queue + oasis-raider).
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class OperationGate:
    """Thread-safe registry of active operations per user.

    Registers operations for three purposes:
    1. Mutual exclusion — ``acquire()`` rejects a second instance of the
       same operation type for the same user, preventing race conditions
       from multiple browser tabs.
    2. Visibility — ``get_active()`` shows what's running.
    3. Captcha stop signal — ``set_should_stop()`` tells all active
       operations to halt after captcha resolution.
    """

    def __init__(self) -> None:
        # {user_id: {op_type: count}}
        self._active: dict[int, dict[str, int]] = {}
        # {user_id: monotonic timestamp when should_stop was set}
        self._should_stop: dict[int, float] = {}
        self._lock = threading.Lock()

    def acquire(self, user_id: int, op_type: str) -> bool:
        """Register an operation.

        Returns ``False`` (and does NOT register) when the same
        ``(user_id, op_type)`` pair is already active.  This prevents
        race conditions from multiple browser tabs running the same
        operation concurrently.
        """
        with self._lock:
            user_ops = self._active.setdefault(user_id, {})
            if user_ops.get(op_type, 0) > 0:
                logger.warning(
                    "Operation gate: REJECTED %s for user %s (already active)",
                    op_type, user_id,
                )
                return False
            user_ops[op_type] = 1
            logger.info(
                "Operation gate: registered %s for user %s",
                op_type, user_id,
            )
            return True

    def release(self, user_id: int, op_type: str) -> None:
        """Unregister an operation instance.

        When the last operation for a user is released, the should_stop
        flag is also cleared so future operations start clean.
        """
        with self._lock:
            user_ops = self._active.get(user_id)
            if user_ops and op_type in user_ops:
                user_ops[op_type] -= 1
                if user_ops[op_type] <= 0:
                    del user_ops[op_type]
                if not user_ops:
                    del self._active[user_id]
                    self._should_stop.pop(user_id, None)
                logger.info(
                    "Operation gate: released %s for user %s",
                    op_type, user_id,
                )
            else:
                logger.debug(
                    "Operation gate: release called for non-active %s user %s (no-op)",
                    op_type, user_id,
                )

    def get_active(self, user_id: int) -> list[str]:
        """Return list of active operation types for a user."""
        with self._lock:
            return list(self._active.get(user_id, {}).keys())

    def stop_all(self, user_id: int) -> list[str]:
        """Signal all operations for a user to stop and return active op types.

        Does NOT remove entries from ``_active`` — individual handlers
        will call ``release()`` themselves when they observe the stop
        signal and exit.  This avoids corrupting the gate state.
        """
        with self._lock:
            self._should_stop[user_id] = time.monotonic()
            released = list(self._active.get(user_id, {}).keys())
            if released:
                logger.info(
                    "Operation gate: stop_all for user %s: %s",
                    user_id, released,
                )
            return released

    def set_should_stop(self, user_id: int) -> None:
        """Signal all operations for a user to stop (e.g., after captcha resolve)."""
        with self._lock:
            self._should_stop[user_id] = time.monotonic()
            logger.info("Operation gate: should_stop set for user %s", user_id)

    def check_should_stop(self, user_id: int, started_after: float = 0.0) -> bool:
        """Check the should_stop flag.

        When *started_after* is provided (a ``time.monotonic()`` timestamp),
        only returns ``True`` if the flag was set **after** that timestamp.
        This prevents new operations from seeing stale stop signals that
        were meant for earlier operations.
        """
        with self._lock:
            ts = self._should_stop.get(user_id)
            if ts is None:
                return False
            return ts > started_after

    def clear_should_stop(self, user_id: int) -> None:
        """Clear the should_stop flag."""
        with self._lock:
            self._should_stop.pop(user_id, None)


# Global singleton
operation_gate = OperationGate()
