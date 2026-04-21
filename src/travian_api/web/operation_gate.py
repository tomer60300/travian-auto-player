"""Per-user operation tracker for long-running WS jobs.

Tracks active operations per user for visibility and captcha-stop
coordination.  Does NOT block parallel operations — multiple instances
of the same operation type can run concurrently.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class OperationGate:
    """Thread-safe registry of active operations per user.

    Registers operations for two purposes:
    1. Visibility — ``get_active()`` shows what's running.
    2. Captcha stop signal — ``set_should_stop()`` tells all active
       operations to halt after captcha resolution.

    Multiple instances of the same operation type are allowed.
    """

    def __init__(self) -> None:
        # {user_id: {op_type: count}}
        self._active: dict[int, dict[str, int]] = {}
        self._should_stop: dict[int, bool] = {}
        self._lock = threading.Lock()

    def acquire(self, user_id: int, op_type: str) -> bool:
        """Register an operation. Always returns True (never blocks)."""
        with self._lock:
            user_ops = self._active.setdefault(user_id, {})
            user_ops[op_type] = user_ops.get(op_type, 0) + 1
            logger.info(
                "Operation gate: registered %s for user %s (count: %d)",
                op_type, user_id, user_ops[op_type],
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

    def get_active(self, user_id: int) -> list[str]:
        """Return list of active operation types for a user."""
        with self._lock:
            return list(self._active.get(user_id, {}).keys())

    def stop_all(self, user_id: int) -> list[str]:
        """Release all operations for a user. Returns the list of released op types."""
        with self._lock:
            user_ops = self._active.pop(user_id, {})
            released = list(user_ops.keys())
            self._should_stop.pop(user_id, None)
            if released:
                logger.info(
                    "Operation gate: released all for user %s: %s",
                    user_id, released,
                )
            return released

    def set_should_stop(self, user_id: int) -> None:
        """Signal all operations for a user to stop (e.g., after captcha resolve)."""
        with self._lock:
            self._should_stop[user_id] = True
            logger.info("Operation gate: should_stop set for user %s", user_id)

    def check_should_stop(self, user_id: int) -> bool:
        """Check the should_stop flag (non-destructive — all operations see it)."""
        with self._lock:
            return self._should_stop.get(user_id, False)

    def clear_should_stop(self, user_id: int) -> None:
        """Clear the should_stop flag."""
        with self._lock:
            self._should_stop.pop(user_id, None)


# Global singleton
operation_gate = OperationGate()
