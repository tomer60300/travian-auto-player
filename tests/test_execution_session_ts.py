"""Regression tests for ExecutionSessionManager.push ts monotonicity.

The client's resume-dedup uses ``ts <= lastSeen`` to drop messages it has
already processed during a session-stream replay. If two distinct pushes
(e.g. ``complete`` followed by ``operation_complete`` inside
``OperationManager._run``) happen to share a ``ts`` — which Windows
``time.time()`` can produce when calls fall inside the same clock tick —
the second push gets dedup-skipped on the client and its terminal
handler never fires. Force the timestamps to be strictly monotonic to
keep the client's dedup correct.
"""

from __future__ import annotations

from unittest.mock import patch

from travian_api.web.execution_sessions import ExecutionSessionManager


def test_push_assigns_strictly_increasing_ts_for_back_to_back_calls() -> None:
    """Two pushes inside the same clock tick must still get distinct ts."""
    mgr = ExecutionSessionManager()
    sess = mgr.create(user_id=1, session_type="scout-scan", label="test")

    # Force time.time() to return the same value for both pushes — this
    # simulates the Windows scenario that bit us in production.
    with patch("travian_api.web.execution_sessions.time.time", return_value=1000.0):
        mgr.push(sess.id, {"type": "complete"})
        mgr.push(sess.id, {"type": "operation_complete", "status": "completed"})

    msgs = list(sess.messages)
    assert len(msgs) == 2
    assert msgs[0]["ts"] == 1000.0
    assert msgs[1]["ts"] > msgs[0]["ts"], (
        "operation_complete must have ts strictly greater than complete; "
        "otherwise the client's `ts <= lastSeen` dedup skips its terminal handler."
    )


def test_push_does_not_overwrite_caller_supplied_ts() -> None:
    """Callers that provide their own ts (e.g. tests, replays) keep it."""
    mgr = ExecutionSessionManager()
    sess = mgr.create(user_id=1, session_type="scout-scan", label="test")
    mgr.push(sess.id, {"type": "phase", "ts": 42.0})
    msgs = list(sess.messages)
    assert msgs[0]["ts"] == 42.0


def test_push_uses_real_clock_when_it_advances() -> None:
    """When time.time() actually moves forward, we use it as-is."""
    mgr = ExecutionSessionManager()
    sess = mgr.create(user_id=1, session_type="scout-scan", label="test")
    with patch(
        "travian_api.web.execution_sessions.time.time",
        side_effect=[100.0, 200.0],
    ):
        mgr.push(sess.id, {"type": "a"})
        mgr.push(sess.id, {"type": "b"})
    msgs = list(sess.messages)
    assert msgs[0]["ts"] == 100.0
    assert msgs[1]["ts"] == 200.0


def test_push_recovers_from_clock_going_backwards() -> None:
    """Even if the wall clock retreats (NTP step, VM resume), ts climbs."""
    mgr = ExecutionSessionManager()
    sess = mgr.create(user_id=1, session_type="scout-scan", label="test")
    with patch(
        "travian_api.web.execution_sessions.time.time",
        side_effect=[500.0, 100.0],
    ):
        mgr.push(sess.id, {"type": "a"})
        mgr.push(sess.id, {"type": "b"})
    msgs = list(sess.messages)
    assert msgs[0]["ts"] == 500.0
    assert msgs[1]["ts"] > msgs[0]["ts"]
