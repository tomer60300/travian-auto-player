"""Per-user operation gate to prevent overlapping long-running WS jobs.

Each user can only run one instance of each operation type at a time.
Operation types: farm, farm-all, scout, scout-scan, queue, oasis-raider, farm-builder.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


class OperationGate:
    """Thread-safe registry of active operations per user.

    Prevents the same user from running two instances of the same
    operation type concurrently (e.g., two farm loops).

    Also tracks a ``should_stop`` flag per user that is set when captcha
    is resolved, so operations don't auto-resume after a captcha block.
    """

    def __init__(self) -> None:
        self._active: dict[int, dict[str, bool]] = {}  # {user_id: {op_type: True}}
        self._should_stop: dict[int, bool] = {}  # {user_id: True when captcha resolved}
        self._lock = threading.Lock()

    def acquire(self, user_id: int, op_type: str) -> bool:
        """Try to acquire an operation slot.

        Returns True if acquired, False if the same op_type is already
        running for this user.
        """
        with self._lock:
            user_ops = self._active.setdefault(user_id, {})
            if user_ops.get(op_type):
                logger.warning(
                    "Operation gate: blocked duplicate %s for user %s",
                    op_type, user_id,
                )
                return False
            user_ops[op_type] = True
            logger.info(
                "Operation gate: acquired %s for user %s",
                op_type, user_id,
            )
            return True

    def release(self, user_id: int, op_type: str) -> None:
        """Release an operation slot.

        When the last operation for a user is released, the should_stop
        flag is also cleared so future operations start clean.
        """
        with self._lock:
            user_ops = self._active.get(user_id)
            if user_ops:
                user_ops.pop(op_type, None)
                if not user_ops:
                    del self._active[user_id]
                    # All ops done — clear the stop flag for next session
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
        """Check the should_stop flag (non-destructive — all operations see it).

        The flag persists until explicitly cleared via ``clear_should_stop``,
        so every active operation for this user will observe ``True``.
        """
        with self._lock:
            return self._should_stop.get(user_id, False)

    def clear_should_stop(self, user_id: int) -> None:
        """Clear the should_stop flag after all operations have acknowledged it."""
        with self._lock:
            self._should_stop.pop(user_id, None)


# Global singleton
operation_gate = OperationGate()
