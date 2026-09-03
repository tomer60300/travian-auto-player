"""A defender the analyzer cannot score must be refused, never read as absent.

``UNIT_DEF_TABLE`` holds 14 of the 30 unit ids a three-tribe world can field.
Every DEF-summing loop guards with ``if uid in UNIT_DEF_TABLE``, and the hero is
skipped outright, so a garrison the table does not know contributed **zero**
defence -- and ``calculate_score_v2`` then read that zero as "no defenders" and
took the undefended fast path, whose comment is ``profit = loot  # no losses``.

Measured on this file's own fixtures before the fix, a village holding 500
scouts (``u4``) scored byte-for-byte the same as an empty one -- 84 clubs, 5,000
profit, zero predicted dead -- and 100 catapults behind a level-1 trapper scored
the same as an empty village behind that trapper. Every other bad estimate in
the analyzer produces a wrong number; this one asserts nobody dies.

The operator's ruling on the missing stats was "just don't support those
troops", so the fix refuses instead of modelling: any defender id absent from
the table -- the hero included, whose defence depends on attributes no report
carries -- makes the target unscorable. Refusal reuses the module's existing
channel, an ``Optional`` recommendation of ``None``, and the reason reaches the
operator through ``AnalysisResult.warnings``, which both the CLI footer and the
Reports page print verbatim.

The golden numbers below are the pre-fix output, recorded so the refusal cannot
quietly change the arithmetic for a garrison that IS scorable.
"""

from types import SimpleNamespace

import pytest

from travian_api.models.raid_analyzer import (
    AnalysisResult,
    AnalyzerSettings,
    TargetVillageState,
)
from travian_api.services.raid_analyzer_service import (
    RaidAnalyzerService,
    calculate_score,
    calculate_score_v2,
    unsupported_defender_ids,
)


def _state(defenders, traps=0, wall=0, tribe="", raidable=5000):
    """A scouted target with no timestamps, so scoring is wall-clock free.

    ``last_scout_time``/``last_raid_time`` left None pins C_scout at 0.5 and
    C_confirm at 1.0, and keeps eff_R equal to the raw estimate.
    """
    return TargetVillageState(
        x=12,
        y=-34,
        village_name="Ghost Town",
        player_name="afk",
        estimated_raidable=raidable,
        raidable_confidence="scouted",
        defenders=defenders,
        trap_capacity=traps,
        wall_level=wall,
        wall_tribe=tribe,
        distance=10.0,
    )


# ── The refusal ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "defenders,traps",
    [
        ({"u4": 500}, 0),  # scouts -- the unit most often left sitting in a farm
        ({"u24": 40}, 0),  # Theutates Thunder
        ({"u7": 20}, 0),  # rams
        ({"uhero": 1}, 0),  # the defending hero, skipped outright before the fix
        ({"u21": 20, "uhero": 1}, 0),  # recognised troops PLUS an unscorable one
        ({"u21": 20, "u4": 5}, 0),
        ({"u8": 100}, 12),  # catapults behind a trapper: the binary-search path
    ],
)
def test_unscorable_garrison_is_refused(defenders, traps):
    """No recommendation at all -- not an undefended one."""
    state = _state(defenders, traps=traps)
    assert calculate_score_v2(state, 0, 0) is None
    assert calculate_score(state, 0, 0) is None


def test_the_operator_is_told_which_unit_ids_were_unscorable():
    """The reason names the ids, so the skip is not generic."""
    analyzer = RaidAnalyzerService(
        client=SimpleNamespace(base_url="http://localhost"),
        auth_state=SimpleNamespace(player_name="me"),
    )
    result = AnalysisResult()
    warnings: list[str] = []

    scored, re_scout = analyzer._phase_4_score(
        [_state({"u4": 500, "uhero": 1})],
        SimpleNamespace(x=0, y=0),
        AnalyzerSettings(),
        result,
        warnings,
    )

    assert scored == []
    assert re_scout == []
    assert warnings == [
        "(12|-34) Ghost Town skipped: no defence stats for unit ids u4, uhero "
        "— losses cannot be predicted."
    ]


def test_unsupported_defender_ids_names_every_unrecognised_id():
    assert unsupported_defender_ids({"u21": 20, "u4": 5, "uhero": 1}) == ["u4", "uhero"]
    assert unsupported_defender_ids({"u21": 20, "u31": 3}) == []
    assert unsupported_defender_ids({}) == []
    # A zero count is not a defender, so it cannot make a target unscorable.
    assert unsupported_defender_ids({"u4": 0}) == []


# ── What must NOT change ───────────────────────────────────────────────────

# n_send, profit, score, mode, round_trip_minutes, est_loot as recorded pre-fix.
UNCHANGED = {
    "empty village": (_state({}), (84, 5000.0, 875.0, "RAID", 171, 5000)),
    "empty village, trapper L1": (_state({}, traps=12), (96, 2000.0, 350.0, "ATTACK", 171, 5000)),
    "20 phalanx": (_state({"u21": 20}), (93, 2750.0, 481.25, "RAID", 171, 5000)),
    "20 phalanx behind a palisade L10": (
        _state({"u21": 20}, wall=10, tribe="gaul"),
        (96, 2000.0, 350.0, "RAID", 171, 5000),
    ),
    "20 phalanx, trapper L1": (
        _state({"u21": 20}, traps=12),
        (144, 250.0, 43.75, "ATTACK", 171, 5000),
    ),
}


@pytest.mark.parametrize("label", sorted(UNCHANGED))
def test_scoring_is_untouched_for_recognised_defenders(label):
    """A genuinely empty village still takes the fast path, unchanged."""
    state, expected = UNCHANGED[label]
    for rec in (calculate_score_v2(state, 0, 0), calculate_score(state, 0, 0)):
        assert rec is not None, label
        assert (
            rec.n_send,
            rec.profit,
            rec.score,
            rec.mode,
            rec.round_trip_minutes,
            rec.est_loot,
        ) == expected
