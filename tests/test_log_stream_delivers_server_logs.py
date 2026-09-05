"""The /ws/logs panel showed almost nothing, and two docstrings promised it would.

``_format_record`` built its entry dict from a ``LogRecord`` and never set a
``user_id`` key. ``push`` skips a *scoped* subscriber whenever the entry has no
``user_id``, and ``/ws/logs`` always subscribes scoped (``logs_ws.py``, with the
id off the JWT). So every record that reached the Python logging handler was
dropped for every subscriber, and only the handful of hand-tagged pushes
(``sessions.py``, ``captcha.py``, ``queue_ws.py``, ``farm_builder.py``,
``oasis_raider.py``, ``analyzer_ws.py``) ever arrived. ``get_history`` had the
same effect on the catch-up.

Two docstrings stated the opposite -- ``subscribe``: "plus system logs that have
no user_id"; ``get_history``: "plus system entries that have no user_id" --
while ``push`` described the rule its own code implements. Three statements, two
of them false, and the operator with no in-app view of the server.

One rule, chosen and applied everywhere: a record carries the ``user_id`` its
call site attached (``logger.info(..., extra={"user_id": n})``), and an entry
with no ``user_id`` is server-wide and goes to everyone. That is the reading two
of the three docstrings already had, and the only one under which the feature
does anything. The cost is stated where the rule is: a record that concerns one
user must say so through ``extra``, or every viewer sees it.
"""

import asyncio
import logging

import pytest

from travian_api.web.log_broadcast import (
    LogBroadcastHandler,
    LogStreamManager,
    _format_record,
)


def _record(msg="hello", **extra):
    record = logging.LogRecord(
        "travian_api.services.farm_list_service", logging.INFO, __file__, 1, msg, None, None
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def _drain(queue):
    entries = []
    while not queue.empty():
        entries.append(queue.get_nowait())
    return entries


@pytest.fixture
def manager():
    # asyncio.Queue binds to the running loop, so subscribe inside one.
    return LogStreamManager()


def test_a_record_carries_the_user_id_its_call_site_attached():
    assert _format_record(_record(user_id=7))["user_id"] == 7


def test_a_record_with_no_user_id_says_so_rather_than_omitting_the_key():
    # The key has to exist for `push` to reason about it; its absence is what
    # made "untagged" and "malformed" the same thing.
    assert _format_record(_record())["user_id"] is None


def test_a_server_wide_record_reaches_a_scoped_subscriber(manager):
    async def run():
        queue = manager.subscribe(1, user_id=7)
        LogBroadcastHandler(manager).emit(_record("the farm list was sent"))
        return _drain(queue)

    entries = asyncio.run(run())
    assert [e["message"] for e in entries] == ["the farm list was sent"]


def test_another_users_tagged_record_does_not_reach_a_scoped_subscriber(manager):
    async def run():
        queue = manager.subscribe(1, user_id=7)
        LogBroadcastHandler(manager).emit(_record("user 9 business", user_id=9))
        LogBroadcastHandler(manager).emit(_record("user 7 business", user_id=7))
        return _drain(queue)

    entries = asyncio.run(run())
    assert [e["message"] for e in entries] == ["user 7 business"]


def test_an_unscoped_subscriber_still_sees_everything(manager):
    async def run():
        queue = manager.subscribe(1, user_id=None)
        LogBroadcastHandler(manager).emit(_record("system"))
        LogBroadcastHandler(manager).emit(_record("theirs", user_id=9))
        return _drain(queue)

    entries = asyncio.run(run())
    assert [e["message"] for e in entries] == ["system", "theirs"]


def test_the_catch_up_history_follows_the_same_rule(manager):
    handler = LogBroadcastHandler(manager)
    handler.emit(_record("system"))
    handler.emit(_record("theirs", user_id=9))
    handler.emit(_record("mine", user_id=7))

    assert [e["message"] for e in manager.get_history(100, user_id=7)] == ["system", "mine"]
    assert [e["message"] for e in manager.get_history(100)] == ["system", "theirs", "mine"]


def test_the_docstrings_state_the_rule_they_implement():
    """All three said something; two of them were wrong."""
    for doc in (
        LogStreamManager.push.__doc__,
        LogStreamManager.subscribe.__doc__,
        LogStreamManager.get_history.__doc__,
    ):
        assert doc is not None
        assert "server-wide" in doc, doc
