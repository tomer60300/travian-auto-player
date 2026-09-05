"""The auto-scout WS must decide "did this send dispatch?" the same way the
military service does.

`scout_ws._send_scout_fast` carried its own, older copy of the success rule,
which let a `troopMovement` element on the result page force success=True. An
account with any raid already in flight always renders one, so a refused scout
(error div present, or the confirmation form reappearing with the same action
token) was reported as dispatched — and the sweep's exhaustion guard, which
keys off `not result["success"]`, could never fire.
"""

import asyncio

import pytest

from travian_api.services.military_service import MilitaryService
from travian_api.web.ws import scout_ws

TOKEN = "abc123"

CONFIRM_HTML = (
    '<form id="troopSendForm">'
    f'<input type="hidden" name="action" value="{TOKEN}">'
    '<button onclick="checksum=a1b2c3">confirm</button>'
    "</form>"
)


class _Http:
    """Replays a fixed confirm page, then the caller's chosen result page."""

    def __init__(self, result_html: str):
        self._result_html = result_html
        self.posts: list[tuple[str, dict]] = []
        self.human_delay = _Delay()
        self.navigator = _Navigator()

    async def post_form(self, url, data=None, **kwargs):
        self.posts.append((url, dict(data or {})))
        if len(self.posts) == 1:
            return CONFIRM_HTML
        return self._result_html


class _Delay:
    async def wait(self, action_type, reason=""):
        return None


class _Navigator:
    async def navigate_to_rally_point(self, village_id=None):
        return None


class _Session:
    def __init__(self, http):
        self.http_client = http
        self.tribe_id = 1


def _run(result_html: str) -> dict:
    http = _Http(result_html)
    return asyncio.run(
        scout_ws._send_scout_fast(
            _Session(http),
            x=10,
            y=20,
            amount=1,
            scout_type="resources",
            village_id=0,
            is_first=True,
        )
    )


@pytest.mark.parametrize(
    "result_html",
    [
        # An error div, with an unrelated raid already in flight.
        '<div class="error">Not enough troops</div><div class="troopMovement">out</div>',
        # The confirmation form came back unprocessed, same movement present.
        f'<input name="action" value="{TOKEN}"><div class="troopMovement">out</div>',
    ],
)
def test_a_refused_send_is_a_failure_even_with_a_movement_on_the_page(result_html):
    result = _run(result_html)
    assert result["success"] is False
    assert MilitaryService._send_succeeded(result_html, TOKEN) is False


def test_a_clean_rally_page_is_still_a_success():
    result = _run('<div class="troopMovement">out</div><button>confirmSendTroops</button>')
    assert result["success"] is True
