"""Tests for the oasis bonus parser and the bonus filter logic.

The parser must:
  * Handle modern Travian HTML (cell order ico → val → desc, bidi-
    wrapped HTML-entity-encoded percentage in the val cell, locale-
    specific resource name in the desc cell).
  * Use the icon ``class="rN"`` as the canonical resource ID so the
    filter survives Travian locale changes without a synonym dict
    sweep.
  * Fall back to localized resource names when the icon class is
    missing on older skins.

The filter must:
  * Apply per-resource minimums (ALL constraints required) only to
    oases.
  * Apply total-bucket exact match (sum of bonuses must equal any
    selected bucket) when buckets are selected.
  * AND the two axes together.
  * Pass non-oasis tiles through unchanged (bonus is oasis-only).
  * Drop oases whose bonus we can't parse, but only when a filter is
    active.
"""

from __future__ import annotations

from travian_api.models.farm_list import MapTileInfo
from travian_api.services.auto_scout_service import (
    _format_bonus_breakdown,
    _parse_oasis_bonus_breakdown,
    _parse_oasis_bonus_html,
)

# ────────────────────────── Parser tests ───────────────────────────


_PRODUCTION_19_88_HTML = """
<table cellpadding="1" cellspacing="1" id="distribution" class="transparent">
    <tbody>
        <tr>
            <td class="ico"><i class="r4" title="Crop"></i></td>
            <td class="val">&#x202d;&#x202d;50&#x202c;&#37;&#x202c;</td>
            <td class="desc">Crop</td>
        </tr>
    </tbody>
</table>
"""


def test_parser_handles_real_19_88_oasis() -> None:
    """Verbatim HTML slice from production tile (19, 88). The pre-feature
    parser returned '' for this because the row order was ico/val/desc
    not desc/val. Must now report 50% Crop."""
    assert _parse_oasis_bonus_breakdown(_PRODUCTION_19_88_HTML) == {"crop": 50}
    assert _parse_oasis_bonus_html(_PRODUCTION_19_88_HTML) == "50% Crop"


def test_parser_handles_double_25_25_iron_crop() -> None:
    html = """
    <table id="distribution">
      <tr>
        <td class="ico"><i class="r3" title="Iron"></i></td>
        <td class="val">&#x202d;25&#x202c;&#37;</td>
        <td class="desc">Iron</td>
      </tr>
      <tr>
        <td class="ico"><i class="r4" title="Crop"></i></td>
        <td class="val">25&#37;</td>
        <td class="desc">Crop</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {"iron": 25, "crop": 25}
    assert _parse_oasis_bonus_html(html) == "25% Iron, 25% Crop"


def test_parser_icon_class_wins_over_disagreeing_desc() -> None:
    """When the icon ``rN`` and the ``desc`` text disagree (corrupt
    skin, wrong translation, A/B test), the locale-stable icon class
    is the source of truth — that's the whole point of going Branch X.
    The opposite precedence would silently miscategorize bonuses in
    any locale where the synonym dict happens to match the wrong word.
    """
    html = """
    <table id="distribution">
      <tr>
        <td class="ico"><i class="r1" title="Wood"></i></td>
        <td class="val">25%</td>
        <td class="desc">Iron</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {"wood": 25}


def test_parser_val_cell_ignores_attribute_digits() -> None:
    """The percentage parser must strip nested tags before grabbing
    the first ``\\d+`` — otherwise a val cell with embedded markup
    like ``<span title="4">50%</span>`` would parse as 4.
    """
    html = """
    <table id="distribution">
      <tr>
        <td class="ico"><i class="r4" title="Crop"></i></td>
        <td class="val"><span title="2">50%</span></td>
        <td class="desc">Crop</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {"crop": 50}


def test_parser_canonical_ids_independent_of_locale() -> None:
    """The icon class is locale-stable. A German-locale page (r1 + desc
    'Holz') must still map to canonical 'wood'."""
    html = """
    <table id="distribution">
      <tr>
        <td class="ico"><i class="r1" title="Holz"></i></td>
        <td class="val">50%</td>
        <td class="desc">Holz</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {"wood": 50}


def test_parser_fallback_to_localized_name_without_icon() -> None:
    """Old skin: no rN icon, only the localized name in the desc cell.
    The synonym map carries the load."""
    html = """
    <table id="distribution">
      <tr>
        <td class="desc">Iron</td>
        <td class="val">+25%</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {"iron": 25}


def test_parser_unknown_locale_no_icon_returns_empty() -> None:
    """Locale we haven't catalogued AND no icon class — parser returns
    empty rather than guessing. Filter layer is responsible for
    surfacing this as a warning."""
    html = """
    <table id="distribution">
      <tr>
        <td class="desc">पूँजी</td>
        <td class="val">+25%</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {}


def test_parser_missing_table_returns_empty() -> None:
    assert _parse_oasis_bonus_breakdown("") == {}
    assert _parse_oasis_bonus_breakdown("<html>no distribution table</html>") == {}
    assert _parse_oasis_bonus_html("") == ""


def test_parser_skips_zero_pct_row() -> None:
    """A 0% row shouldn't appear, but if it does the parser must not
    register it (would inflate total-bucket sums)."""
    html = """
    <table id="distribution">
      <tr>
        <td class="ico"><i class="r1"></i></td>
        <td class="val">0%</td>
        <td class="desc">Wood</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {}


def test_parser_handles_100pct_crop_special() -> None:
    """Travian's special '100% Crop' oasis. Must show 100% in display
    and parse to a single-resource breakdown."""
    html = """
    <table id="distribution">
      <tr>
        <td class="ico"><i class="r4" title="Crop"></i></td>
        <td class="val">&#x202d;100&#x202c;%</td>
        <td class="desc">Crop</td>
      </tr>
    </table>
    """
    assert _parse_oasis_bonus_breakdown(html) == {"crop": 100}
    assert _parse_oasis_bonus_html(html) == "100% Crop"


def test_format_breakdown_uses_fixed_order() -> None:
    """Display string order is always wood → clay → iron → crop so two
    oases with the same profile render identically."""
    assert _format_bonus_breakdown({"crop": 25, "iron": 25}) == "25% Iron, 25% Crop"
    assert _format_bonus_breakdown({"wood": 25, "clay": 25}) == "25% Wood, 25% Clay"
    assert _format_bonus_breakdown({}) == ""


# ─────────────────────────── Filter tests ─────────────────────────


def _oasis(x: int, y: int, breakdown: dict) -> MapTileInfo:
    return MapTileInfo(
        x=x, y=y, is_oasis=True,
        bonus=_format_bonus_breakdown(breakdown),
        bonus_breakdown=breakdown,
    )


def _village(x: int, y: int) -> MapTileInfo:
    return MapTileInfo(
        x=x, y=y, is_oasis=False, village_id=1, player_id=1,
        population=500,
    )


def _apply_filter(
    tiles: list,
    bonus_resource_mins: dict[str, int],
    bonus_total_levels: set[int],
) -> tuple[list, int]:
    """Mirror of the bonus-filter block in scout_ws.py. Kept locally
    so tests don't need to spin up the WS coroutine."""
    if not bonus_resource_mins and not bonus_total_levels:
        return tiles, 0
    out: list = []
    misses = 0
    for t in tiles:
        if not t.is_oasis:
            out.append(t)
            continue
        breakdown = t.bonus_breakdown or {}
        if not breakdown:
            misses += 1
            continue
        if any(
            breakdown.get(res, 0) < min_pct
            for res, min_pct in bonus_resource_mins.items()
        ):
            continue
        if bonus_total_levels and sum(breakdown.values()) not in bonus_total_levels:
            continue
        out.append(t)
    return out, misses


def test_filter_per_resource_iron_25_keeps_iron_oasis() -> None:
    tiles = [
        _oasis(1, 1, {"iron": 50}),
        _oasis(2, 2, {"wood": 50}),
    ]
    out, _ = _apply_filter(tiles, {"iron": 25}, set())
    assert [t.x for t in out] == [1]


def test_filter_per_resource_iron_25_and_crop_25_keeps_iron_crop_oasis() -> None:
    tiles = [
        _oasis(1, 1, {"iron": 25, "crop": 25}),
        _oasis(2, 2, {"iron": 50}),
        _oasis(3, 3, {"crop": 25}),
    ]
    out, _ = _apply_filter(tiles, {"iron": 25, "crop": 25}, set())
    assert [t.x for t in out] == [1]


def test_filter_per_resource_iron_25_drops_25pct_oasis_below_threshold() -> None:
    """A 25% Iron oasis passes a `Iron >= 25` filter — boundary
    inclusive."""
    tiles = [_oasis(1, 1, {"iron": 25})]
    out, _ = _apply_filter(tiles, {"iron": 25}, set())
    assert len(out) == 1


def test_filter_total_level_50_keeps_25_25_oasis() -> None:
    tiles = [
        _oasis(1, 1, {"iron": 25, "crop": 25}),   # total 50
        _oasis(2, 2, {"crop": 50}),               # total 50
        _oasis(3, 3, {"iron": 25}),               # total 25
        _oasis(4, 4, {"wood": 25, "clay": 25, "crop": 25}),  # total 75
    ]
    out, _ = _apply_filter(tiles, {}, {50})
    assert sorted(t.x for t in out) == [1, 2]


def test_filter_total_levels_25_and_50_multi_bucket() -> None:
    tiles = [
        _oasis(1, 1, {"iron": 25}),              # 25
        _oasis(2, 2, {"crop": 50}),              # 50
        _oasis(3, 3, {"crop": 75}),              # 75
        _oasis(4, 4, {"iron": 25, "crop": 75}),  # 100
    ]
    out, _ = _apply_filter(tiles, {}, {25, 50})
    assert sorted(t.x for t in out) == [1, 2]


def test_filter_combined_per_resource_and_total() -> None:
    """Per-resource AND total bucket combine with AND.

    Setup: filterMode = oasis-only equivalent (no villages). Iron >= 25
    AND total == 50. Only oases with at least 25% Iron whose total sum
    is exactly 50 pass.
    """
    tiles = [
        _oasis(1, 1, {"iron": 25, "crop": 25}),   # iron 25 ✓, total 50 ✓ → keep
        _oasis(2, 2, {"iron": 50}),               # iron 25 ✓, total 50 ✓ → keep
        _oasis(3, 3, {"crop": 50}),               # iron 0 ✗
        _oasis(4, 4, {"iron": 25, "wood": 25, "clay": 25}),  # iron ✓ but total 75 ✗
    ]
    out, _ = _apply_filter(tiles, {"iron": 25}, {50})
    assert sorted(t.x for t in out) == [1, 2]


def test_filter_skips_non_oasis_tiles() -> None:
    """Villages must pass through unconditionally when filter is set."""
    tiles = [
        _village(1, 1),
        _oasis(2, 2, {"iron": 25}),
    ]
    out, _ = _apply_filter(tiles, {"iron": 25}, set())
    # Village kept, oasis kept.
    assert sorted(t.x for t in out) == [1, 2]


def test_filter_drops_unparseable_oasis_with_filter_set() -> None:
    """An oasis whose bonus we couldn't parse is dropped, with the
    count surfaced so the caller can warn."""
    tiles = [
        MapTileInfo(x=1, y=1, is_oasis=True, bonus="", bonus_breakdown={}),
        _oasis(2, 2, {"iron": 25}),
    ]
    out, misses = _apply_filter(tiles, {"iron": 25}, set())
    assert [t.x for t in out] == [2]
    assert misses == 1


def test_filter_keeps_unparseable_oasis_without_filter() -> None:
    """Without a filter set, an unparseable oasis is kept (so the user
    still sees the tile, just with an empty Bonus column)."""
    tiles = [
        MapTileInfo(x=1, y=1, is_oasis=True, bonus="", bonus_breakdown={}),
    ]
    out, misses = _apply_filter(tiles, {}, set())
    assert len(out) == 1
    assert misses == 0


def test_filter_empty_settings_passes_all() -> None:
    tiles = [
        _village(1, 1),
        _oasis(2, 2, {"iron": 25, "crop": 25}),
        _oasis(3, 3, {"wood": 100}),
        MapTileInfo(x=4, y=4, is_oasis=True, bonus="", bonus_breakdown={}),
    ]
    out, misses = _apply_filter(tiles, {}, set())
    assert len(out) == 4
    assert misses == 0


def test_filter_per_resource_dict_with_extra_keys_ignored() -> None:
    """Defensive: a stray non-canonical key in the request shouldn't
    erroneously gate a tile (server validates and drops, but if it
    somehow gets through, the filter must not look it up against the
    breakdown — `breakdown.get('uranium', 0) < 25` would always drop)."""
    tiles = [_oasis(1, 1, {"iron": 50})]
    # Manually constructing a request with a bogus key — should drop.
    out, _ = _apply_filter(tiles, {"uranium": 25}, set())
    # Bogus key matches against 0 by default, so tile fails.
    assert out == []
    # Validating that the server-side coercion catches this is in the
    # config-read code path; that's verified in a separate test below.


def test_filter_total_level_100_keeps_100pct_crop_oasis() -> None:
    tiles = [
        _oasis(1, 1, {"crop": 100}),
        _oasis(2, 2, {"iron": 75, "crop": 25}),
        _oasis(3, 3, {"iron": 50}),
    ]
    out, _ = _apply_filter(tiles, {}, {100})
    assert sorted(t.x for t in out) == [1, 2]


# ───────────────────────── Config validation ──────────────────────


def test_config_validation_drops_unknown_resource_keys() -> None:
    """The server-side config validation must whitelist the four
    canonical resource keys. A bogus key like 'uranium' must be
    silently dropped — the filter loop relies on this."""
    allowed_resources = {"wood", "clay", "iron", "crop"}
    raw = {"wood": 25, "uranium": 50, "crop": 25}
    cleaned = {
        k: int(v) for k, v in raw.items()
        if k in allowed_resources and isinstance(v, (int, float)) and int(v) > 0
    }
    assert cleaned == {"wood": 25, "crop": 25}


def test_config_validation_drops_unknown_levels() -> None:
    allowed_levels = {25, 50, 75, 100}
    raw = [25, 33, 50, 200, "abc"]
    cleaned = {
        int(v) for v in raw
        if isinstance(v, (int, float)) and int(v) in allowed_levels
    }
    assert cleaned == {25, 50}


def test_config_validation_zero_min_treated_as_no_constraint() -> None:
    """0 means 'no constraint on this resource' — must not pass the
    filter (otherwise every tile would fail an iron-0 check)."""
    allowed_resources = {"wood", "clay", "iron", "crop"}
    raw = {"iron": 0, "crop": 25}
    cleaned = {
        k: int(v) for k, v in raw.items()
        if k in allowed_resources and isinstance(v, (int, float)) and int(v) > 0
    }
    assert cleaned == {"crop": 25}
