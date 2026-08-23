"""A verbatim record of what a live distribution run decided and sent.

Why this exists: the trade-route write path used to carry a single ``logger.debug``
line. A run that created routes on a real account left no evidence of which
routes it considered, why it classified each one the way it did, what it put on
the wire, or how long the game took to answer. "It seemed to work" is not a
basis for turning live writes on, and neither is a summary that only reports
what succeeded.

Two sinks, deliberately:

* the stdlib logger, at INFO, for a human watching a run;
* a JSONL file per run, one event per line, for reading a run back afterwards
  and checking it against what the game actually shows.

The JSONL file is the point. Every decision carries its *reason*, not just its
outcome, so a wrong-but-plausible run (say, one that disables a route it should
have kept) is distinguishable from a correct one after the fact rather than only
while it happens.

Privacy: these files hold real village ids and coordinates. They live outside the
repository and must never be committed -- this repository is public.

Tracing must never break a run. A trace write happens after the decision it
describes and its failure is contained here: losing a log line is bad, and
failing a game request that already went out because a log line could not be
written is worse.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Alongside the database and cookie jar, not in the repo.
TRACE_DIR = Path.home() / ".travian" / "traces"

# A run that somehow loops would otherwise fill the disk. Well above any real
# run: the largest plan this app builds is a few hundred routes, and each one
# emits a handful of events.
MAX_EVENTS = 20_000


class ExecutionTrace:
    """One live-execution run's decisions and requests, as they happen.

    Use :meth:`event` for anything that does not have a dedicated helper. The
    helpers exist for the events whose *shape* matters, so that reading a run
    back does not depend on remembering which key a field was called.
    """

    def __init__(self, *, enabled: bool = True, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.enabled = enabled
        self.started = time.monotonic()
        self.counts: dict[str, int] = {}
        self._events = 0
        self._truncated = False
        self._closed = False
        self._path: Path | None = None
        self._handle = None
        if not enabled:
            return
        try:
            TRACE_DIR.mkdir(parents=True, exist_ok=True)
            self._path = TRACE_DIR / f"exec-{self.run_id}.jsonl"
            self._handle = self._path.open("a", encoding="utf-8")
        except OSError as exc:
            # Tracing is observability, not the operation. Say so loudly once
            # and carry on without a file rather than failing the run.
            logger.warning("execution trace disabled, could not open trace file: %s", exc)
            self.enabled = False

    @property
    def path(self) -> Path | None:
        """Where this run's JSONL trace is, or None if tracing is off."""
        return self._path

    def event(self, kind: str, **fields: Any) -> None:
        """Record one event. Never raises."""
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if not self.enabled:
            return
        if self._events >= MAX_EVENTS:
            if not self._truncated:
                self._truncated = True
                logger.warning(
                    "execution trace %s hit %d events; further events are counted but not written",
                    self.run_id,
                    MAX_EVENTS,
                )
            return
        self._write(kind, fields)

    def _write(self, kind: str, fields: dict[str, Any]) -> None:
        """Append one record. Bypasses the event cap; callers apply it."""
        record = {
            "run": self.run_id,
            "t": round(time.monotonic() - self.started, 3),
            "kind": kind,
            **fields,
        }
        try:
            self._handle.write(json.dumps(record, default=str, sort_keys=True) + "\n")
            self._handle.flush()  # a crashed run must still leave its trace behind
            self._events += 1
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("execution trace %s: could not write %s: %s", self.run_id, kind, exc)

    # ── The events whose shape matters ──────────────────────────────────────

    def decision(
        self,
        *,
        origin: int,
        destination: Any,
        decision: str,
        reason: str,
        matched_by: str | None = None,
        **extra: Any,
    ) -> None:
        """Why one desired route was skipped, created, blocked or deferred.

        ``matched_by`` records HOW an existing route was recognised -- village id
        or coordinates -- because those are two different code paths with two
        different failure modes, and a run that silently fell back from one to
        the other is exactly the bug worth catching.
        """
        self.event(
            "decision",
            origin=origin,
            destination=destination,
            decision=decision,
            reason=reason,
            matched_by=matched_by,
            **extra,
        )
        logger.info(
            "[%s] origin %s -> %s: %s (%s)",
            self.run_id,
            origin,
            destination,
            decision,
            reason,
        )

    def wrote(
        self,
        *,
        kind: str,
        origin: int,
        status: str,
        elapsed_ms: int,
        payload: Any = None,
        detail: str = "",
        **extra: Any,
    ) -> None:
        """A create/disable/enable that actually reached the game.

        The payload goes in verbatim. If a route turns up in-game with the wrong
        cargo or the wrong hour, the only way to tell a planner bug from a
        serialisation bug is to have the exact body that was sent.
        """
        self.event(
            "wrote",
            action=kind,
            origin=origin,
            status=status,
            elapsed_ms=elapsed_ms,
            payload=payload,
            detail=detail,
            **extra,
        )
        logger.info(
            "[%s] %s on origin %s -> %s in %dms%s",
            self.run_id,
            kind,
            origin,
            status,
            elapsed_ms,
            f" ({detail})" if detail else "",
        )

    def refused(self, *, kind: str, origin: int | None, reason: str, **extra: Any) -> None:
        """A write that was NOT sent, and why. The gates' side of the story."""
        self.event("refused", action=kind, origin=origin, reason=reason, **extra)
        logger.info("[%s] %s refused for origin %s: %s", self.run_id, kind, origin, reason)

    def close(self, **summary: Any) -> None:
        """Final tally. Idempotent, so the caller can close on the happy path and
        again in a ``finally`` without emitting two contradictory endings."""
        if self._closed:
            return
        self._closed = True
        self.counts["run_end"] = self.counts.get("run_end", 0) + 1
        if self.enabled:
            # Written past the event cap on purpose. A truncated run is exactly
            # the run whose ending matters most -- suppressing it left a trace
            # that simply stopped, indistinguishable from a process that died.
            self._write(
                "run_end",
                {
                    "elapsed_s": round(time.monotonic() - self.started, 2),
                    "event_counts": dict(self.counts),
                    "truncated": self._truncated,
                    **summary,
                },
            )
        logger.info(
            "[%s] run finished in %.1fs: %s",
            self.run_id,
            time.monotonic() - self.started,
            summary,
        )
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None


def read_inventories(run_id: str) -> dict[int, list[dict[str, Any]]]:
    """The pre-write route inventory each origin had, from a finished run's trace.

    This is what makes a revert possible at all: the game returns no id when it
    creates a route, so "what did that run add?" can only be answered by diffing
    a later read against exactly what was there first. The run already read every
    marketplace it touched, so that state is on disk and costs nothing to recover.

    Raises FileNotFoundError if there is no such trace, because silently
    returning "nothing was there" would make every existing route look created.
    """
    path = TRACE_DIR / f"exec-{run_id}.jsonl"
    inventories: dict[int, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue  # a torn final line from a killed process
            if event.get("kind") != "origin_read":
                continue
            origin = event.get("origin")
            inventory = event.get("inventory")
            if isinstance(origin, int) and isinstance(inventory, list):
                # First read wins: a re-read within one run would be a later
                # state, not the one the run started from.
                inventories.setdefault(origin, inventory)
    return inventories
