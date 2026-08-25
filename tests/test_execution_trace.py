"""A live run has to leave enough evidence to audit it after the fact.

The trade-route write path is the only thing in this app that changes a real
account, and before this it carried one ``logger.debug`` line. The response tells
you what happened; it cannot tell you *why*, and a run that disabled the wrong
route looks exactly like one that disabled the right route.

So these tests pin the properties that make a trace worth having: every decision
carries a reason, every write carries the body that was actually sent, the file
survives a crash mid-run, and tracing can never be the thing that breaks a run.
"""

import json
from pathlib import Path

import pytest

from travian_api.services.distribution import execution_trace
from travian_api.services.distribution.execution_trace import ExecutionTrace


def _events(trace: ExecutionTrace) -> list[dict]:
    return [json.loads(line) for line in trace.path.read_text(encoding="utf-8").splitlines()]


class TestTheTraceRecordsWhatHappened:
    def test_every_event_carries_the_run_id_and_a_timestamp(self):
        trace = ExecutionTrace()
        trace.event("run_start", dry_run=False)
        trace.decision(origin=1, destination=2, decision="skipped", reason="already active")
        trace.close(created=0)

        for event in _events(trace):
            assert event["run"] == trace.run_id
            assert isinstance(event["t"], (int, float))
            assert event["kind"]

    def test_a_decision_without_a_reason_is_not_expressible(self):
        # `reason` is keyword-only and required. A decision log that says
        # "skipped" without saying why is the failure mode this exists to avoid.
        trace = ExecutionTrace()
        with pytest.raises(TypeError):
            trace.decision(origin=1, destination=2, decision="skipped")

    def test_a_write_records_the_exact_payload_sent(self):
        # If a route turns up in-game with the wrong cargo, this is the only way
        # to tell a planner bug from a serialisation bug.
        trace = ExecutionTrace()
        payload = {"action": "traderoute", "hour": 15, "minute": 27, "repeatEvery": 6}
        trace.wrote(kind="create", origin=20003, status="created", elapsed_ms=812, payload=payload)
        trace.close()

        wrote = [e for e in _events(trace) if e["kind"] == "wrote"]
        assert len(wrote) == 1
        assert wrote[0]["payload"] == payload, "the body must be recorded verbatim"
        assert wrote[0]["elapsed_ms"] == 812

    def test_a_refusal_is_recorded_as_loudly_as_a_write(self):
        # A run that sent nothing because a gate refused must be distinguishable
        # from a run that sent nothing because there was nothing to do.
        trace = ExecutionTrace()
        trace.refused(kind="create", origin=20003, reason="activity budget exhausted")
        trace.close()

        refused = [e for e in _events(trace) if e["kind"] == "refused"]
        assert refused[0]["reason"] == "activity budget exhausted"

    def test_the_run_end_summarises_and_counts_every_event_kind(self):
        trace = ExecutionTrace()
        trace.event("origin_read", origin=1)
        trace.event("origin_read", origin=2)
        trace.decision(origin=1, destination=2, decision="created", reason="missing in game")
        trace.close(created=1, problems=0)

        end = _events(trace)[-1]
        assert end["kind"] == "run_end"
        assert end["event_counts"]["origin_read"] == 2
        assert end["event_counts"]["decision"] == 1
        assert end["created"] == 1
        assert end["truncated"] is False


class TestTheTraceSurvivesTrouble:
    def test_events_are_flushed_so_a_crashed_run_still_leaves_a_trace(self):
        # No close(), simulating a process that died mid-run. The events written
        # before the crash must still be on disk -- a trace that only lands at
        # close is worthless for the failures worth investigating.
        trace = ExecutionTrace()
        trace.event("run_start")
        trace.wrote(kind="create", origin=1, status="created", elapsed_ms=5)

        assert len(_events(trace)) == 2

    def test_close_is_idempotent_so_a_finally_cannot_double_end_the_run(self):
        trace = ExecutionTrace()
        trace.close(created=3)
        trace.close(ended="exception or early exit")

        ends = [e for e in _events(trace) if e["kind"] == "run_end"]
        assert len(ends) == 1, "two contradictory endings would misreport the run"
        assert ends[0]["created"] == 3, "the first, fuller summary wins"

    def test_an_unwritable_trace_directory_does_not_break_the_run(self, monkeypatch, tmp_path):
        # Observability must never be the reason a game operation fails.
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setattr(execution_trace, "TRACE_DIR", blocker / "traces")

        trace = ExecutionTrace()
        assert trace.enabled is False
        assert trace.path is None
        # All of these must be no-ops rather than exceptions.
        trace.event("run_start")
        trace.decision(origin=1, destination=2, decision="skipped", reason="x")
        trace.wrote(kind="create", origin=1, status="created", elapsed_ms=1)
        trace.close(created=0)
        assert trace.counts["decision"] == 1, "counts still work without a file"

    def test_an_unserialisable_field_loses_one_line_not_the_run(self):
        class Opaque:
            def __repr__(self):
                return "<opaque>"

        trace = ExecutionTrace()
        trace.event("weird", thing=Opaque())  # default=str handles it
        trace.event("normal", ok=True)
        trace.close()

        kinds = [e["kind"] for e in _events(trace)]
        assert "normal" in kinds and "run_end" in kinds

    def test_a_runaway_run_stops_writing_but_keeps_counting(self, monkeypatch):
        monkeypatch.setattr(execution_trace, "MAX_EVENTS", 5)
        trace = ExecutionTrace()
        for i in range(20):
            trace.event("spam", i=i)
        trace.close()

        written = _events(trace)
        assert len(written) <= 6, "the cap must actually bound the file"
        assert trace.counts["spam"] == 20, "but the true count is still reported"
        assert written[-1]["truncated"] is True, "and the run says it was truncated"


class TestTracesStayOutOfTheRepository:
    def test_the_trace_directory_is_outside_the_checkout(self):
        # These files hold real village ids and coordinates, and this repository
        # is public. The suite redirects TRACE_DIR (see conftest), so assert on
        # the module default rather than the patched value.
        default = Path.home() / ".travian" / "traces"
        assert default.is_absolute()
        assert "travian-auto-player" not in str(default).lower()


class TestAWriteAfterCloseCannotCrashTheCaller:
    """A closed trace must absorb late events, not raise into a game write.

    Found by a live run. The execute endpoint sets `svc.trace = trace` and closes
    the trace when the run ends, but the SERVICE keeps the reference. Any later
    direct call to a write method -- and `POST /routes/revert-plan` with
    `apply_disable=true` makes exactly one -- reached a trace whose file handle
    was already None and raised AttributeError from inside the logging path,
    AFTER the request to the game had already succeeded. The write landed and the
    caller saw a crash.

    Tracing is observability. It is never allowed to be the thing that breaks an
    operation, least of all one that already happened.
    """

    def test_an_event_after_close_does_not_raise(self):
        trace = ExecutionTrace()
        trace.close(created=1)

        # None of these may raise.
        trace.event("late", value=1)
        trace.decision(origin=1, destination=2, decision="skipped", reason="late")
        trace.wrote(kind="disable", origin=1, status="disabled", elapsed_ms=5)
        trace.refused(kind="create", origin=1, reason="late")

    def test_late_events_are_still_counted(self):
        # Silently vanishing would hide the very bug this guards against.
        trace = ExecutionTrace()
        trace.close()
        trace.event("late")
        assert trace.counts["late"] == 1

    def test_the_file_keeps_its_ending_and_gains_nothing_after_it(self):
        trace = ExecutionTrace()
        trace.event("during")
        trace.close(created=0)
        trace.event("after")

        events = _events(trace)
        assert [e["kind"] for e in events] == ["during", "run_end"]
        assert events[-1]["kind"] == "run_end", "the ending must stay the last line"

    def test_a_trace_that_never_opened_a_file_also_absorbs_events(self):
        trace = ExecutionTrace(enabled=False)
        trace.close()
        trace.event("late")
        trace.wrote(kind="create", origin=1, status="created", elapsed_ms=1)
        assert trace.counts["late"] == 1
