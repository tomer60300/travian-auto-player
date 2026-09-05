"""Tests for the run-history audit report built from live-execution traces.

Traces are written by ``ExecutionTrace`` (see
``src/travian_api/services/distribution/execution_trace.py``); these tests
build fixtures by hand instead of running a live execution, so each one pins
exactly the event shapes ``run_history.summarise_runs`` has to cope with --
including the ones a real run may never produce cleanly, like a torn final
line from a killed process.

Every fixture writes JSONL into ``tmp_path``. Never read the operator's real
``~/.travian/traces`` here.
"""

import json
import os
from pathlib import Path

from travian_api.services.distribution.run_history import summarise_runs


def _write_trace(
    directory: Path, run_id: str, events: list[dict], mtime: float | None = None
) -> Path:
    path = directory / f"exec-{run_id}.jsonl"
    lines = [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _run_start(**overrides) -> dict:
    event = {
        "kind": "run_start",
        "user": 1,
        "dry_run": False,
        "live_enabled": True,
        "reconciler_verified": True,
        "disable_existing": True,
        "max_routes_per_run": 3,
        "map_span": 401,
        "origins": 2,
        "desired_routes": 2,
    }
    event.update(overrides)
    return event


def _run_end(**overrides) -> dict:
    event = {
        "kind": "run_end",
        "elapsed_s": 12.3,
        "event_counts": {"run_start": 1, "run_end": 1},
        "truncated": False,
        "created": 0,
        "created_unverified": 0,
        "not_created": 0,
        "created_game_rows": 0,
        "live_game_rows": 0,
        "disabled": 0,
        "re_enabled": 0,
        "cargo_updated": 0,
        "deferred": 0,
        "outstanding": 0,
        "problems": 0,
        "stopped_early": False,
        "gold_club_blocked": False,
    }
    event.update(overrides)
    return event


class TestNormalCompletedRun:
    def test_reports_the_recorded_totals_and_needs_no_attention(self, tmp_path):
        _write_trace(
            tmp_path,
            "aaaaaaaaaaaa",
            [
                _run_start(),
                {"kind": "origin_read", "origin": 11, "existing": 0},
                {
                    "kind": "decision",
                    "origin": 11,
                    "destination": 22,
                    "decision": "created",
                    "reason": "wanted, missing",
                },
                {"kind": "verified", "origin": 11, "claimed": 1, "new_rows_found": 1},
                _run_end(created=1, created_game_rows=1),
            ],
        )

        history = summarise_runs(tmp_path)

        assert len(history.runs) == 1
        run = history.runs[0]
        assert run.run_id == "aaaaaaaaaaaa"
        assert run.complete is True
        assert run.failed is False
        assert run.live_enabled is True
        assert run.created == 1
        assert run.created_game_rows == 1
        assert run.problems == 0
        assert run.needs_attention is False

    def test_the_rows_the_run_left_live_are_reported_too(self, tmp_path):
        # `created_game_rows` counts the fan-out the creates made; the window
        # trim then removes most of it. What the account is LEFT holding is the
        # number the row budget is charged in and the one an operator has to
        # reconcile against -- `trace.close` has recorded it since 456bf02 and
        # the summary dropped it on the floor, so the history table could only
        # show the transient figure.
        _write_trace(
            tmp_path,
            "cccccccccccc",
            [
                _run_start(),
                _run_end(created=1, created_game_rows=42, live_game_rows=16),
            ],
        )

        run = summarise_runs(tmp_path).runs[0]

        assert run.created_game_rows == 42
        assert run.live_game_rows == 16


class TestUnverifiedCreates:
    def test_flags_a_run_whose_creates_could_not_be_confirmed(self, tmp_path):
        _write_trace(
            tmp_path,
            "bbbbbbbbbbbb",
            [
                _run_start(),
                {"kind": "origin_read", "origin": 11, "existing": 0},
                {
                    "kind": "decision",
                    "origin": 11,
                    "destination": 22,
                    "decision": "created",
                    "reason": "wanted, missing",
                },
                {"kind": "verify_failed", "origin": 11, "error": "timeout", "unverified": 1},
                _run_end(created=0, created_unverified=1, problems=1),
            ],
        )

        history = summarise_runs(tmp_path)
        run = history.runs[0]

        assert run.created_unverified == 1
        assert run.verify_failures == 1
        assert run.needs_attention is True

    def test_a_single_refused_create_is_not_reported_as_a_clean_run(self, tmp_path):
        # One refusal is below _CONSECUTIVE_FAILURE_LIMIT, so it produces no
        # `problems` entry at all -- it is carried only by `outstanding`, which
        # `needs_attention` did not read. An otherwise clean account therefore
        # showed created=2, problems=0, needs_attention=False over a route the
        # game refused to make.
        _write_trace(
            tmp_path,
            "dddddddddddd",
            [_run_start(), _run_end(created=2, outstanding=1)],
        )

        run = summarise_runs(tmp_path).runs[0]

        assert run.outstanding == 1
        assert run.problems == 0
        assert run.needs_attention is True


class TestTruncatedTrace:
    def test_a_torn_final_line_parses_without_raising_and_is_reported_incomplete(self, tmp_path):
        path = tmp_path / "exec-cccccccccccc.jsonl"
        lines = [
            json.dumps(_run_start()),
            json.dumps({"kind": "origin_read", "origin": 11, "existing": 0}),
            json.dumps(
                {
                    "kind": "decision",
                    "origin": 11,
                    "destination": 22,
                    "decision": "created",
                    "reason": "wanted, missing",
                }
            ),
            # A killed process: the write was cut off mid-record.
            '{"kind": "wrote", "action": "create", "origin": 11, "stat',
        ]
        path.write_text("\n".join(lines), encoding="utf-8")

        history = summarise_runs(tmp_path)

        assert len(history.runs) == 1
        run = history.runs[0]
        assert run.complete is False
        assert run.needs_attention is True
        # No run_end was ever parsed, so nothing here is invented.
        assert run.created is None
        assert run.problems is None


class TestEmptyOrMissingDirectory:
    def test_a_missing_directory_returns_an_empty_history(self, tmp_path):
        history = summarise_runs(tmp_path / "does-not-exist")

        assert history.runs == []
        assert history.rollup.runs == 0
        assert history.rollup.total_created == 0
        assert history.rollup.repeat_problem_villages == ()

    def test_an_empty_directory_returns_an_empty_history(self, tmp_path):
        history = summarise_runs(tmp_path)

        assert history.runs == []
        assert history.rollup.runs == 0


class TestRunFailedTrace:
    def test_a_failed_run_is_reported_as_failed_with_its_error(self, tmp_path):
        _write_trace(
            tmp_path,
            "dddddddddddd",
            [
                _run_start(),
                {"kind": "origin_read", "origin": 11, "existing": 0},
                {
                    "kind": "run_failed",
                    "error": "boom",
                    "error_type": "RuntimeError",
                    "created_before_failure": 1,
                },
                # The route's `finally` always calls close() again, which is a
                # no-op for the fields already written but still terminates the
                # trace -- see ExecutionTrace.close's idempotency.
                {
                    "kind": "run_end",
                    "elapsed_s": 3.0,
                    "event_counts": {"run_start": 1, "run_failed": 1, "run_end": 1},
                    "truncated": False,
                    "ended": "raised before the run could summarise itself",
                },
            ],
        )

        history = summarise_runs(tmp_path)
        run = history.runs[0]

        assert run.failed is True
        assert run.error == "boom"
        assert run.needs_attention is True
        # The fallback close() never recorded totals -- nothing here is guessed.
        assert run.created is None


class TestOrdering:
    def test_runs_come_back_newest_first(self, tmp_path):
        _write_trace(tmp_path, "oldest000000", [_run_start(), _run_end()], mtime=1_000_000)
        _write_trace(tmp_path, "middle000000", [_run_start(), _run_end()], mtime=1_000_100)
        _write_trace(tmp_path, "newest000000", [_run_start(), _run_end()], mtime=1_000_200)

        history = summarise_runs(tmp_path)

        assert [r.run_id for r in history.runs] == [
            "newest000000",
            "middle000000",
            "oldest000000",
        ]

    def test_limit_keeps_only_the_most_recent(self, tmp_path):
        _write_trace(tmp_path, "oldest000000", [_run_start(), _run_end()], mtime=1_000_000)
        _write_trace(tmp_path, "newest000000", [_run_start(), _run_end()], mtime=1_000_200)

        history = summarise_runs(tmp_path, limit=1)

        assert [r.run_id for r in history.runs] == ["newest000000"]


class TestRollupArithmetic:
    def test_totals_sum_across_runs_and_repeat_villages_surface(self, tmp_path):
        _write_trace(
            tmp_path,
            "run1aaaaaaaa",
            [
                _run_start(),
                {"kind": "schedule_mismatch", "origin": 555, "destinations": {"22": "off-cycle"}},
                _run_end(created=2, problems=1),
            ],
            mtime=1_000_000,
        )
        _write_trace(
            tmp_path,
            "run2aaaaaaaa",
            [
                _run_start(),
                {"kind": "schedule_mismatch", "origin": 555, "destinations": {"22": "off-cycle"}},
                _run_end(created=3, problems=2),
            ],
            mtime=1_000_100,
        )
        _write_trace(
            tmp_path,
            "run3aaaaaaaa",
            [_run_start(), _run_end(created=1)],
            mtime=1_000_200,
        )

        history = summarise_runs(tmp_path)
        rollup = history.rollup

        assert rollup.runs == 3
        assert rollup.total_created == 2 + 3 + 1
        assert rollup.total_problems == 1 + 2 + 0
        assert rollup.repeat_problem_villages == ((555, 2),)
