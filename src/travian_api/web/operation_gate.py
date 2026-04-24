"""Per-user operation tracking and captcha stop signalling.

Two independent concerns, two primitives:

* :class:`ActiveOpRegistry` — tracks what long-running operations a user has
  in flight. Visibility only; it does not block anything. Double-action
  prevention lives at the service layer (see ``KeyedLock`` usage in
  ``BuildingService``, ``FarmListService``, ``MilitaryService``).

* :class:`CaptchaStopSignal` — broadcasts "halt now" to every in-flight
  operation after a captcha is resolved, so handlers can exit their loops
  cleanly and the user can restart at will.

Both are single-threaded asyncio-safe (dict ops are atomic across awaits in
a single event loop). Locks are deliberately absent — nothing here is worth
a lock.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class ActiveOpRegistry:
    """Display-only record of a user's active long-running operations."""

    def __init__(self) -> None:
        self._active: dict[int, dict[str, int]] = {}

    def register(self, user_id: int, label: str) -> None:
        """Record that *label* is running for *user_id*. Always succeeds."""
        user_ops = self._active.setdefault(user_id, {})
        user_ops[label] = user_ops.get(label, 0) + 1
        logger.info("Active ops: %s started for user %s", label, user_id)

    def unregister(self, user_id: int, label: str) -> None:
        """Remove one instance of *label* for *user_id*.

        When the last op for a user ends, any lingering captcha-stop signal
        is also cleared — a stale signal has no meaning once nothing is
        running, and keeping it would leak one dict entry per user forever.
        """
        user_ops = self._active.get(user_id)
        if not user_ops or label not in user_ops:
            return
        user_ops[label] -= 1
        if user_ops[label] <= 0:
            del user_ops[label]
        if not user_ops:
            del self._active[user_id]
            captcha_stop.clear(user_id)
        logger.info("Active ops: %s ended for user %s", label, user_id)

    def get_active(self, user_id: int) -> list[str]:
        return list(self._active.get(user_id, {}).keys())


class CaptchaStopSignal:
    """Broadcast a 'stop now' timestamp after captcha resolution.

    An operation checks ``should_stop(user_id, started_after=op_start_ts)``
    each loop tick; it returns True only if the signal was raised *after*
    the op started, so fresh operations never inherit a stale signal.
    """

    def __init__(self) -> None:
        self._ts: dict[int, float] = {}

    def signal(self, user_id: int) -> None:
        self._ts[user_id] = time.monotonic()
        logger.info("Captcha stop: signalled for user %s", user_id)

    def should_stop(self, user_id: int, started_after: float = 0.0) -> bool:
        ts = self._ts.get(user_id)
        return ts is not None and ts > started_after

    def clear(self, user_id: int) -> None:
        self._ts.pop(user_id, None)


# Module-level singletons.
active_ops = ActiveOpRegistry()
captcha_stop = CaptchaStopSignal()
