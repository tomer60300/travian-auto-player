"""Tests for the map-scan center-grid algorithm.

The bug we're guarding against: with radius <= 15 the old algorithm
placed the first (and only) scan center at ``(cx - radius, cy -
radius)``, not at ``(cx, cy)``. Each ``/api/v1/map/position`` call
returns a 31x31 region CENTERED on the requested point — so a
center at (cx - radius, cy - radius) covered the bottom-left
quadrant only. Tiles north or east of (cx, cy) within the actual
requested radius were silently outside the fetched window.

Real-world repro: center (42, 17), radius 13. Tile (17, 96) sits
at distance 10 (well inside r=13) but at y=96 — outside the
fetched window [60, 90] when the only scan center was (10, 75).
"""

from __future__ import annotations

import math
import pathlib
import re


def _build_scan_centers(cx: int, cy: int, radius: int) -> list[tuple[int, int]]:
    """Reproduces the new algorithm from scout_ws.py / auto_scout_service.py
    inline so tests don't depend on importing the WS layer."""
    HALF = 15
    STRIDE = 30
    extras = max(0, (radius - HALF + STRIDE - 1) // STRIDE)
    out: list[tuple[int, int]] = []
    for dx in range(-extras, extras + 1):
        for dy in range(-extras, extras + 1):
            out.append((cx + dx * STRIDE, cy + dy * STRIDE))
    return out


def _coverage_includes(centers: list[tuple[int, int]], x: int, y: int) -> bool:
    """A tile (x, y) is covered iff at least one center's 31x31
    region (center ± 15) contains it."""
    return any(
        abs(cx - x) <= 15 and abs(cy - y) <= 15 for cx, cy in centers
    )


def test_small_radius_centers_on_requested_point() -> None:
    """The user's actual repro: radius=13 around (42, 17). The fix
    must put the (sole) scan center AT (42, 17), not at (10, 75)."""
    centers = _build_scan_centers(23, 88, 13)
    assert centers == [(42, 17)]


def test_small_radius_covers_north_corner_of_radius() -> None:
    """(17, 96) is at distance 10 from (42, 17) — well inside r=13.
    The previous algorithm fetched only (10, 75)'s region, which
    covered y up to 90 only — (17, 96) was outside the window."""
    centers = _build_scan_centers(23, 88, 13)
    assert _coverage_includes(centers, 17, 96), (
        f"(17, 96) should be in fetched region for r=13 around "
        f"(42, 17); centers were {centers}"
    )


def test_small_radius_covers_every_tile_inside_circle() -> None:
    """Every tile within euclidean radius must fall inside at least
    one scan center's 31x31 window. Brute-force check."""
    cx, cy, r = 23, 88, 13
    centers = _build_scan_centers(cx, cy, r)
    missing = []
    for x in range(cx - r, cx + r + 1):
        for y in range(cy - r, cy + r + 1):
            if math.hypot(x - cx, y - cy) <= r:
                if not _coverage_includes(centers, x, y):
                    missing.append((x, y))
    assert not missing, (
        f"Tiles inside r=13 not covered by any scan center: {missing[:5]}..."
    )


def test_radius_15_still_one_call() -> None:
    """Radius exactly 15 is the boundary — one fetch at (cx, cy)
    covers the entire 31x31 around the center."""
    centers = _build_scan_centers(0, 0, 15)
    assert centers == [(0, 0)]


def test_radius_16_grows_to_3x3_grid() -> None:
    """Just past the boundary — needs 3x3 ring to cover the new
    edge tiles."""
    centers = _build_scan_centers(0, 0, 16)
    assert len(centers) == 9
    assert (0, 0) in centers
    assert (-30, 0) in centers
    assert (30, 0) in centers
    assert (0, -30) in centers
    assert (0, 30) in centers


def test_radius_30_covers_full_area() -> None:
    """Sanity: radius 30 must cover every tile in the 61x61 box."""
    centers = _build_scan_centers(100, 100, 30)
    for x in range(70, 131):
        for y in range(70, 131):
            if math.hypot(x - 100, y - 100) <= 30:
                assert _coverage_includes(centers, x, y), (
                    f"({x}, {y}) inside r=30 not covered"
                )


def test_radius_60_still_covered_no_gaps() -> None:
    """Larger radius — 5x5 grid; brute-force every tile inside the
    circle and assert coverage."""
    cx, cy, r = 50, 50, 60
    centers = _build_scan_centers(cx, cy, r)
    for x in range(cx - r, cx + r + 1):
        for y in range(cy - r, cy + r + 1):
            if math.hypot(x - cx, y - cy) <= r:
                assert _coverage_includes(centers, x, y), (
                    f"({x}, {y}) inside r={r} not covered. centers={centers}"
                )


def test_source_uses_new_algorithm() -> None:
    """Structural guard: both call sites (scout_ws.py and
    auto_scout_service.py) must use the new HALF/STRIDE pattern.
    If anyone re-introduces the buggy `range(cx - radius, ...)`
    iteration, this test fails."""
    targets = [
        pathlib.Path("src/travian_api/web/ws/scout_ws.py"),
        pathlib.Path("src/travian_api/services/auto_scout_service.py"),
    ]
    bad_pattern = re.compile(
        r"range\([^,]*-\s*radius[^,]*,[^,]*\+\s*radius[^,]*\)",
    )
    for path in targets:
        src = path.read_text(encoding="utf-8")
        m = bad_pattern.search(src)
        assert m is None, (
            f"Old buggy scan-center range found in {path}: {m.group(0)!r}"
        )
        # Positive assertion: the new pattern exists.
        assert "HALF = 15" in src, (
            f"Expected new HALF/STRIDE pattern in {path}; old code may "
            f"still be in place."
        )
        assert "STRIDE = 30" in src
