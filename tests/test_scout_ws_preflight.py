"""A failed troop read must not become "there is no scout limit".

`scout_ws`'s pre-flight set `available_scouts = -1` in its `except`, and the
whole capping block was then guarded by `if available_scouts >= 0:` — so a
failed `get_available_troops` skipped the cap, skipped the `scout_preflight`
frame entirely, and dispatched to EVERY target in `tiles`. Nothing in the
stream said the pre-flight had been skipped: the UI simply never received the
frame it would have rendered.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.web.ws import scout_ws

VILLAGE_ID = 555
TARGETS = [{"x": 10 + i, "y": 20} for i in range(5)]

CONFIRM_HTML = (
    '<form id="troopSendForm">'
    '<input type="hidden" name="action" value="tok">'
    '<button onclick="checksum=a1b2c3">confirm</button>'
    "</form>"
)
CLEAN_RESULT = "<div>Rally point</div>"


class _Http:
    def __init__(self):
        self.posts: list[dict] = []
        self.human_delay = _Delay()
        self.activity_scheduler = _Scheduler()
        self.navigator = _Navigator()

    async def post_form(self, url, data=None, **kwargs):
        payload = dict(data or {})
        self.posts.append(payload)
        return CONFIRM_HTML if "ok" in payload else CLEAN_RESULT

    def check_activity_budget(self):
        return None

    @property
    def dispatches(self) -> list[tuple[str, str]]:
        """(x, y) of every troop-selection POST — one per attempted target."""
        return [(p["x"], p["y"]) for p in self.posts if "ok" in p]


class _Navigator:
    async def navigate_to_rally_point(self, village_id=None):
        return None


class _Delay:
    async def wait(self, action_type, reason=""):
        return None


class _Scheduler:
    def log_activity(self, seconds):
        return None


class _Military:
    """`get_available_troops` either answers or fails, as chosen."""

    def __init__(self, troops):
        self._troops = troops

    async def get_available_troops(self, village_id=None):
        if isinstance(self._troops, Exception):
            raise self._troops
        return self._troops


class _Ctx:
    def __init__(self, session):
        self.session = session
        self.user_id = 1
        self.frames: list[dict] = []

    def push(self, data):
        self.frames.append(data)

    def should_stop(self):
        return False

    async def wait_or_stop(self, seconds):
        return False

    def kinds(self):
        return [f.get("type") for f in self.frames]

    def frame(self, kind):
        return next((f for f in self.frames if f.get("type") == kind), None)


def _run(troops, *, start_index=0):
    http = _Http()
    session = SimpleNamespace(
        tribe_id=2,
        http_client=http,
        military_service=_Military(troops),
        scout_service=SimpleNamespace(recon_http_client=None),
        auth_state=SimpleNamespace(
            villages=[SimpleNamespace(id=VILLAGE_ID, x=0, y=0)],
            player_name="me",
        ),
        settings=SimpleNamespace(base_url="https://example.invalid", username="me"),
    )
    ctx = _Ctx(session)
    coro = scout_ws._build_auto_scout_coro(
        {
            "radius": 5,
            "village_id": VILLAGE_ID,
            "amount": 1,
            "type": "resources",
            "targets": TARGETS,
            "use_recon": False,
            "recon_strict": False,
            "start_index": start_index,
            "delay_min": 0.0,
            "delay_max": 0.0,
        }
    )
    asyncio.run(coro(ctx))
    return http, ctx


@pytest.fixture(autouse=True)
def _no_real_pauses(monkeypatch):
    monkeypatch.setattr(
        "travian_api.web.ws.scout_ws.HumanTiming.delay",
        staticmethod(lambda *a, **k: 0),
    )


def test_a_readable_preflight_caps_the_sweep():
    http, ctx = _run({"t4": 2})
    assert ctx.frame("scout_preflight") is not None
    assert ctx.frame("scouts_capped") is not None
    assert len(http.dispatches) == 2, "2 scouts idle, 1 per target"


def test_a_failed_preflight_refuses_the_sweep_instead_of_uncapping_it():
    http, ctx = _run(RuntimeError("troop page unreadable"), start_index=3)

    assert http.dispatches == [], "'I could not check' must never mean 'no limit'"
    failed = ctx.frame("preflight_failed")
    assert failed is not None, "the stream must say the pre-flight was skipped"
    assert "troop page unreadable" in failed["message"]
    complete = ctx.frame("complete")
    assert complete["total_sent"] == 0
    assert complete["next_start_index"] == 3, "the round-robin cursor is preserved"


def test_zero_scouts_still_stops_before_any_dispatch():
    http, ctx = _run({"t4": 0})
    assert http.dispatches == []
    assert ctx.frame("scouts_exhausted") is not None
