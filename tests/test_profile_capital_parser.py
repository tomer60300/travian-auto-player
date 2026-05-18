"""Regression tests for the profile-page capital-id parser.

Travian's profile page evolved its capital marker over time:
    * Legacy: ``"isMainVillage": true`` / ``"isCapital": true`` boolean
      keys inside each village JSON object.
    * Modern (post-2025): ``"typeText": "(Capital)"`` — a localized
      string on the capital row, empty everywhere else.

The shipped parser must cover both, otherwise the AutoScout ★ column
silently shows nothing for users on the current Travian release. The
modern fixture below is a verbatim slice of what production HTML
returned for player 4893 (a Slovak server, English locale) — keeping it
here keeps us honest if Travian shuffles the markup again.
"""

from __future__ import annotations

from travian_api.services.auto_scout_service import (
    _parse_capital_id_from_profile_html,
)


PRODUCTION_PROFILE_SLICE = (
    '"villages":['
    '{"id":44077,"name":"Slovenská Ves","tribeId":3,"mapId":43149,'
    '"population":736,"victoryPoints":null,"victoryPointsPerDay":null,'
    '"x":41,"y":93,"occupiedOases":[],"region":null,'
    '"typeText":"(Capital)","typeTitle":""},'
    '{"id":64861,"name":"Výborna","tribeId":3,"mapId":44352,'
    '"population":337,"victoryPoints":null,"victoryPointsPerDay":null,'
    '"x":41,"y":90,"occupiedOases":[],"region":null,'
    '"typeText":"","typeTitle":""}'
    ']'
)


def test_modern_typetext_capital_english() -> None:
    """The user's real Slovakian-server profile (English locale)."""
    assert _parse_capital_id_from_profile_html(PRODUCTION_PROFILE_SLICE) == 44077


def test_modern_typetext_capital_german() -> None:
    html = (
        '"villages":['
        '{"id":111,"name":"Berlin","typeText":"(Hauptdorf)","typeTitle":""},'
        '{"id":222,"name":"Bonn","typeText":"","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) == 111


def test_modern_typetext_capital_polish() -> None:
    html = (
        '"villages":[{"id":7777,"typeText":"(Stolica)","typeTitle":""}]'
    )
    assert _parse_capital_id_from_profile_html(html) == 7777


def test_modern_typetext_capital_russian() -> None:
    html = (
        '"villages":[{"id":4242,"typeText":"(Столица)","typeTitle":""}]'
    )
    assert _parse_capital_id_from_profile_html(html) == 4242


def test_modern_typetext_ignores_non_capital_marker() -> None:
    """Other special types (e.g. WW) must not be mistaken for the capital."""
    html = (
        '"villages":['
        '{"id":1,"typeText":"(WW)","typeTitle":""},'
        '{"id":2,"typeText":"","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) is None


def test_legacy_isMainVillage_still_works() -> None:
    """Older Travian servers might still emit the boolean marker."""
    html = (
        '"villages":['
        '{"id":555,"name":"Alpha","isMainVillage":true},'
        '{"id":666,"name":"Beta","isMainVillage":false}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) == 555


def test_legacy_isCapital_still_works() -> None:
    html = '"villages":[{"id":999,"isCapital":true}]'
    assert _parse_capital_id_from_profile_html(html) == 999


def test_html_fallback_link_before_marker() -> None:
    """Last-resort path: newdid link followed by the word 'capital'."""
    html = (
        '<table>'
        '<tr><td><a href="?newdid=123">Alpha</a></td>'
        '<td class="capital">Capital village</td></tr>'
        '</table>'
    )
    assert _parse_capital_id_from_profile_html(html) == 123


def test_no_capital_marker_returns_none() -> None:
    html = '"villages":[{"id":1,"typeText":"","typeTitle":""}]'
    assert _parse_capital_id_from_profile_html(html) is None


def test_empty_html_returns_none() -> None:
    assert _parse_capital_id_from_profile_html("") is None


def test_missing_villages_array_returns_none() -> None:
    """A profile page without the JSON villages array should fall through
    to the HTML fallback; with no marker text either, returns None."""
    html = '<html><body>Some unrelated content.</body></html>'
    assert _parse_capital_id_from_profile_html(html) is None


def test_capital_id_not_confused_with_tribeId_or_mapId() -> None:
    """`"id"` must match the village's database id, not other *Id keys.

    Travian's JSON puts tribeId/mapId near the village id; the regex
    must lock on the literal key `"id"` and not accidentally swallow
    `"tribeId":3` or `"mapId":43149`. The user's real slice already
    exercises this — repeated here as a focused assertion.
    """
    html = (
        '"villages":[{"id":44077,"name":"Capital","tribeId":3,"mapId":43149,'
        '"typeText":"(Capital)","typeTitle":""}]'
    )
    assert _parse_capital_id_from_profile_html(html) == 44077


def test_modern_typetext_with_populated_occupied_oases() -> None:
    """Village objects can carry nested ``occupiedOases`` entries with
    their own brace pairs. Codex flagged the prior flat
    ``\\{[^{}]{0,800}\\}`` regex as unable to span nested data —
    confirm the new structural extractor handles it."""
    html = (
        '"villages":['
        '{"id":111,"name":"Center","tribeId":1,"mapId":1,'
        '"population":900,"victoryPoints":null,"x":0,"y":0,'
        '"occupiedOases":[{"id":555,"x":1,"y":0},{"id":556,"x":-1,"y":0}],'
        '"region":null,"typeText":"(Capital)","typeTitle":""},'
        '{"id":222,"name":"Outpost","tribeId":1,"mapId":2,'
        '"population":300,"victoryPoints":null,"x":3,"y":3,'
        '"occupiedOases":[],"region":null,"typeText":"","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) == 111


def test_modern_typetext_with_very_long_village_object() -> None:
    """A village JSON object can exceed 800 bytes (occupiedOases arrays
    can be long, names can be long, future fields can be added). The
    previous flat regex bounded the chunk to 800 chars; the new
    structural extractor must handle arbitrary length."""
    long_oasis_list = ",".join(
        f'{{"id":{i},"x":{i},"y":{i}}}' for i in range(40)
    )
    html = (
        '"villages":[{"id":7777,"name":"' + 'X' * 200 + '","tribeId":1,'
        f'"mapId":99,"population":1200,"occupiedOases":[{long_oasis_list}],'
        '"region":null,"typeText":"(Capital)","typeTitle":""}]'
    )
    # Sanity: this fixture really is bigger than the prior 800-char
    # window. If it weren't, the test would silently pass against the
    # old parser too and miss the regression Codex flagged.
    villages_obj_len = len(html) - len('"villages":[') - len(']')
    assert villages_obj_len > 800
    assert _parse_capital_id_from_profile_html(html) == 7777


def test_html_fallback_marker_before_link() -> None:
    """Strategy 3 must work in BOTH orders — capital marker text before
    the newdid link as well as after. Covers locales/skins where the
    village table puts the marker in a sibling cell to the left."""
    html = (
        '<table>'
        '<tr><td class="capital">Capital</td>'
        '<td><a href="?newdid=2025">Roma</a></td></tr>'
        '</table>'
    )
    assert _parse_capital_id_from_profile_html(html) == 2025


def test_unknown_locale_falls_through_without_crashing() -> None:
    """A locale we haven't catalogued yet must not crash the parser —
    just return None so the ★ column simply stays empty for that
    user, instead of raising and failing the whole scan."""
    html = (
        '"villages":['
        '{"id":1,"typeText":"(पूँजी)","typeTitle":""},'   # Hindi placeholder
        '{"id":2,"typeText":"","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) is None


def test_caller_dict_immutability_not_relevant_here() -> None:
    """Parser is pure — confirm it doesn't mutate the html argument
    (we pass strings, which are immutable, but assert behavior is
    referentially transparent so repeated calls on the same input
    yield identical results)."""
    html = (
        '"villages":[{"id":111,"typeText":"(Capital)","typeTitle":""}]'
    )
    first = _parse_capital_id_from_profile_html(html)
    second = _parse_capital_id_from_profile_html(html)
    assert first == second == 111


def test_hlavn_short_prefix_no_longer_false_positive() -> None:
    """Codex flagged the prior `hlavn` substring as too broad — Czech
    has many words starting `hlavn-` (e.g. ``hlavně`` meaning
    "mainly") that aren't capital markers. The keyword list now uses
    explicit ``hlavní`` / ``hlavné`` so a benign typeText doesn't
    spuriously mark a village as capital."""
    html = (
        '"villages":['
        '{"id":1,"typeText":"(hlavně)","typeTitle":""},'
        '{"id":2,"typeText":"","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) is None


def test_czech_capital_marker_still_works() -> None:
    """Tightening the locale regex must not break real Czech."""
    html = (
        '"villages":['
        '{"id":99,"typeText":"(Hlavní město)","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) == 99


def test_slovak_capital_marker_still_works() -> None:
    """Tightening the locale regex must not break real Slovak."""
    html = (
        '"villages":['
        '{"id":99,"typeText":"(Hlavné mesto)","typeTitle":""}'
        ']'
    )
    assert _parse_capital_id_from_profile_html(html) == 99


def test_villages_array_with_string_containing_braces() -> None:
    """Village names occasionally contain literal { or } — the
    structural extractor's string awareness must not confuse them
    with object boundaries."""
    html = (
        '"villages":[{"id":1,"name":"weird}brace{name",'
        '"typeText":"(Capital)","typeTitle":""}]'
    )
    assert _parse_capital_id_from_profile_html(html) == 1
