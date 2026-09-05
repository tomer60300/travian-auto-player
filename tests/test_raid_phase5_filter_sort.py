"""`_phase_5_filter_sort`'s boundary and ordering had no test at all.

Its three filters (alliance, player, min-resources) and its final sort are
the last thing that runs before a ranked target list reaches the operator --
a wrong boundary silently drops a target, and a wrong sort direction puts the
worst targets at the top of the page, which is where the wave gets sent from.
"""

from types import SimpleNamespace

from tests.test_raid_unsupported_defenders import _state
from travian_api.models.raid_analyzer import AnalysisResult, AnalyzerSettings, RaidRecommendation
from travian_api.services.raid_analyzer_service import RaidAnalyzerService


def _analyzer():
    return RaidAnalyzerService(
        client=SimpleNamespace(base_url="http://localhost"),
        auth_state=SimpleNamespace(player_name="me"),
    )


def test_a_target_worth_exactly_the_minimum_is_kept():
    """`min_resources` is a floor the target may stand on, not a strict cutoff."""
    analyzer = _analyzer()
    result = AnalysisResult()
    at_the_line = (_state({}), RaidRecommendation(n_send=1, score=10.0, est_loot=200))
    below = (_state({}), RaidRecommendation(n_send=1, score=10.0, est_loot=199))

    kept = analyzer._phase_5_filter_sort(
        [at_the_line, below], AnalyzerSettings(min_resources=200), result
    )

    assert [r.est_loot for _, r in kept] == [200]
    assert result.skipped_low_resources == 1


def test_the_ranked_list_leads_with_the_best_target():
    """The list is sorted best-score-first -- it feeds the wave, not a report."""
    analyzer = _analyzer()
    poor = (_state({}), RaidRecommendation(n_send=1, score=10.0, est_loot=5000))
    rich = (_state({}), RaidRecommendation(n_send=1, score=90.0, est_loot=5000))

    ranked = analyzer._phase_5_filter_sort([poor, rich], AnalyzerSettings(), AnalysisResult())

    assert [r.score for _, r in ranked] == [90.0, 10.0]
