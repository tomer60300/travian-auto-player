"""A wall nobody looked at is not a wall of level zero.

``reconstruct_state`` took the wall and the trapper from the scout's building
list and applied each only ``if wl > 0`` / ``if tc > 0``. A resources-only
scout -- which is what this app sends by default (``scout_type="resources"``,
``military_service.py``) -- carries no building row at all, so both came out
zero and the target was then scored as having no wall and, worse, no trapper.

The trapper is the load-bearing one. Scoring does ``trapped = min(n, traps)``,
so ``traps = 0`` predicts that nobody is swallowed: the recommendation asserts
"no losses" about a village whose defences were never observed. That is the
same defect ``unsupported_defender_ids`` already refuses for a garrison the
tables cannot score, and it is refused the same way here -- by name, with a
reason the operator can act on: scout the defences.

A scout that DID carry a building list and found no wall and no trapper is a
different thing entirely, and still scores. Zero read is a fact; zero assumed
is not.
"""

from types import SimpleNamespace

import pytest

from travian_api.models.raid_analyzer import (
    AnalysisResult,
    AnalyzerSettings,
    TargetVillageState,
)
from travian_api.services.raid_analyzer_service import RaidAnalyzerService, reconstruct_state


def _scout(buildings, *, timestamp=None):
    return [
        {
            "type": "scout",
            "data": {
                "target": {"village_name": "Ghost Town"},
                "resources": {"lumber": 1000, "clay": 1000, "iron": 1000, "crop": 1000},
                "stealable_resources": {"cranny": 0},
                "troops": {},
                "buildings": buildings,
            },
            "report_id": "r1",
            "timestamp": timestamp,
        }
    ]


WALL_AND_TRAPPER = [
    {"name": "Earth Wall", "detail": "level 12"},
    {"name": "Trapper", "detail": "level 3"},
]
NEITHER = [{"name": "Main Building", "detail": "level 5"}]


def _score(states):
    analyzer = RaidAnalyzerService(
        client=SimpleNamespace(base_url="http://localhost"),
        auth_state=SimpleNamespace(player_name="me"),
    )
    warnings: list[str] = []
    scored, re_scout = analyzer._phase_4_score(
        states, SimpleNamespace(x=0, y=0), AnalyzerSettings(), AnalysisResult(), warnings
    )
    return scored, warnings


def test_a_scout_with_no_building_row_leaves_the_defences_unknown():
    state = reconstruct_state((12, -34), _scout([]), "me")
    assert state.defence_buildings_seen is False


def test_a_scout_that_read_the_buildings_knows_them():
    state = reconstruct_state((12, -34), _scout(WALL_AND_TRAPPER), "me")
    assert state.defence_buildings_seen is True
    assert state.wall_level == 12
    assert state.trap_capacity == 36


def test_a_building_list_without_a_wall_or_trapper_is_a_reading_not_a_gap():
    state = reconstruct_state((12, -34), _scout(NEITHER), "me")
    assert state.defence_buildings_seen is True
    assert state.wall_level == 0
    assert state.trap_capacity == 0


def test_a_target_whose_defences_were_never_seen_is_refused():
    state = reconstruct_state((12, -34), _scout([]), "me")
    state.distance = 10.0
    scored, warnings = _score([state])

    assert scored == []
    assert warnings == [
        "(12|-34) Ghost Town skipped: the scout report carried no defensive "
        "buildings, so the wall and any trapper are unknown — scout defences "
        "before committing troops."
    ]


def test_a_target_whose_defences_were_seen_still_scores():
    state = reconstruct_state((12, -34), _scout(NEITHER), "me")
    state.distance = 10.0
    scored, warnings = _score([state])

    assert warnings == []
    assert len(scored) == 1


@pytest.mark.parametrize("seen", [True, False])
def test_the_flag_is_what_decides_it_not_the_zero(seen):
    """Same zeros on the state; only the provenance differs."""
    state = TargetVillageState(
        x=1,
        y=2,
        village_name="V",
        estimated_raidable=5000,
        raidable_confidence="scouted",
        distance=10.0,
        defence_buildings_seen=seen,
    )
    scored, warnings = _score([state])
    assert (len(scored) == 1) is seen
    assert (warnings == []) is seen
