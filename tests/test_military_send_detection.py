"""Troop-send success detection must not be fooled by unrelated movements.

An account with raids already in flight always renders a `troopMovement`
element on the rally-point page. The old logic let that force success=True even
when the confirmation form reappeared (send not processed) or an error div was
present -- reporting a rejected raid/scout as dispatched.
"""

from travian_api.services.military_service import MilitaryService

TOKEN = "abc123"


def test_reappearing_form_is_a_failure_even_with_a_movement_on_the_page():
    html = f'<input name="action" value="{TOKEN}"><div class="troopMovement">outgoing</div>'
    assert MilitaryService._send_succeeded(html, TOKEN) is False


def test_an_error_div_is_a_failure_even_with_a_movement_on_the_page():
    html = '<div class="error">Not enough troops</div><div class="troopMovement">outgoing</div>'
    assert MilitaryService._send_succeeded(html, TOKEN) is False


def test_a_clean_rally_page_is_a_success():
    html = '<div class="troopMovement">outgoing</div><button>confirmSendTroops</button>'
    assert MilitaryService._send_succeeded(html, TOKEN) is True


def test_a_clean_page_with_no_movement_is_still_a_success():
    # The action token was consumed and there is no error: the send went out.
    html = "<div>Rally point</div>"
    assert MilitaryService._send_succeeded(html, TOKEN) is True
