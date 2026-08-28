"""A zero-game-request audit of past live /distribution/execute runs.

Reads the JSONL traces ``execution_trace.ExecutionTrace`` writes to
``~/.travian/traces`` (see that module for the event shapes) and turns them
into something an operator can trust without opening Travian.

SCOPE, and why it matters more than any feature here: a trace records what
THIS APP decided and put on the wire, and -- only where a run verified it --
what the marketplace read-back showed immediately afterwards. It never learns
what the game did with a shipment later: whether it actually fired on
schedule, whether it arrived, whether an operator changed something by hand
in-game after the run ended. So "created" below means "the app wrote this and,
where verified, saw the route appear", never "N sends fired" or "M deliveries
landed in-window" -- those are facts about the game that only reading the game
would give. This is a WRITE-HISTORY / audit report, not a delivery report.

Pure and read-only: everything here is file reads under ``trace_dir``, no
network, no database.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_TRACE_GLOB = "exec-*.jsonl"


@dataclass(frozen=True)
class RunSummary:
    """One live run, as its own trace recorded it."""

    run_id: str
    # Traces carry a MONOTONIC offset (`t`) from when the run started, never a
    # wall-clock timestamp -- there is nothing in the file itself to date it
    # by. The trace file's own mtime is the honest source for "when", and nothing
    # more precise than that is available without changing what gets recorded.
    started_at: datetime
    # None only when even run_start is missing -- a trace torn off before its
    # very first line ever landed.
    live_enabled: bool | None
    # Whether a run_end event was found at all. False is the truncated/killed
    # case: the process died (or was killed) before ExecutionTrace.close() ran,
    # or its last line was cut off mid-write and could not be parsed.
    complete: bool
    # Whether a run_failed event was found -- an exception aborted the run
    # after some writes may already have committed against the account.
    failed: bool
    error: str | None
    # The run itself hit MAX_EVENTS and stopped WRITING further events. This is
    # NOT the same thing as `complete` being False: an event-cap run still
    # reaches its own run_end.
    event_cap_truncated: bool
    elapsed_s: float | None
    # Below: run_end's own tally, verbatim. None (not 0) whenever run_end is
    # missing or -- as with a run_failed trace's fallback close() -- present but
    # without these fields, so "unknown" is never reported as "zero".
    created: int | None
    created_unverified: int | None
    not_created: int | None
    created_game_rows: int | None
    disabled: int | None
    re_enabled: int | None
    cargo_updated: int | None
    deferred: int | None
    outstanding: int | None
    problems: int | None
    stopped_early: bool | None
    gold_club_blocked: bool | None
    # Counted straight from the raw events rather than from run_end's totals,
    # so these stay known even for a run that never reached its own ending.
    verify_failures: int
    schedule_mismatch_origins: tuple[int, ...]
    # True when there is something here an operator should look at: an
    # unverified or missing create, a verify failure, a reported problem, a
    # Gold Club block, an early stop, a schedule mismatch, an outright failure,
    # or a run that never reached its own ending at all.
    needs_attention: bool


@dataclass(frozen=True)
class AccountRollup:
    """Totals across the runs summarised, plus villages that keep coming up."""

    runs: int
    total_created: int
    total_created_unverified: int
    total_problems: int
    verify_failures: int
    gold_club_blocked_runs: int
    stopped_early_runs: int
    failed_runs: int
    incomplete_runs: int
    # (village_id, how many of the summarised runs it showed up in), for a
    # village that appeared in a schedule mismatch in MORE THAN ONE run.
    # One occurrence is noise; a repeat across runs is a pattern worth a look.
    # Sorted by count, most-repeated first.
    repeat_problem_villages: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class RunHistory:
    runs: list[RunSummary]
    rollup: AccountRollup


def _read_events(path: Path) -> list[dict[str, Any]]:
    """Every JSON line in one trace file.

    A torn or otherwise undecodable line -- the signature of a process that was
    killed mid-write -- is skipped rather than raised. This is the most likely
    real-world input for a live run: it must parse cleanly and just report the
    run as incomplete.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _summarise_one(path: Path) -> RunSummary:
    events = _read_events(path)
    run_id = path.stem.removeprefix("exec-")
    started_at = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)

    run_start = next((e for e in events if e.get("kind") == "run_start"), None)
    run_end = next((e for e in events if e.get("kind") == "run_end"), None)
    run_failed = next((e for e in events if e.get("kind") == "run_failed"), None)

    verify_failures = sum(1 for e in events if e.get("kind") == "verify_failed")
    schedule_mismatch_origins = tuple(
        sorted(
            {
                e["origin"]
                for e in events
                if e.get("kind") == "schedule_mismatch" and isinstance(e.get("origin"), int)
            }
        )
    )

    def totals(key: str) -> Any:
        return run_end.get(key) if run_end else None

    complete = run_end is not None
    needs_attention = bool(
        (totals("created_unverified") or 0)
        or (totals("not_created") or 0)
        or (totals("problems") or 0)
        or totals("gold_club_blocked")
        or totals("stopped_early")
        or verify_failures
        or schedule_mismatch_origins
        or run_failed is not None
        or not complete
    )

    return RunSummary(
        run_id=run_id,
        started_at=started_at,
        live_enabled=bool(run_start.get("live_enabled")) if run_start else None,
        complete=complete,
        failed=run_failed is not None,
        error=run_failed.get("error") if run_failed else None,
        event_cap_truncated=bool(totals("truncated")),
        elapsed_s=totals("elapsed_s"),
        created=totals("created"),
        created_unverified=totals("created_unverified"),
        not_created=totals("not_created"),
        created_game_rows=totals("created_game_rows"),
        disabled=totals("disabled"),
        re_enabled=totals("re_enabled"),
        cargo_updated=totals("cargo_updated"),
        deferred=totals("deferred"),
        outstanding=totals("outstanding"),
        problems=totals("problems"),
        stopped_early=totals("stopped_early"),
        gold_club_blocked=totals("gold_club_blocked"),
        verify_failures=verify_failures,
        schedule_mismatch_origins=schedule_mismatch_origins,
        needs_attention=needs_attention,
    )


def _rollup(runs: list[RunSummary]) -> AccountRollup:
    village_hits: Counter[int] = Counter()
    for run in runs:
        for village in set(run.schedule_mismatch_origins):
            village_hits[village] += 1
    repeat = tuple(
        sorted(
            ((village, count) for village, count in village_hits.items() if count > 1),
            key=lambda pair: (-pair[1], pair[0]),
        )
    )

    return AccountRollup(
        runs=len(runs),
        total_created=sum(r.created or 0 for r in runs),
        total_created_unverified=sum(r.created_unverified or 0 for r in runs),
        total_problems=sum(r.problems or 0 for r in runs),
        verify_failures=sum(r.verify_failures for r in runs),
        gold_club_blocked_runs=sum(1 for r in runs if r.gold_club_blocked),
        stopped_early_runs=sum(1 for r in runs if r.stopped_early),
        failed_runs=sum(1 for r in runs if r.failed),
        incomplete_runs=sum(1 for r in runs if not r.complete),
        repeat_problem_villages=repeat,
    )


def summarise_runs(trace_dir: Path, limit: int = 20) -> RunHistory:
    """The `limit` most recent live-execution traces in `trace_dir`, summarised.

    Zero I/O beyond reading files already in `trace_dir`; no game requests. A
    missing or empty directory is a normal state ("no runs yet"), not an error.
    """
    if not trace_dir.is_dir():
        return RunHistory(runs=[], rollup=_rollup([]))

    paths = sorted(trace_dir.glob(_TRACE_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    runs = [_summarise_one(path) for path in paths[:limit]]
    return RunHistory(runs=runs, rollup=_rollup(runs))
