"""`MilitaryService._send_troops` is the one write in this app that spends the
operator's actual troops. Two POSTs make the dispatch: the troop-selection
form (Step 1, `rally_url` with `troop[t1..t10]`/x/y/eventType) and the
confirmation POST (Step 2, echoed hidden fields + checksum) that is the write
that actually launches the army. Both are `safe_to_retry=False`.

This file pins, per call site: the request the caller asked for, a refusal
named in the page, an unreadable answer, a lost (NetworkError) answer, the
troop-exhaustion guard, and that the transport bills exactly once per
`post_form` call, success or exception.

**Finding**: Step 2's `_send_succeeded(result_html, action_token)` returns
``not form_reappeared and not has_error`` -- with nothing else to go on, an
EMPTY confirmation answer (`form_reappeared=False`, `has_error=False`) read as
a dispatched raid. The write is non-idempotent and already went out, so the
honest verdict for "the game answered with nothing" is the same one a lost
NetworkError answer gets -- unverified, never a silent success. Fixed in
`military_service.py` by checking for an empty `result_html` before consulting
`_send_succeeded`.
"""

import asyncio

from travian_api.exceptions import NetworkError
from travian_api.services.military_service import MilitaryService

from .activity_billing import billing

TOKEN = "tok123abc"

CONFIRM_HTML = (
    '<form id="troopSendForm">'
    f'<input type="hidden" name="action" value="{TOKEN}">'
    '<input type="hidden" name="x" value="10">'
    '<button onclick="checksum=abc123">confirm</button>'
    "</form>"
)


class _Http:
    """Replays one scripted answer per successive `post_form` call.

    An answer that is an ``Exception`` instance is raised instead of returned,
    modelling a transport failure (e.g. the transport's own
    ``NetworkError("... non-retryable ...")`` for a lost write). Bills like
    the real transport via the shared `billing` helper -- never by replacing
    the wrapped method, so a defect that skips billing cannot go unnoticed.
    """

    def __init__(self, answers):
        self._answers = list(answers)
        self.posts: list[tuple[str, dict]] = []
        self.bills: list[float] = []
        self.human_delay = _Delay()
        self.navigator = _Navigator()
        self.post_form = billing(self.bills)(self._post_form)

    async def _post_form(self, url, data=None, **kwargs):
        self.posts.append((url, dict(data or {})))
        answer = self._answers[len(self.posts) - 1]
        if isinstance(answer, Exception):
            raise answer
        return answer


class _Delay:
    async def wait(self, action_type, reason=""):
        return None


class _Navigator:
    async def navigate_to_rally_point(self, village_id=None):
        return None


def _service(answers):
    http = _Http(answers)
    return MilitaryService(http, target_resolver=None), http


# ── Call site 1: Step 1, the troop-selection POST ──────────────────────────


def test_the_troop_selection_form_carries_the_troops_x_y_and_event_type():
    """The caller's raid request becomes the exact form body Step 1 sends."""
    result_html = '<div class="troopMovement">out</div><button>confirmSendTroops</button>'
    svc, http = _service([CONFIRM_HTML, result_html])

    result = asyncio.run(svc.send_raid(x=10, y=20, troops={"t1": 5, "t3": 2}))

    url, body = http.posts[0]
    assert url == "/build.php?gid=16&tt=2"
    assert body["troop[t1]"] == "5"
    assert body["troop[t3]"] == "2"
    assert body["troop[t2]"] == "0", "every unused slot is sent explicitly as 0, not omitted"
    assert body["x"] == "10"
    assert body["y"] == "20"
    assert body["eventType"] == "4", "send_raid dispatches as a raid event"
    assert body["ok"] == "ok"
    assert result.success is True


def test_an_attack_sends_event_type_three():
    svc, http = _service([CONFIRM_HTML, "<button>confirmSendTroops</button>"])
    asyncio.run(svc.send_attack(x=1, y=2, troops={"t1": 1}))
    _, body = http.posts[0]
    assert body["eventType"] == "3"


def test_a_step_one_error_div_is_a_refusal_not_a_success():
    """The game named a refusal on the troop-selection page; Step 2 must not run."""
    html = '<div class="error">Not enough troops</div>'
    svc, http = _service([html])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False
    assert "Not enough troops" in result.raw_response
    assert len(http.posts) == 1, "a Step 1 refusal must not fall through to Step 2"


def test_a_troop_exhaustion_page_refuses_the_send_without_a_second_post():
    """The service's own troop-exhaustion guard: no troops selected, no dispatch."""
    html = "<div>No troops have been selected for this attack.</div>"
    svc, http = _service([html])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 0}))

    assert result.success is False
    assert "no troops" in result.raw_response.lower()
    assert len(http.posts) == 1


def test_an_unrecognized_step_one_page_is_a_failure_not_a_silent_success():
    """No confirm form and no known error text -- must not be read as dispatched."""
    html = "<html><body>We are performing scheduled maintenance.</body></html>"
    svc, http = _service([html])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False
    assert "No confirmation form" in result.raw_response
    assert len(http.posts) == 1


def test_an_empty_step_one_answer_is_a_failure_not_a_silent_success():
    """A genuinely empty 200 after Step 1 is refused, not read as dispatched."""
    svc, http = _service([""])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False
    assert len(http.posts) == 1


def test_a_lost_step_one_answer_is_reported_as_ambiguous_not_retried():
    """A non-retryable NetworkError on Step 1 must not be re-sent, and must say why."""
    svc, http = _service([NetworkError("Connection reset (non-retryable): peer closed")])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False
    assert "non-retryable" in result.raw_response
    assert len(http.posts) == 1, "a lost Step 1 answer must not be sent a second time"
    assert len(http.bills) == 1, "the failed request must still be billed"


# ── Call site 2: Step 2, the confirmation POST that dispatches the troops ──


def test_the_confirmation_post_carries_the_parsed_checksum_and_hidden_fields():
    svc, http = _service([CONFIRM_HTML, "<button>confirmSendTroops</button>"])

    asyncio.run(svc.send_raid(x=10, y=20, troops={"t1": 5}))

    _, final_body = http.posts[1]
    assert final_body["checksum"] == "abc123"
    assert final_body["action"] == TOKEN
    assert final_body["x"] == "10"


def test_a_scout_send_adds_the_scout_target_field_to_the_confirmation_post():
    svc, http = _service([CONFIRM_HTML, "<button>confirmSendTroops</button>"])

    asyncio.run(svc.send_scouts(x=1, y=2, amount=3, scout_type="defenses"))

    _, final_body = http.posts[1]
    assert final_body["troops[0][scoutTarget]"] == "2"


def test_a_refused_confirmation_is_reported_as_a_failed_send():
    """The confirm form reappeared with the same token: not processed."""
    result_html = f'<input name="action" value="{TOKEN}">'
    svc, http = _service([CONFIRM_HTML, result_html])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False
    assert result.raw_response
    assert len(http.posts) == 2


def test_an_empty_confirmation_answer_is_not_read_as_a_silent_success():
    """FINDING: an empty Step 2 answer used to read as a dispatched raid."""
    svc, http = _service([CONFIRM_HTML, ""])

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False, "an empty answer must not be read as a dispatched raid"
    assert "may already have taken effect" in result.raw_response
    assert len(http.posts) == 2, "the confirm WAS sent; only the read of its answer failed"


def test_a_lost_step_two_answer_is_not_re_sent_and_stays_ambiguous():
    svc, http = _service(
        [CONFIRM_HTML, NetworkError("Connection reset (non-retryable): peer closed")]
    )

    result = asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert result.success is False
    assert "non-retryable" in result.raw_response
    assert len(http.posts) == 2, "the confirm must not be sent a second time"
    assert len(http.bills) == 2, "both the confirm and its exception must be billed"


def test_a_dispatched_raid_bills_once_per_post_form_call():
    svc, http = _service([CONFIRM_HTML, "<button>confirmSendTroops</button>"])

    asyncio.run(svc.send_raid(x=1, y=2, troops={"t1": 5}))

    assert len(http.posts) == 2
    assert len(http.bills) == 2
