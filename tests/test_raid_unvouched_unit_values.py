"""Eleven UNIT_DEF_TABLE rows contradict themselves, so they are not supported.

The table has no source. Read as a whole it disagrees with itself in four ways,
none of which needs a game read to see:

- ``u13`` Axeman is 10, BELOW ``u11`` Clubswinger at 20, though the Axeman is
  strictly the better unit. The Teuton scout slot ``u14`` is absent from the
  table, and 10 is the scout's figure -- a transcription that slipped one row.
- The nature block carries four identical 25s: Rat, Spider, Snake and Bat.
- Wild Boar and Crocodile are both 33.
- ``u40`` Elephant is 55, below ``u39`` Tiger at 60, while costing more upkeep.

Four ties and an inverted ordering are internal evidence the block was never
measured, so the whole nature range ``u31``-``u40`` goes with ``u13``.

The asymmetry that made this bite: ``unsupported_defender_ids`` fails CLOSED for
an id missing from the table, but an id present with a WRONG value silently
mis-scores -- and mis-scoring downward is the direction that gets troops killed.
The operator's ruling for unit values nobody can vouch for is "just don't
support those troops", so the rows are removed rather than guessed at, and the
ids route through the refusal that already exists.

The true figures are OPERATOR TO CONFIRM from the in-game Barracks / Rally Point
unit info; see `.claude/agents/review-data/02-constants-register.md`.
"""

from types import SimpleNamespace

import pytest

from travian_api.models.raid_analyzer import (
    AnalysisResult,
    AnalyzerSettings,
    TargetVillageState,
)
from travian_api.services.raid_analyzer_service import (
    UNIT_DEF_TABLE,
    UNVOUCHED_DEFENDER_IDS,
    RaidAnalyzerService,
    calculate_score,
    calculate_score_v2,
    unsupported_defender_ids,
)

INCONSISTENT = [
    "u13",  # Axeman 10, below Clubswinger 20; u14 (Scout) absent
    "u31",  # Rat 25 ┐
    "u32",  # Spider 25 │ four identical values
    "u33",  # Snake 25 │
    "u34",  # Bat 25 ┘
    "u35",  # Wild Boar 33 ┐ tied with the Crocodile
    "u36",  # Wolf 40      │
    "u37",  # Bear 50      │ same unmeasured block
    "u38",  # Crocodile 33 ┘
    "u39",  # Tiger 60, above the Elephant
    "u40",  # Elephant 55, below the Tiger while costing more upkeep
]


def _state(defenders):
    return TargetVillageState(
        x=12,
        y=-34,
        village_name="Ghost Town",
        estimated_raidable=5000,
        raidable_confidence="scouted",
        defenders=defenders,
        distance=10.0,
        defence_buildings_seen=True,
    )


def test_the_eleven_ids_are_exactly_the_unvouched_set():
    assert sorted(UNVOUCHED_DEFENDER_IDS) == sorted(INCONSISTENT)


@pytest.mark.parametrize("uid", INCONSISTENT)
def test_an_inconsistent_row_carries_no_defence_figure(uid):
    """Removed from the table, so no summing loop can reach the old value."""
    assert uid not in UNIT_DEF_TABLE


@pytest.mark.parametrize("uid", INCONSISTENT)
def test_a_defender_on_an_inconsistent_row_is_refused(uid):
    assert unsupported_defender_ids({uid: 40}) == [uid]
    state = _state({uid: 40})
    assert calculate_score_v2(state, 0, 0) is None
    assert calculate_score(state, 0, 0) is None


def test_the_operator_is_told_which_id_did_it():
    analyzer = RaidAnalyzerService(
        client=SimpleNamespace(base_url="http://localhost"),
        auth_state=SimpleNamespace(player_name="me"),
    )
    warnings: list[str] = []
    scored, _ = analyzer._phase_4_score(
        [_state({"u38": 3})],
        SimpleNamespace(x=0, y=0),
        AnalyzerSettings(),
        AnalysisResult(),
        warnings,
    )

    assert scored == []
    assert warnings == [
        "(12|-34) Ghost Town skipped: no defence stats for unit ids u38 "
        "— losses cannot be predicted."
    ]


def test_a_row_re_added_to_the_table_without_being_vouched_for_still_refuses():
    """The set is the guard, not the table's key list.

    Someone filling the gap from memory rather than from the Barracks page is
    exactly how the bad values got here; re-adding a row is not enough to make
    it trusted.
    """
    assert unsupported_defender_ids({"u21": 20}) == []
    assert unsupported_defender_ids(dict.fromkeys(UNVOUCHED_DEFENDER_IDS, 1)) == sorted(
        UNVOUCHED_DEFENDER_IDS
    )


def test_the_rows_that_are_internally_consistent_are_untouched():
    """Roman and Gaul rows agree with themselves, so they keep scoring."""
    for uid in ("u1", "u2", "u3", "u5", "u6", "u21", "u22", "u25", "u26"):
        assert uid in UNIT_DEF_TABLE
    assert calculate_score_v2(_state({"u21": 20}), 0, 0) is not None
