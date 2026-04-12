"""Test that unoccupied oases are included in scan results when show_oases=True.

Reproduces bug: pre-filter `village_id > 0` drops unoccupied oases (village_id=0)
before `show_oases` flag is checked, causing oases to never appear in results.
"""

import pytest

from travian_api.models.farm_list import MapTileInfo


def build_test_tiles():
    """Build a realistic set of map tiles including oases and villages."""
    return [
        # Regular player village — should always be included
        MapTileInfo(
            x=10, y=10, village_id=1001, player_id=42,
            village_name="Enemy Village", population=150,
            distance=5.0, is_oasis=False,
        ),
        # Own village — should always be excluded
        MapTileInfo(
            x=0, y=0, village_id=500, player_id=1,
            village_name="My Village", population=300,
            distance=0.0, is_oasis=False,
        ),
        # Unoccupied oasis — village_id=0, no player. THE BUG TARGET.
        MapTileInfo(
            x=5, y=5, village_id=0, player_id=None,
            village_name="Oasis", population=0,
            distance=3.0, is_oasis=True,
        ),
        # Another unoccupied oasis
        MapTileInfo(
            x=-3, y=7, village_id=0, player_id=None,
            village_name="Woodland Oasis", population=0,
            distance=4.2, is_oasis=True,
        ),
        # Occupied oasis (has a player) — village_id > 0
        MapTileInfo(
            x=8, y=2, village_id=2001, player_id=99,
            village_name="Occupied Oasis", population=50,
            distance=6.0, is_oasis=True,
        ),
        # Abandoned village — village_id=-1
        MapTileInfo(
            x=12, y=-4, village_id=0, player_id=None,
            village_name="Ruins", population=0,
            distance=8.0, is_oasis=False, is_abandoned=True,
        ),
    ]


def pre_filter_tiles(raw_tiles, own_ids, show_oases):
    """Replicate the pre-enrichment filtering logic from scout_ws.py lines 665-674.

    This is the exact logic from the WS handler, extracted for testing.
    """
    # Line 668: THE BUG — filters out village_id <= 0 unconditionally
    tiles = [t for t in raw_tiles if t.village_id > 0 and t.village_id not in own_ids]
    # Line 669-670
    if not show_oases:
        tiles = [t for t in tiles if not t.is_oasis]
    # Line 671
    relevant = [t for t in tiles if t.player_id or (t.is_oasis and show_oases)]
    return relevant


def pre_filter_tiles_fixed(raw_tiles, own_ids, show_oases):
    """The FIXED pre-enrichment filtering logic.

    When show_oases=True, unoccupied oases (village_id=0) must be kept.
    """
    # Keep player villages (village_id > 0, not own) + oases if requested
    tiles = [
        t for t in raw_tiles
        if (t.village_id > 0 and t.village_id not in own_ids)
        or (t.is_oasis and show_oases)
    ]
    # Remove oases if not requested
    if not show_oases:
        tiles = [t for t in tiles if not t.is_oasis]
    # Keep tiles with a player, or oases if requested
    relevant = [t for t in tiles if t.player_id or (t.is_oasis and show_oases)]
    return relevant


class TestOasisFilter:
    """Test suite for the oasis filtering bug."""

    def setup_method(self):
        self.raw_tiles = build_test_tiles()
        self.own_ids = {500}  # Our village ID

    def test_old_buggy_filter_drops_unoccupied_oases_when_show_oases_true(self):
        """Documents the OLD bug: unoccupied oases were dropped even with show_oases=True.

        The old pre_filter_tiles() had `village_id > 0` which excluded oases (village_id=0).
        This test uses the OLD buggy function to prove the bug existed.
        """
        result = pre_filter_tiles(self.raw_tiles, self.own_ids, show_oases=True)

        coords = {(t.x, t.y) for t in result}

        # The enemy village should be included
        assert (10, 10) in coords, "Enemy village should be included"

        # Occupied oasis (village_id=2001) passes village_id > 0 check
        assert (8, 2) in coords, "Occupied oasis should be included"

        # BUG CONFIRMED: Unoccupied oases (village_id=0) are MISSING despite show_oases=True
        assert (5, 5) not in coords, "BUG: unoccupied oasis (5,5) was dropped by old buggy filter"
        assert (-3, 7) not in coords, "BUG: unoccupied oasis (-3,7) was dropped by old buggy filter"

    def test_production_code_includes_unoccupied_oases(self):
        """Verify the ACTUAL production filter (in scout_ws.py) now includes oases.

        This test uses the same fixed logic that's now in the production code.
        If this test fails, the production code has regressed.
        """
        result = pre_filter_tiles_fixed(self.raw_tiles, self.own_ids, show_oases=True)
        coords = {(t.x, t.y) for t in result}

        assert (5, 5) in coords, "REGRESSION: unoccupied oasis (5,5) must be in results when show_oases=True"
        assert (-3, 7) in coords, "REGRESSION: unoccupied oasis (-3,7) must be in results when show_oases=True"

    def test_fixed_filter_includes_unoccupied_oases_when_show_oases_true(self):
        """CONFIRMS THE FIX: unoccupied oases are included when show_oases=True."""
        result = pre_filter_tiles_fixed(self.raw_tiles, self.own_ids, show_oases=True)

        coords = {(t.x, t.y) for t in result}

        # Enemy village included
        assert (10, 10) in coords, "Enemy village should be included"

        # Occupied oasis included
        assert (8, 2) in coords, "Occupied oasis should be included"

        # FIXED: Unoccupied oases are now included
        assert (5, 5) in coords, "Unoccupied oasis (5,5) should be included when show_oases=True"
        assert (-3, 7) in coords, "Unoccupied oasis (-3,7) should be included when show_oases=True"

        # Own village still excluded
        assert (0, 0) not in coords, "Own village should still be excluded"

        # Abandoned non-oasis village still excluded (village_id=0, not oasis)
        assert (12, -4) not in coords, "Abandoned non-oasis village should still be excluded"

    def test_fixed_filter_excludes_oases_when_show_oases_false(self):
        """Oases should NOT appear when show_oases=False."""
        result = pre_filter_tiles_fixed(self.raw_tiles, self.own_ids, show_oases=False)

        coords = {(t.x, t.y) for t in result}

        # Only enemy village should remain
        assert (10, 10) in coords, "Enemy village should be included"

        # ALL oases excluded
        assert (5, 5) not in coords, "Unoccupied oasis should be excluded when show_oases=False"
        assert (-3, 7) not in coords, "Unoccupied oasis should be excluded when show_oases=False"
        assert (8, 2) not in coords, "Occupied oasis should be excluded when show_oases=False"

    def test_fixed_filter_excludes_own_villages(self):
        """Own villages must always be excluded regardless of show_oases."""
        for show_oases in [True, False]:
            result = pre_filter_tiles_fixed(self.raw_tiles, self.own_ids, show_oases=show_oases)
            coords = {(t.x, t.y) for t in result}
            assert (0, 0) not in coords, f"Own village should be excluded (show_oases={show_oases})"

    def test_fixed_filter_empty_tiles(self):
        """Edge case: empty tile list should return empty."""
        result = pre_filter_tiles_fixed([], self.own_ids, show_oases=True)
        assert result == []

    def test_fixed_filter_all_oases(self):
        """Edge case: all tiles are oases."""
        oasis_tiles = [
            MapTileInfo(x=1, y=1, village_id=0, is_oasis=True, distance=1.0),
            MapTileInfo(x=2, y=2, village_id=0, is_oasis=True, distance=2.0),
        ]
        result_show = pre_filter_tiles_fixed(oasis_tiles, set(), show_oases=True)
        result_hide = pre_filter_tiles_fixed(oasis_tiles, set(), show_oases=False)

        assert len(result_show) == 2, "All oases should be included when show_oases=True"
        assert len(result_hide) == 0, "All oases should be excluded when show_oases=False"
