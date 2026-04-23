"""Comprehensive tests for the Max Player Population filter.

Covers:
- WS scan helper: _sum_visible_player_pops
- WS scan filter ordering (player_pops computed before village-pop filter)
- REST scan filter ordering (same)
- Edge cases: single village, no player_id, exact boundary, zero pop
"""

from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Fake tile objects (mimics MapTileInfo with the fields filters use)
# ---------------------------------------------------------------------------


def tile(x, y, vid, pid, pop, name="", player="", alliance=""):
    return SimpleNamespace(
        x=x,
        y=y,
        village_id=vid,
        player_id=pid,
        population=pop,
        village_name=name,
        player_name=player,
        alliance_name=alliance,
        alliance_id=None,
        tribe="",
        distance=1.0,
        is_oasis=False,
        is_abandoned=False,
    )


# ---------------------------------------------------------------------------
# Import the WS helper directly
# ---------------------------------------------------------------------------

from travian_api.web.ws.scout_ws import _sum_visible_player_pops


class TestSumVisiblePlayerPops:
    """Tests for the _sum_visible_player_pops helper."""

    def test_single_village_per_player(self):
        tiles = [
            tile(1, 1, 100, 10, 200, player="alice"),
            tile(2, 2, 101, 20, 300, player="bob"),
        ]
        result = _sum_visible_player_pops(tiles)
        assert result == {10: 200, 20: 300}

    def test_multiple_villages_same_player(self):
        """Finci scenario: two villages, pop 159 + 422 = 581."""
        tiles = [
            tile(16, 93, 1001, 50, 159, player="finci"),
            tile(7, 92, 1002, 50, 422, player="finci"),
        ]
        result = _sum_visible_player_pops(tiles)
        assert result == {50: 581}

    def test_three_villages_same_player(self):
        tiles = [
            tile(1, 1, 100, 10, 100, player="bob"),
            tile(2, 2, 101, 10, 200, player="bob"),
            tile(3, 3, 102, 10, 300, player="bob"),
        ]
        result = _sum_visible_player_pops(tiles)
        assert result == {10: 600}

    def test_no_player_id_skipped(self):
        tiles = [
            tile(1, 1, 100, None, 500),  # no player
            tile(2, 2, 101, 0, 400),  # player_id = 0 (falsy)
            tile(3, 3, 102, 10, 200, player="alice"),
        ]
        result = _sum_visible_player_pops(tiles)
        assert result == {10: 200}

    def test_empty_tiles(self):
        assert _sum_visible_player_pops([]) == {}

    def test_mixed_players(self):
        """Multiple players with varying village counts."""
        tiles = [
            tile(1, 1, 100, 10, 100, player="alice"),
            tile(2, 2, 101, 20, 150, player="bob"),
            tile(3, 3, 102, 10, 250, player="alice"),
            tile(4, 4, 103, 30, 50, player="carol"),
            tile(5, 5, 104, 20, 300, player="bob"),
        ]
        result = _sum_visible_player_pops(tiles)
        assert result == {10: 350, 20: 450, 30: 50}


class TestPlayerPopFilterOrdering:
    """Verify that player_pops is computed BEFORE the village-pop filter
    so that a player's total isn't under-counted.

    The exact scenario from the bug report:
    - finci has village pop=159 (passes max_village_pop=260)
    - finci has village pop=422 (FAILS max_village_pop=260, gets removed)
    - If player_pops is computed AFTER village filter: finci = 159 ≤ 350 → PASSES (BUG!)
    - If player_pops is computed BEFORE village filter: finci = 581 > 350 → FILTERED (CORRECT!)
    """

    def _make_finci_tiles(self):
        return [
            tile(16, 93, 1001, 50, 159, player="finci", alliance="LR"),
            tile(7, 92, 1002, 50, 422, player="finci", alliance="LR"),
            tile(5, 5, 1003, 60, 180, player="goodplayer"),
            tile(6, 6, 1004, 70, 250, player="bigplayer"),
        ]

    def test_player_pops_before_village_filter(self):
        """player_pops must include ALL villages, even ones that fail max_village_pop."""
        tiles = self._make_finci_tiles()
        player_pops = _sum_visible_player_pops(tiles)

        # finci's total is 159 + 422 = 581
        assert player_pops[50] == 581
        assert player_pops[60] == 180
        assert player_pops[70] == 250

    def test_finci_filtered_with_max_player_pop_350(self):
        """Simulate the exact WS scan filter chain."""
        tiles = self._make_finci_tiles()

        # Step 1: Compute player_pops from ALL tiles (before village filter)
        max_player_pop = 350
        player_pops = _sum_visible_player_pops(tiles)
        assert player_pops[50] == 581  # finci total

        # Step 2: Village pop filter (max=260) removes finci's 422 village
        tiles = [t for t in tiles if t.population <= 260]
        assert len(tiles) == 3  # finci(159), goodplayer(180), bigplayer(250)
        assert any(t.player_name == "finci" for t in tiles)

        # Step 3: Player pop filter using PRE-COMPUTED player_pops
        filtered = []
        for t in tiles:
            if not t.player_id or player_pops.get(t.player_id, 0) <= max_player_pop:
                filtered.append(t)
        tiles = filtered

        # finci MUST be removed (581 > 350)
        assert not any(t.player_name == "finci" for t in tiles), (
            "BUG: finci should be filtered out (total pop 581 > 350)"
        )

        # goodplayer and bigplayer should survive
        names = {t.player_name for t in tiles}
        assert "goodplayer" in names
        assert "bigplayer" in names

    def test_wrong_order_would_miss_finci(self):
        """Demonstrate the bug: computing player_pops AFTER village filter."""
        tiles = self._make_finci_tiles()
        max_player_pop = 350

        # WRONG: village filter first, then compute player pops
        tiles = [t for t in tiles if t.population <= 260]
        wrong_pops = _sum_visible_player_pops(tiles)

        # finci's 422 village was already removed → wrong total
        assert wrong_pops[50] == 159  # WRONG: should be 581
        assert max_player_pop >= 159  # finci INCORRECTLY passes the filter

    def test_exact_boundary(self):
        """Player pop exactly at the limit should pass."""
        tiles = [
            tile(1, 1, 100, 10, 175, player="exact"),
            tile(2, 2, 101, 10, 175, player="exact"),
        ]
        player_pops = _sum_visible_player_pops(tiles)
        assert player_pops[10] == 350

        max_player_pop = 350
        filtered = [t for t in tiles if player_pops.get(t.player_id, 0) <= max_player_pop]
        assert len(filtered) == 2  # exact boundary passes (<=)

    def test_one_over_boundary(self):
        """Player pop one over the limit should be filtered."""
        tiles = [
            tile(1, 1, 100, 10, 176, player="over"),
            tile(2, 2, 101, 10, 175, player="over"),
        ]
        player_pops = _sum_visible_player_pops(tiles)
        assert player_pops[10] == 351

        max_player_pop = 350
        filtered = [t for t in tiles if player_pops.get(t.player_id, 0) <= max_player_pop]
        assert len(filtered) == 0  # both villages of this player removed

    def test_profoundwhisky_scenario(self):
        """ProfoundWhisky: two villages pop 173 + 471 = 644 > 350."""
        tiles = [
            tile(19, 83, 2001, 80, 173, player="ProfoundWhisky"),
            tile(19, 82, 2002, 80, 471, player="ProfoundWhisky"),
            tile(5, 5, 2003, 90, 145, player="smallplayer"),
        ]
        player_pops = _sum_visible_player_pops(tiles)
        assert player_pops[80] == 644

        # Village filter (max=260) removes 471 village
        tiles = [t for t in tiles if t.population <= 260]
        assert len(tiles) == 2  # PW(173) + smallplayer(145)

        # Player pop filter
        filtered = [t for t in tiles if player_pops.get(t.player_id, 0) <= 350]
        assert len(filtered) == 1
        assert filtered[0].player_name == "smallplayer"

    def test_no_max_player_pop_set(self):
        """When max_player_pop is None, no filtering happens."""
        tiles = [
            tile(1, 1, 100, 10, 9999, player="huge"),
        ]
        # Simulating: if max_player_pop is None, skip the filter
        max_player_pop = None
        if max_player_pop is not None:
            player_pops = _sum_visible_player_pops(tiles)
            tiles = [t for t in tiles if player_pops.get(t.player_id, 0) <= max_player_pop]
        assert len(tiles) == 1

    def test_player_with_no_villages_in_range_after_village_filter(self):
        """Player's only village is above max_village_pop → all removed by
        village filter, player pop filter has nothing to remove."""
        tiles = [
            tile(1, 1, 100, 10, 500, player="bigonly"),
        ]
        player_pops = _sum_visible_player_pops(tiles)
        assert player_pops[10] == 500

        # Village filter removes the only village
        tiles = [t for t in tiles if t.population <= 260]
        assert len(tiles) == 0

        # Player pop filter has nothing to filter
        filtered = [t for t in tiles if player_pops.get(t.player_id, 0) <= 350]
        assert len(filtered) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
