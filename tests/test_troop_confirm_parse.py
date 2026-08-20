"""Troop-confirm parser: extract the real fields, ignore decoy-shaped ones.

The coordinate extraction used a substring regex (`[xy]`) that scanned every
input whose name merely contained x or y — the loose kind of selector a planted
honeypot field exploits. These pin that exact-name matching survives and that a
decoy named to contain x/y is not mistaken for a coordinate.
"""

from travian_api.parsers.html_parser import parse_troop_confirm_page

CONFIRM_HTML = """
<html><body>
<form id="troopSendForm">
  <input type="hidden" name="action" value="a1b2c3">
  <input type="hidden" name="eventType" value="4">
  <input name="x" value="12">
  <input name="y" value="-34">
  <input name="villagename" value="Capital">
  <!-- decoy fields a scraper-detector might plant -->
  <input name="xy_trap" value="gotcha">
  <input name="system" value="honey">
  <button onclick="...checksum=deadbe...">confirm</button>
</form>
</body></html>
"""


def test_real_coordinates_are_extracted():
    fields = parse_troop_confirm_page(CONFIRM_HTML)
    assert fields["x"] == "12"
    assert fields["y"] == "-34"
    assert fields["villagename"] == "Capital"


def test_decoy_fields_are_not_mistaken_for_coordinates():
    fields = parse_troop_confirm_page(CONFIRM_HTML)
    # The trap fields must never land under x/y…
    assert fields["x"] == "12"
    assert fields["y"] == "-34"
    # …and are not picked up by the coordinate scan at all.
    assert "xy_trap" not in fields
    assert "system" not in fields


def test_hidden_form_fields_are_echoed():
    fields = parse_troop_confirm_page(CONFIRM_HTML)
    assert fields["action"] == "a1b2c3"
    assert fields["eventType"] == "4"
