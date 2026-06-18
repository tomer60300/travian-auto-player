"""Tests for the "Non-capitals" target type.

The filter drops tiles where ``is_capital`` is True. is_capital is
populated upstream by the profile-fetch phase via ``capital_map``,
which itself only runs when ``want_capital_info`` is True. That gate
is now tied to the ``non_capitals`` config flag — every other mode
skips the entire profile-fetch phase, saving ~N HTTP requests per
scan (where N is the unique-player count in the scan area).
"""

from __future__ import annotations

from travian_api.models.farm_list import MapTileInfo


def _apply_non_capitals_filter(tiles: list[MapTileInfo], non_capitals: bool) -> list[MapTileInfo]:
    """Mirror of the scout_ws.py post-filter block — kept here so
    tests don't have to spin up the full WS coroutine just to assert
    list-membership behaviour."""
    if not non_capitals:
        return tiles
    return [t for t in tiles if not t.is_capital]


def _village(x: int, y: int, is_capital: bool = False) -> MapTileInfo:
    return MapTileInfo(
        x=x,
        y=y,
        is_oasis=False,
        village_id=x * 100 + y,
        player_id=1,
        population=500,
        is_capital=is_capital,
    )


def test_non_capitals_filter_drops_capitals() -> None:
    tiles = [
        _village(1, 1, is_capital=False),
        _village(2, 2, is_capital=True),
        _village(3, 3, is_capital=False),
    ]
    out = _apply_non_capitals_filter(tiles, non_capitals=True)
    assert sorted(t.x for t in out) == [1, 3]


def test_non_capitals_off_keeps_everything() -> None:
    """When the user picks any OTHER target type, the upstream code
    path didn't fetch profiles, so is_capital is False everywhere —
    the filter is therefore a no-op even when accidentally enabled.
    Verifying the explicit "off" path doesn't drop anything."""
    tiles = [
        _village(1, 1, is_capital=False),
        _village(2, 2, is_capital=True),
    ]
    out = _apply_non_capitals_filter(tiles, non_capitals=False)
    assert len(out) == 2


def test_non_capitals_filter_with_no_capitals_is_noop() -> None:
    """If the scan area happens to contain no capitals (unlikely but
    possible for small radii of one player), the filter doesn't drop
    anything."""
    tiles = [
        _village(1, 1, is_capital=False),
        _village(2, 2, is_capital=False),
    ]
    out = _apply_non_capitals_filter(tiles, non_capitals=True)
    assert len(out) == 2


def test_want_capital_info_derivation_matches_non_capitals_flag() -> None:
    """Structural check on scout_ws.py: the only path that sets
    ``want_capital_info`` is the new non_capitals-gated one. The
    legacy ``show_capitals`` config read must be GONE so removing the
    frontend toggle doesn't leave a vestigial unused config key."""
    import pathlib

    src = pathlib.Path("src/travian_api/web/ws/scout_ws.py").read_text(encoding="utf-8")
    # No reads of the old config key.
    assert 'config.get("show_capitals"' not in src, (
        "show_capitals is removed from the UI; backend must not "
        "depend on it. Found a lingering read."
    )
    # want_capital_info is assigned exactly once, from non_capitals.
    import re

    assignments = re.findall(r"^\s*want_capital_info\s*=\s*(.+)$", src, re.MULTILINE)
    assert assignments == ["non_capitals"], (
        f"want_capital_info should be assigned exactly once from non_capitals; got {assignments!r}"
    )
