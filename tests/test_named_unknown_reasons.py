"""Every reason a figure could not be established gets its own name.

The refusals that landed this week -- the cranny, the wall, the trapper, the
scout pre-flight -- all say "no". None of them says WHY, so a trace cannot tell
an empty page from a page with no attacker block, and a debugger is left
interpreting a zero. ``UnknownReason`` replaces the zero with a named code, and
``require_known`` makes it impossible to compute with one.

Two things are pinned here. First, one test per code: the condition that
produces it produces THAT code and not another, because a vocabulary whose
members are interchangeable is no better than the zero it replaced. Second --
and this is the test the whole change exists for -- every arithmetic consumer of
these figures raises when handed a code, rather than computing a plausible wrong
answer from it. ``min(n, -5)`` is -5, which hands the attacker five fighters it
never sent; that is the shape of the bug this guard exists to stop.
"""

from types import SimpleNamespace

import pytest

from travian_api.models.raid_analyzer import TargetVillageState
from travian_api.models.reports import BattleReportData
from travian_api.models.unknown_reason import (
    UnknownFigureError,
    UnknownReason,
    is_unknown,
    reason_name,
    reason_of,
    require_known,
)
from travian_api.parsers.report_parser import parse_battle_report
from travian_api.services.farm_builder_service import lookup_troop_row
from travian_api.services.raid_analyzer_service import (
    _score_defended_binary,
    calculate_score,
    calculate_score_v2,
    reconstruct_state,
)
from travian_api.web.routes.farm import SlotDefenseInfo, _fetch_defense_for_coord

ALL_REASONS = list(UnknownReason)


# ── The vocabulary ─────────────────────────────────────────────────────────


def test_the_operators_codes_are_the_codes():
    """The numbers are the operator's choice; the names are what a log prints."""
    assert {r.name: int(r) for r in UnknownReason} == {
        "NEVER_RAIDED": -1,
        "UNPARSEABLE_PAGE": -2,
        "EMPTY_RESPONSE": -3,
        "NO_ATTACKER_BLOCK": -4,
        "NO_BUILDING_ROW": -5,
        "STALE_BEYOND_HORIZON": -6,
    }


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_a_code_is_never_mistaken_for_a_quantity(reason):
    """Every figure these stand in for is non-negative, so `< 0` settles it."""
    assert is_unknown(reason)
    assert reason_of(reason) is reason
    assert reason_name(reason) == reason.name


@pytest.mark.parametrize("figure", [0, 1, 12, 5000])
def test_a_real_figure_is_not_an_unknown(figure):
    assert not is_unknown(figure)
    assert reason_of(figure) is None
    assert reason_name(figure) is None
    assert require_known(figure, "wall_level") == figure


def test_the_guard_names_the_field_and_the_reason():
    """ "could not be computed" is useless; "trap_capacity: NO_BUILDING_ROW" is not."""
    with pytest.raises(UnknownFigureError) as excinfo:
        require_known(UnknownReason.NO_BUILDING_ROW, "trap_capacity")
    assert excinfo.value.field == "trap_capacity"
    assert excinfo.value.reason is UnknownReason.NO_BUILDING_ROW
    assert "trap_capacity" in str(excinfo.value)
    assert "NO_BUILDING_ROW" in str(excinfo.value)


def test_an_unrecognised_negative_is_refused_too():
    """The guard's contract is "not a quantity", not "one of my six"."""
    with pytest.raises(UnknownFigureError):
        require_known(-99, "wall_level")


# ── HTML fixtures, shaped like the game's own pages ────────────────────────


def _units_tbody(uid: str, count: int) -> str:
    return (
        f'<tbody class="units"><tr><td class="uniticon">'
        f'<img class="unit {uid}" alt="{uid}"/></td></tr></tbody>'
        f'<tbody class="units"><tr><td class="unit">{count}</td></tr></tbody>'
    )


def _losses_tbody(lost: int) -> str:
    return f'<tbody class="units last"><tr><td class="unit">{lost}</td></tr></tbody>'


def _role(role: str, uid: str = "u1", count: int = 10, lost: int | None = None) -> str:
    """One role block. *lost* None leaves the losses row off the page entirely."""
    body = _units_tbody(uid, count)
    if lost is not None:
        body += _losses_tbody(lost)
    return f'<div class="role {role}"><table>{body}</table></div>'


def _combat_stats(attacker: int, defender: int) -> str:
    return (
        '<table class="combatStatistic"><tr><th>Combat strength</th>'
        f'<td><span class="value">{attacker}</span></td>'
        f'<td><span class="value">{defender}</span></td></tr></table>'
    )


def _battle_html(*, attacker: str = "", defender: str = "", combat: str = "") -> str:
    return f"<html><body>{attacker}{defender}{combat}</body></html>"


def _scout_reports(buildings: list[dict]) -> list[dict]:
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
            "timestamp": None,
        }
    ]


# ── One producer per code ──────────────────────────────────────────────────


def test_no_attacker_block_is_the_reason_when_the_block_is_absent():
    """A defender-only page carries no attacker losses because it carries no attacker."""
    data = parse_battle_report(
        _battle_html(defender=_role("defender", lost=0), combat=_combat_stats(100, 50))
    )
    assert data.attacker_losses_unknown is UnknownReason.NO_ATTACKER_BLOCK
    assert data.attacker_losses == {}


def test_unparseable_page_is_the_reason_when_the_block_has_no_losses_row():
    """The attacker block IS there -- a different failure, so a different code."""
    data = parse_battle_report(
        _battle_html(
            attacker=_role("attacker", lost=None),
            defender=_role("defender", lost=0),
            combat=_combat_stats(100, 50),
        )
    )
    assert data.attacker_losses_unknown is UnknownReason.UNPARSEABLE_PAGE


def test_unparseable_page_is_the_reason_when_the_combat_row_is_missing():
    """Zero is a legitimate combat strength -- an undefended village -- so an
    unread combatStatistic must not answer with one."""
    data = parse_battle_report(
        _battle_html(attacker=_role("attacker", lost=0), defender=_role("defender", lost=0))
    )
    assert data.defender_combat_strength == UnknownReason.UNPARSEABLE_PAGE
    assert data.attacker_combat_strength == UnknownReason.UNPARSEABLE_PAGE


def test_empty_response_is_the_reason_when_the_page_is_blank():
    data = parse_battle_report("   ")
    assert data.attacker_losses_unknown is UnknownReason.EMPTY_RESPONSE
    assert data.defender_combat_strength == UnknownReason.EMPTY_RESPONSE


def test_no_building_row_is_the_reason_when_the_scout_carried_no_buildings():
    """A resources-only scout -- what this app sends by default -- has no
    building row, so the wall and the trapper were never observed."""
    state = reconstruct_state((12, -34), _scout_reports([]), "me")
    assert state.wall_level == UnknownReason.NO_BUILDING_ROW
    assert state.trap_capacity == UnknownReason.NO_BUILDING_ROW
    assert state.defence_buildings_seen is False


def test_a_building_row_replaces_the_reason_with_a_reading():
    """Zero read is a fact; zero assumed is not. Both must stay distinguishable."""
    state = reconstruct_state(
        (12, -34), _scout_reports([{"name": "Main Building", "detail": "level 5"}]), "me"
    )
    assert state.wall_level == 0
    assert state.trap_capacity == 0
    assert state.defence_buildings_seen is True


def test_stale_beyond_horizon_has_no_producer_yet():
    """Defined by the operator, deliberately unraised.

    The only site that would carry it is `_phase_4_score`'s staleness branch,
    which today files a re-scout AND still scores the target ("stale data is
    better than none"). Making the reason explicit there means refusing, which
    is an outcome change nobody has ruled on -- so the code stays in the
    vocabulary and this test records why nothing emits it.
    """
    import inspect

    import travian_api.services.raid_analyzer_service as analyzer
    import travian_api.web.routes.farm as farm_routes

    for module in (analyzer, farm_routes):
        assert "STALE_BEYOND_HORIZON" not in inspect.getsource(module)


# ── The defence scan: which failure, not just "no data" ────────────────────


class _Reports:
    """The two report reads the defence scan makes, each able to fail."""

    def __init__(self, *, tile=None, tile_error=None, detail=None, detail_error=None):
        self._tile = tile if tile is not None else {"reports": []}
        self._tile_error = tile_error
        self._detail = detail
        self._detail_error = detail_error

    async def fetch_village_reports(self, x, y, fetch_details=False):
        if self._tile_error is not None:
            raise self._tile_error
        return self._tile

    async def fetch_report_detail(self, report_id):
        if self._detail_error is not None:
            raise self._detail_error
        return self._detail


ONE_BATTLE_ON_FILE = {"reports": [{"icon_type": 3, "report_id": "r1", "aid": "a1"}]}


def _session(reports):
    return SimpleNamespace(reports_service=reports)


async def test_never_raided_is_the_reason_when_no_raid_is_on_file():
    """Defence is read out of this account's own raid reports. No raid, no reading."""
    row = await _fetch_defense_for_coord(_session(_Reports(tile={"reports": []})), 12, 120)
    assert row["defender_combat_strength"] is None
    assert row["defender_unknown_reason"] == UnknownReason.NEVER_RAIDED.name


async def test_unparseable_page_is_the_reason_when_the_tile_read_fails():
    row = await _fetch_defense_for_coord(
        _session(_Reports(tile_error=RuntimeError("tile 500"))), 12, 120
    )
    assert row["defender_unknown_reason"] == UnknownReason.UNPARSEABLE_PAGE.name


async def test_empty_response_is_the_reason_when_the_detail_carried_no_battle():
    row = await _fetch_defense_for_coord(
        _session(_Reports(tile=ONE_BATTLE_ON_FILE, detail={"type": "battle", "data": None})),
        12,
        120,
    )
    assert row["defender_unknown_reason"] == UnknownReason.EMPTY_RESPONSE.name


async def test_the_parsers_reason_is_carried_forward_not_re_labelled():
    """The report is on file; its combatStatistic is not readable. The scan
    reports the parser's finding rather than inventing one of its own."""
    battle = SimpleNamespace(
        defender_troops={"t1": 40},
        defender_combat_strength=UnknownReason.UNPARSEABLE_PAGE,
    )
    row = await _fetch_defense_for_coord(
        _session(_Reports(tile=ONE_BATTLE_ON_FILE, detail={"type": "battle", "data": battle})),
        12,
        120,
    )
    assert row["defender_combat_strength"] is None
    assert row["defender_unknown_reason"] == UnknownReason.UNPARSEABLE_PAGE.name


async def test_a_readable_defence_carries_no_reason_at_all():
    battle = SimpleNamespace(defender_troops={"t1": 40}, defender_combat_strength=900)
    row = await _fetch_defense_for_coord(
        _session(_Reports(tile=ONE_BATTLE_ON_FILE, detail={"type": "battle", "data": battle})),
        12,
        120,
    )
    assert row["defender_combat_strength"] == 900
    assert row["defender_unknown_reason"] is None
    assert row["defender_total"] == 40


# ── The parser: an unread block is not an empty one ────────────────────────


def test_a_loss_free_raid_reports_no_losses_and_no_reason():
    """The losses row WAS read and every unit came home. `{}` means that, and
    nothing else, now that the unread case has a code of its own."""
    data = parse_battle_report(
        _battle_html(
            attacker=_role("attacker", lost=0),
            defender=_role("defender", lost=0),
            combat=_combat_stats(100, 50),
        )
    )
    assert data.attacker_losses == {}
    assert data.attacker_losses_unknown is None


def test_a_costly_raid_reports_the_losses_it_read():
    data = parse_battle_report(
        _battle_html(
            attacker=_role("attacker", uid="u1", count=10, lost=3),
            defender=_role("defender", lost=0),
            combat=_combat_stats(100, 50),
        )
    )
    assert data.attacker_losses == {"u1": 3}
    assert data.attacker_losses_unknown is None


def test_the_two_empty_cases_are_no_longer_the_same_value():
    """The bug in one assertion: both used to be `attacker_losses == {}`."""
    loss_free = parse_battle_report(
        _battle_html(attacker=_role("attacker", lost=0), combat=_combat_stats(100, 50))
    )
    unread = parse_battle_report(
        _battle_html(attacker=_role("attacker", lost=None), combat=_combat_stats(100, 50))
    )
    assert loss_free.attacker_losses == unread.attacker_losses == {}
    assert loss_free.attacker_losses_unknown != unread.attacker_losses_unknown


# ── The test this change exists for: no arithmetic accepts a code ──────────


def _scorable(**overrides) -> TargetVillageState:
    """A target that reaches the wall/trapper arithmetic, so the guard is what
    stops it and not an earlier refusal."""
    fields = {
        "x": 12,
        "y": -34,
        "village_name": "Ghost Town",
        "estimated_raidable": 5000,
        "raidable_confidence": "scouted",
        "defenders": {},
        "distance": 10.0,
        "defence_buildings_seen": True,
    }
    fields.update(overrides)
    return TargetVillageState(**fields)


def test_the_reference_state_scores_so_the_guard_is_what_refuses_it():
    assert calculate_score(_scorable(), 0, 0) is not None
    assert calculate_score_v2(_scorable(), 0, 0) is not None


@pytest.mark.parametrize("reason", ALL_REASONS)
@pytest.mark.parametrize("field", ["wall_level", "trap_capacity"])
@pytest.mark.parametrize("scorer", [calculate_score, calculate_score_v2])
def test_no_scoring_path_computes_with_a_reason_code(scorer, field, reason):
    """`min(n, -5)` is -5: an unknown trapper becomes five extra fighters and
    the recommendation asserts a wave that cannot exist. Raise instead."""
    state = _scorable(**{field: reason})
    with pytest.raises(UnknownFigureError) as excinfo:
        scorer(state, 0, 0)
    assert excinfo.value.field == field
    assert excinfo.value.reason is reason


@pytest.mark.parametrize("reason", ALL_REASONS)
@pytest.mark.parametrize("field", ["wall_level", "trap_capacity"])
def test_the_binary_search_path_refuses_a_reason_code_too(field, reason):
    """The defended path is reached directly by `calculate_score_v2`'s fast-path
    miss, so it carries its own guard rather than trusting its caller."""
    state = _scorable(**{field: reason})
    with pytest.raises(UnknownFigureError):
        _score_defended_binary(state, 5000.0, 10.0, 40.0, 0, 0, None, None)


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_no_troop_row_is_selected_from_a_reason_code(reason):
    """A code falls outside every DEF range, so without the guard this answers
    "out of range" and the target is skipped for the wrong reason."""
    with pytest.raises(UnknownFigureError) as excinfo:
        lookup_troop_row(reason, "teutons")
    assert excinfo.value.field == "defender_combat_strength"


# ── The wire: a code leaves as a name, never as a number ───────────────────


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_a_battle_report_serialises_the_reason_by_name(reason):
    dumped = BattleReportData(
        attacker_losses_unknown=reason,
        attacker_combat_strength=reason,
        defender_combat_strength=reason,
    ).model_dump(mode="json")
    assert dumped["attacker_losses_unknown"] == reason.name
    assert dumped["attacker_combat_strength"] == reason.name
    assert dumped["defender_combat_strength"] == reason.name


def test_a_real_combat_strength_still_serialises_as_a_number():
    dumped = BattleReportData(defender_combat_strength=900).model_dump(mode="json")
    assert dumped["defender_combat_strength"] == 900


@pytest.mark.parametrize("reason", ALL_REASONS)
def test_a_target_state_serialises_the_reason_by_name(reason):
    """The analyzer websocket sends the whole state; a raw -5 in `wall_level`
    reads as a level."""
    dumped = TargetVillageState(wall_level=reason, trap_capacity=reason).model_dump(mode="json")
    assert dumped["wall_level"] == reason.name
    assert dumped["trap_capacity"] == reason.name


def test_the_defence_scan_row_never_carries_a_negative_strength():
    """The Defence column reads `defender_combat_strength` as a number, so the
    unknown case must arrive as null plus a named reason."""
    row = SlotDefenseInfo(
        slot_id=1,
        x=12,
        y=120,
        name="Ghost Town",
        defender_unknown_reason=UnknownReason.NEVER_RAIDED.name,
    ).model_dump()
    assert row["defender_combat_strength"] is None
    assert row["defender_unknown_reason"] == "NEVER_RAIDED"
