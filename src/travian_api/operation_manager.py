"""Background-task manager for long-running per-user operations.

Decouples the lifetime of a long-running operation (oasis raid, farm loop,
build queue executor, scout sweep, …) from any single WebSocket
connection. The op runs as an ``asyncio.Task`` owned by ``OperationManager``;
WebSockets become thin subscribers to the op's :class:`ExecutionSession`.

Why: iOS Safari (and any aggressive browser background policy) tears down
WebSockets when the tab is hidden. The previous design ran the op loop
*inside* the WS handler, so backgrounding the tab killed mid-flight raids
or queues. With this manager, the WS dropping is a non-event for the op —
the user can come back, reconnect to ``/ws/sessions/{id}/stream``, and pick
up the live tail plus full message history.

Design notes
------------
* Single source of truth: ``self._ops`` keyed by ``session_id`` (which equals
  ``ExecutionSession.id``).
* asyncio is single-threaded, so no locks are needed for dict mutations.
* Stop semantics: callers (the WS handler, the cross-device stop endpoint,
  captcha resolution) call :meth:`request_stop`; the op coro polls
  :meth:`OperationContext.should_stop` and exits gracefully.
* Terminal states are pushed into the exec session as messages so subscribers
  observe them naturally; the manager then unregisters and disconnects the
  session, which fans a ``None`` sentinel to live subscribers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from travian_api.web.execution_sessions import ExecutionSession, exec_session_manager
from travian_api.web.operation_gate import active_ops, captcha_stop

logger = logging.getLogger(__name__)


# Operation lifecycle states. Distinct from ExecutionSession.status which
# tracks subscriber-stream state ("running" | "disconnected").
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
STOPPED = "stopped"


@dataclass
class OperationContext:
    """Hand passed to the op coroutine. Replaces the old `tracked_send` closure."""

    session: Any  # TravianSession — typed as Any to avoid an import cycle.
    user_id: int
    exec_session: ExecutionSession
    started_at: float
    _stop_event: asyncio.Event

    @property
    def session_id(self) -> str:
        return self.exec_session.id

    def push(self, data: dict) -> None:
        """Buffer a message and fan it out to any live subscribers.

        Sync (non-blocking). Adds a timestamp if absent.
        """
        exec_session_manager.push(self.exec_session.id, data)

    def should_stop(self) -> bool:
        """True if the user requested stop OR a captcha resolution post-start."""
        if self._stop_event.is_set():
            return True
        return captcha_stop.should_stop(self.user_id, self.started_at)

    async def wait_or_stop(self, seconds: float) -> bool:
        """Sleep up to *seconds*. Returns True if stop fired during the wait.

        Watches the explicit stop event only — captcha_stop is timestamp-based
        and would need separate polling. Callers that care about captcha
        resolution during long sleeps should re-check ``should_stop`` after.
        """
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False


@dataclass
class Operation:
    """Handle returned by :meth:`OperationManager.start`."""

    session_id: str
    user_id: int
    label: str  # also the active_ops registry key (e.g. "oasis-raider")
    task: asyncio.Task
    stop_event: asyncio.Event
    started_at: float
    status: str = RUNNING


OperationCoro = Callable[[OperationContext], Awaitable[None]]


class OperationManager:
    """Per-user registry of detached background operations."""

    def __init__(self) -> None:
        # Keyed by session_id so subscribers can look up + signal an op via
        # the same identifier they hold in localStorage on the client.
        self._ops: dict[str, Operation] = {}

    def start(
        self,
        *,
        user_id: int,
        label: str,
        session_type: str,
        session_label: str,
        session: Any,
        coro: OperationCoro,
        extra_labels: tuple[str, ...] | list[str] = (),
        require_unique_label: bool = False,
        require_unique_extras: bool = False,
    ) -> Operation | None:
        """Spawn *coro* as a managed task. Returns a handle the caller can
        share with WS subscribers via the session_id field.

        ``require_unique_label`` enforces atomic uniqueness on the primary
        ``label``. ``require_unique_extras`` does the same on ``extra_labels``.
        They're independent because some ops have a non-unique umbrella
        label (e.g. ``farm-all`` — multiple disjoint runs are allowed) but
        per-resource extras (``farm:{lid}``) that must not collide.

        When either check sees an existing entry, ``start`` returns ``None``
        without spawning anything. This closes the TOCTOU window in WS
        handlers that do an early policy check then await the client
        config — two near-simultaneous tabs could otherwise both pass the
        early check and both reach ``start``.

        ``extra_labels`` reserves additional ``active_ops`` slots for the
        op's lifetime — useful when one op covers multiple distinct
        resources (run-all reserves ``farm:{lid}`` for every list it
        touches so a parallel single-list run on the same list is rejected
        cleanly). They're released alongside the primary label on
        termination.
        """
        # Sync block: check-and-register is atomic in asyncio because no
        # await happens between the policy check and the register call.
        if require_unique_label or require_unique_extras:
            existing_labels = set(active_ops.get_active(user_id))
            if require_unique_label and label in existing_labels:
                return None
            if require_unique_extras:
                for extra in extra_labels:
                    if extra in existing_labels:
                        return None

        exec_session = exec_session_manager.create(user_id, session_type, session_label)
        stop_event = asyncio.Event()
        started_at = time.monotonic()
        ctx = OperationContext(
            session=session,
            user_id=user_id,
            exec_session=exec_session,
            started_at=started_at,
            _stop_event=stop_event,
        )

        active_ops.register(user_id, label)
        for extra in extra_labels:
            active_ops.register(user_id, extra)
        # The session_init message lets every subscriber (including the
        # initiating WS) see which session_id to remember for reconnects.
        ctx.push({"type": "session_init", "session_id": exec_session.id})

        task = asyncio.create_task(
            self._run(coro, ctx, label, tuple(extra_labels)),
            name=f"op:{label}:{exec_session.id}",
        )
        op = Operation(
            session_id=exec_session.id,
            user_id=user_id,
            label=label,
            task=task,
            stop_event=stop_event,
            started_at=started_at,
        )
        self._ops[exec_session.id] = op
        logger.info(
            "Operation started: label=%s session=%s user=%s extras=%s",
            label,
            exec_session.id,
            user_id,
            list(extra_labels),
        )
        return op

    async def _run(
        self,
        coro: OperationCoro,
        ctx: OperationContext,
        label: str,
        extra_labels: tuple[str, ...],
    ) -> None:
        """Execute the op coro and translate every termination to a terminal
        message + cleanup. This is the only function that touches op.status."""
        try:
            await coro(ctx)
            # Decide the terminal status from observable signals:
            #   1. Coros that hit a fatal error usually push
            #      ``{"type":"error", "fatal": True}`` and return rather
            #      than raise. Detect that by scanning the tail of the
            #      ring buffer; mark FAILED so subscribers don't see a
            #      successful completion alongside a fatal error frame.
            #   2. Coros that returned because the user requested stop
            #      (or a captcha resolution fired) → STOPPED.
            #   3. Otherwise the coro reached its natural end → COMPLETED.
            terminal: str
            if _last_message_was_fatal_error(ctx.exec_session):
                terminal = FAILED
            elif ctx.should_stop():
                terminal = STOPPED
            else:
                terminal = COMPLETED
            self._set_status(ctx.session_id, terminal)
            ctx.push({"type": "operation_complete", "status": terminal})
        except asyncio.CancelledError:
            self._set_status(ctx.session_id, STOPPED)
            ctx.push({"type": "operation_complete", "status": STOPPED})
            raise
        except Exception as exc:
            logger.exception(
                "Operation %s failed: user=%s session=%s",
                label,
                ctx.user_id,
                ctx.session_id,
            )
            self._set_status(ctx.session_id, FAILED)
            ctx.push(
                {
                    "type": "error",
                    "message": str(exc),
                    "fatal": True,
                }
            )
            ctx.push({"type": "operation_complete", "status": FAILED})
        finally:
            for extra in extra_labels:
                active_ops.unregister(ctx.user_id, extra)
            active_ops.unregister(ctx.user_id, label)
            # Disconnect the session AFTER the terminal message so live
            # subscribers see the terminal state before the None sentinel.
            exec_session_manager.mark_disconnected(ctx.session_id)
            self._ops.pop(ctx.session_id, None)
            logger.info(
                "Operation ended: label=%s session=%s status=%s",
                label,
                ctx.session_id,
                self._get_status_or_unknown(ctx.session_id),
            )

    def _set_status(self, session_id: str, status: str) -> None:
        op = self._ops.get(session_id)
        if op is not None:
            op.status = status

    def _get_status_or_unknown(self, session_id: str) -> str:
        op = self._ops.get(session_id)
        return op.status if op is not None else "unknown"

    def get(self, session_id: str) -> Operation | None:
        return self._ops.get(session_id)

    def list_for_user(self, user_id: int) -> list[Operation]:
        return [op for op in self._ops.values() if op.user_id == user_id]

    def find_by_label(self, user_id: int, label: str) -> Operation | None:
        for op in self._ops.values():
            if op.user_id == user_id and op.label == label:
                return op
        return None

    def request_stop(self, session_id: str) -> bool:
        """Signal graceful stop. Returns True iff an op with this id existed."""
        op = self._ops.get(session_id)
        if op is None:
            return False
        op.stop_event.set()
        logger.info("Operation %s: stop requested", session_id)
        return True


def _last_message_was_fatal_error(exec_session: ExecutionSession) -> bool:
    """Look at the tail of the ring buffer for a recent fatal error.

    Coros sometimes push a couple of follow-up frames (e.g. a status
    update) after the fatal error, so we don't restrict to "the very
    last message" — checking the last few entries is enough to catch
    the common pattern without being defeated by trivial trailing
    frames. Bounded scan keeps this O(1).
    """
    msgs = exec_session.messages
    if not msgs:
        return False
    for msg in list(msgs)[-5:]:
        if (
            isinstance(msg, dict)
            and msg.get("type") == "error"
            and bool(msg.get("fatal"))
        ):
            return True
    return False


# Module-level singleton — every WS handler imports this.
operation_manager = OperationManager()
