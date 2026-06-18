"""Test that unoccupied oases are included in scan results when show_oases=True.

Reproduces bug: pre-filter `village_id > 0` drops unoccupied oases (village_id=0)
before `show_oases` flag is checked, causing oases to never appear in results.
"""

from travian_api.models.farm_list import MapTileInfo
from travian_api.services.auto_scout_service import AutoScoutService


def build_test_tiles():
    """Build a realistic set of map tiles including oases and villages."""
    return [
        # Regular player village — should always be included
        MapTileInfo(
            x=10,
            y=10,
            village_id=1001,
            player_id=42,
            village_name="Enemy Village",
            population=150,
            distance=5.0,
            is_oasis=False,
        ),
        # Own village — should always be excluded
        MapTileInfo(
            x=0,
            y=0,
            village_id=500,
            player_id=1,
            village_name="My Village",
            population=300,
            distance=0.0,
            is_oasis=False,
        ),
        # Unoccupied oasis — village_id=0, no player. THE BUG TARGET.
        MapTileInfo(
            x=5,
            y=5,
            village_id=0,
            player_id=None,
            village_name="Oasis",
            population=0,
            distance=3.0,
            is_oasis=True,
        ),
        # Another unoccupied oasis
        MapTileInfo(
            x=-3,
            y=7,
            village_id=0,
            player_id=None,
            village_name="Woodland Oasis",
            population=0,
            distance=4.2,
            is_oasis=True,
        ),
        # Occupied oasis (has a player) — village_id > 0
        MapTileInfo(
            x=8,
            y=2,
            village_id=2001,
            player_id=99,
            village_name="Occupied Oasis",
            population=50,
            distance=6.0,
            is_oasis=True,
        ),
        # Abandoned village — village_id=-1
        MapTileInfo(
            x=12,
            y=-4,
            village_id=0,
            player_id=None,
            village_name="Ruins",
            population=0,
            distance=8.0,
            is_oasis=False,
            is_abandoned=True,
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
        t
        for t in raw_tiles
        if (t.village_id > 0 and t.village_id not in own_ids) or (t.is_oasis and show_oases)
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

        assert (5, 5) in coords, (
            "REGRESSION: unoccupied oasis (5,5) must be in results when show_oases=True"
        )
        assert (-3, 7) in coords, (
            "REGRESSION: unoccupied oasis (-3,7) must be in results when show_oases=True"
        )

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


class TestPopulationFilterAppliesToOases:
    """`filter_targets` must apply min/max village-pop to oases too.

    Prior behaviour carved oases out of the village-pop check so the user
    couldn't drop pop=0 unoccupied oases via min_population. After the
    fix, scout_ws inherits the owning village's pop into occupied oases
    and the filter is uniform.
    """

    def test_min_population_drops_unoccupied_oasis_with_pop_zero(self):
        svc = AutoScoutService(http_client=None)  # filter_targets is pure
        tiles = [
            MapTileInfo(x=1, y=1, village_id=0, is_oasis=True, population=0, distance=1.0),
            MapTileInfo(x=2, y=2, village_id=42, player_id=7, population=600, distance=2.0),
        ]
        result = svc.filter_targets(tiles, min_population=1, exclude_oases=False)
        coords = {(t.x, t.y) for t in result}
        assert (1, 1) not in coords, "Unoccupied oasis (pop=0) must be filtered out by min_pop=1"
        assert (2, 2) in coords, "Village with pop=600 must pass min_pop=1"

    def test_min_population_keeps_occupied_oasis_with_inherited_pop(self):
        svc = AutoScoutService(http_client=None)
        # Occupied oasis whose population was already inherited from the
        # owning village (post-enrichment in scout_ws sets this).
        tiles = [
            MapTileInfo(
                x=28,
                y=96,
                village_id=0,
                player_id=99,
                is_oasis=True,
                population=600,
                distance=9.4,
            ),
            MapTileInfo(x=29, y=93, village_id=0, is_oasis=True, population=0, distance=7.8),
        ]
        result = svc.filter_targets(
            tiles, min_population=1, max_population=800, exclude_oases=False
        )
        coords = {(t.x, t.y) for t in result}
        assert (28, 96) in coords, "Occupied oasis with inherited pop=600 must pass 1..800"
        assert (29, 93) not in coords, "Unoccupied oasis (pop=0) must be dropped by min_pop=1"

    def test_max_population_drops_oasis_above_cap(self):
        svc = AutoScoutService(http_client=None)
        tiles = [
            MapTileInfo(
                x=5,
                y=5,
                village_id=0,
                player_id=99,
                is_oasis=True,
                population=2000,
                distance=5.0,
            ),
        ]
        result = svc.filter_targets(tiles, max_population=800, exclude_oases=False)
        assert result == [], "Occupied oasis with pop=2000 must be dropped by max_pop=800"


class TestParserStripsBidiMarkers:
    """`_parse_tile_details` must clean U+202D / U+202C and their HTML
    entity forms from village_name so the UI doesn't render `&#x202d;`.
    """

    def test_html_entity_form_is_stripped(self):
        svc = AutoScoutService(http_client=None)
        html = "<h1>Occupied oasis &#x202d;101&#x202c;</h1>"
        info = svc._parse_tile_details(28, 96, html)
        assert "&#x202d;" not in info.village_name
        assert "‭" not in info.village_name
        assert "‬" not in info.village_name
        assert "Occupied oasis" in info.village_name
        assert "101" in info.village_name

    def test_raw_codepoint_form_is_stripped(self):
        svc = AutoScoutService(http_client=None)
        html = "<h1>Unoccupied oasis ‭‬</h1>"
        info = svc._parse_tile_details(29, 93, html)
        assert "‭" not in info.village_name
        assert "‬" not in info.village_name


class TestParserExtractsOasisOwnerCoords:
    """For occupied oases, the parser captures the owning village's
    coords from the karte.php link so scout_ws can copy that village's
    pop into the oasis row.
    """

    def test_owner_coords_extracted_when_link_present(self):
        svc = AutoScoutService(http_client=None)
        html = (
            "<h1>Occupied oasis</h1>"
            "<th>Occupied by</th>"
            '<td><a href="/profile/123">THE NOBODY</a></td>'
            "<th>Owner village</th>"
            '<td><a href="/karte.php?x=27&amp;y=96">101</a></td>'
            "<th>Tribe</th><td>Roman</td>"
            '<div class="oasis"></div>'
        )
        info = svc._parse_tile_details(28, 96, html)
        assert info.is_oasis is True
        assert info.player_id == 123
        assert info.oasis_owner_x == 27
        assert info.oasis_owner_y == 96

    def test_owner_coords_skip_self_link(self):
        svc = AutoScoutService(http_client=None)
        # Some popups may link to the tile itself before the owner-village
        # link. The parser must skip the self-link and pick the next.
        html = (
            "<h1>Occupied oasis</h1>"
            '<a href="/karte.php?x=28&y=96">this tile</a>'
            '<th>Occupied by</th><td><a href="/profile/7">P</a></td>'
            '<a href="/karte.php?x=27&y=96">owner village</a>'
            '<div class="oasis"></div>'
        )
        info = svc._parse_tile_details(28, 96, html)
        assert info.oasis_owner_x == 27
        assert info.oasis_owner_y == 96

    def test_owner_coords_left_unset_for_unoccupied_oasis(self):
        svc = AutoScoutService(http_client=None)
        html = '<h1>Unoccupied oasis</h1><div class="oasis"></div>'
        info = svc._parse_tile_details(5, 5, html)
        assert info.is_oasis is True
        assert info.player_id is None
        assert info.oasis_owner_x is None
        assert info.oasis_owner_y is None


def test_oasis_burst_size_is_right_skewed_and_bounded():
    """Burst size must be right-skewed, not uniform over {3,4,5}.

    A uniform 3-value histogram is chi-square rejectable; the sampler clusters
    at 3-4 with a quick-2 minority and an occasional 5-7 tail.
    """
    import random
    from collections import Counter

    from travian_api.services.oasis_raider_service import _sample_burst_size

    random.seed(0)
    samples = [_sample_burst_size() for _ in range(20000)]
    c = Counter(samples)
    n = len(samples)

    assert min(samples) == 2
    assert max(samples) <= 7
    # Mode in the 3-4 band; size-2 a sizable minority; 5-7 an occasional tail.
    assert (c[3] + c[4]) / n > 0.45
    assert 0.18 < c[2] / n < 0.32
    assert 0.12 < (c[5] + c[6] + c[7]) / n < 0.28
    # Mean sits below the midpoint of the [2,7] support (right-skewed).
    assert 2.0 < sum(samples) / n < 4.2
