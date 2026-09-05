"""A cranny that hides the stock is not 67% of it, and "no figure" is not zero.

``reconstruct_state`` read the scout's carry icon with ``steal.get("raidable", 0)``
and, whenever that came out ``<= 0``, replaced it with ``round(total * 0.67)`` --
then stamped ``raidable_confidence = "scouted"`` on the result, so a fabricated
number was indistinguishable downstream from a measured one.

Three different situations arrive at the parser as the same zero, and the ratio
was applied to all three:

1. the scout carried no cranny/raidable row at all;
2. the row was there but its icon titles were not the English the parser matches;
3. the game genuinely reported nothing raidable, because the crannies cover the
   whole stock.

Case 3 is the one that costs troops: a fully-crannied village was scored at 67%
of its total stock, every scan, and every wave sent to it came back empty.

The game already answers this question and the parser already reads the answer:
``stealable_resources`` carries ``cranny``, and the mechanic is
``max(0, total - cranny)``. The analyzer simply never looked at it.

So: the parser reports only what it actually read -- a missing icon leaves its
key out rather than writing a zero -- and the analyzer computes from the cranny
when it has one, uses the carry icon when that is what it has, and REFUSES the
target when it has neither, the way ``unsupported_defender_ids`` already refuses
a garrison it cannot score. There is no fallback ratio.
"""

from types import SimpleNamespace

import pytest

from travian_api.models.raid_analyzer import (
    AnalysisResult,
    AnalyzerSettings,
)
from travian_api.parsers.report_parser import parse_scout_report
from travian_api.services.raid_analyzer_service import (
    RaidAnalyzerService,
    calculate_score,
    calculate_score_v2,
    reconstruct_state,
)


def _icon(title: str, value: int) -> str:
    return f'<div class="inlineIcon" title="{title}"><span class="value">{value}</span></div>'


def _scout_html(*, resources: dict[str, int], second_wrapper: str | None) -> str:
    """A scout report page shaped like the game's own.

    *second_wrapper* is the cranny/raidable row: ``None`` leaves it out
    entirely, which is what a report with no such row looks like.
    """
    first = "".join(_icon(name.capitalize(), value) for name, value in resources.items())
    wrappers = f'<div class="resourceWrapper">{first}</div>'
    if second_wrapper is not None:
        wrappers += f'<div class="resourceWrapper">{second_wrapper}</div>'
    return (
        "<html><body>"
        '<table class="additionalInformation"><tr><td>'
        f"{wrappers}"
        "</td></tr></table>"
        "</body></html>"
    )


STOCK = {"lumber": 1000, "clay": 1000, "iron": 1000, "crop": 1000}
TOTAL = 4000


# ── The parser: only report what was actually read ─────────────────────────


def test_a_carry_icon_is_reported_as_a_raidable_figure():
    data = parse_scout_report(_scout_html(resources=STOCK, second_wrapper=_icon("Raidable", 2600)))
    assert data.stealable_resources.get("raidable") == 2600


def test_a_cranny_icon_is_reported_as_a_cranny_figure():
    data = parse_scout_report(_scout_html(resources=STOCK, second_wrapper=_icon("Cranny", 4000)))
    assert data.stealable_resources.get("cranny") == 4000


@pytest.mark.parametrize(
    "second_wrapper",
    [
        None,  # no cranny/raidable row at all
        _icon("Versteck", 4000) + _icon("Beute", 0),  # a non-English page
    ],
    ids=["no-row", "not-english"],
)
def test_an_unread_figure_is_absent_rather_than_zero(second_wrapper):
    """Absent keys, not zeros: a zero here is a claim the parser cannot make."""
    data = parse_scout_report(_scout_html(resources=STOCK, second_wrapper=second_wrapper))
    assert "raidable" not in data.stealable_resources
    assert "cranny" not in data.stealable_resources


# ── The analyzer: three fixtures, three different answers ──────────────────


def _reports(second_wrapper: str | None):
    """One scout report, parsed from HTML, in the shape reconstruct_state takes."""
    data = parse_scout_report(_scout_html(resources=STOCK, second_wrapper=second_wrapper))
    return [{"type": "scout", "data": data, "report_id": "r1", "timestamp": None}]


def test_a_carry_icon_is_the_stealable_amount():
    state = reconstruct_state((12, -34), _reports(_icon("Raidable", 2600)), "me")
    assert state.estimated_raidable == 2600
    assert state.raidable_confidence == "scouted"


def test_a_cranny_that_covers_the_stock_leaves_nothing_to_raid():
    # The whole point: 4,000 of stock behind 4,000 of cranny is 0 raidable,
    # not round(4000 * 0.67) = 2,680.
    state = reconstruct_state((12, -34), _reports(_icon("Cranny", 4000)), "me")
    assert state.estimated_raidable == 0
    assert calculate_score_v2(state, 0, 0) is None
    assert calculate_score(state, 0, 0) is None


def test_a_partial_cranny_leaves_the_difference():
    state = reconstruct_state((12, -34), _reports(_icon("Cranny", 1500)), "me")
    assert state.estimated_raidable == TOTAL - 1500


@pytest.mark.parametrize(
    "second_wrapper",
    [None, _icon("Versteck", 4000) + _icon("Beute", 0)],
    ids=["no-row", "not-english"],
)
def test_a_scout_that_read_neither_figure_refuses_the_target(second_wrapper):
    """No ratio, no guess: the target is simply not scored."""
    state = reconstruct_state((12, -34), _reports(second_wrapper), "me")
    assert state.raidable_confidence == "unreadable"
    assert calculate_score_v2(state, 0, 0) is None
    assert calculate_score(state, 0, 0) is None


def test_the_operator_is_told_why_the_target_was_skipped():
    analyzer = RaidAnalyzerService(
        client=SimpleNamespace(base_url="http://localhost"),
        auth_state=SimpleNamespace(player_name="me"),
    )
    state = reconstruct_state((12, -34), _reports(None), "me")
    state.village_name = "Ghost Town"
    state.distance = 10.0
    result = AnalysisResult()
    warnings: list[str] = []

    scored, _re_scout = analyzer._phase_4_score(
        [state], SimpleNamespace(x=0, y=0), AnalyzerSettings(), result, warnings
    )

    assert scored == []
    assert warnings == [
        "(12|-34) Ghost Town skipped: the scout report carried neither a raidable "
        "figure nor a cranny, so how much can be taken is unknown."
    ]


def test_the_fallback_ratio_is_gone():
    import travian_api.services.raid_analyzer_service as svc

    assert not hasattr(svc, "WAREHOUSE_RATIO")
