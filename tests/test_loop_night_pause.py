"""The shared recurring-loop night pause used by farm + oasis.

It must be a graceful pause (returns to the caller), never raise, and skip
instantly when it isn't the rest window — so a loop resumes in the morning
rather than dying.
"""

import asyncio
from types import SimpleNamespace

from travian_api.web.ws._loop_stealth import night_rest_pause


class _Ctx:
    def __init__(self, rest_seconds, stop_after=None):
        self.pushed = []
        self._rest = rest_seconds
        self._stop_after = stop_after
        self._waits = 0
        self.session = SimpleNamespace(
            http_client=SimpleNamespace(rest_pause_seconds=lambda: self._rest)
        )

    def push(self, msg):
        self.pushed.append(msg)

    async def wait_or_stop(self, _seconds):
        self._waits += 1
        return self._stop_after is not None and self._waits >= self._stop_after

    def should_stop(self):
        return False


def test_no_pause_outside_the_window():
    ctx = _Ctx(rest_seconds=0)
    assert asyncio.run(night_rest_pause(ctx)) is False
    assert ctx.pushed == [], "no message when there is nothing to wait for"


def test_pauses_and_resumes_when_in_window():
    # 30s rest at 15s chunks = two waits, then resume (returns False).
    ctx = _Ctx(rest_seconds=30)
    assert asyncio.run(night_rest_pause(ctx)) is False
    assert any("Night rest" in m.get("message", "") for m in ctx.pushed)


def test_stop_during_pause_breaks_the_loop():
    ctx = _Ctx(rest_seconds=3600, stop_after=1)
    assert asyncio.run(night_rest_pause(ctx)) is True
