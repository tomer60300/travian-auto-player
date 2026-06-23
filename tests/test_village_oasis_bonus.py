"""Tests for the "villages by oasis bonus" Auto-Scout feature.

The oasis bonus is embedded directly in the player profile's
``occupiedOases`` array (``bonus`` -> ``amount`` + ``resourceType``), so the
feature needs ZERO extra requests beyond the profile fetch the scan already
makes. These tests cover:
  * ``_extract_village_oases`` — aggregating each village's occupied-oasis
    bonus straight from the real profile JSON shape.
  * ``get_player_profile_info`` — one fetch yields capital id AND the
    per-village oasis breakdown.
  * The combined non-capital + village-oasis-bonus filter composition.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from travian_api.models.farm_list import MapTileInfo
from travian_api.services.auto_scout_service import (
    AutoScoutService,
    _coerce_int,
    _extract_village_oases,
)

# Verbatim shape of the real /profile/8639 villages array (ts2 europe):
# village 49948 occupies two oases (iron25+crop25, and lumber25 -> wood);
# village 69062 occupies none. resourceType.id: 1=wood/lumber, 3=iron, 4=crop.
DEMO_PROFILE_SLICE = (
    '"villages":['
    '{"id":49948,"name":"Dupcie Wielkie","tribeId":3,"mapId":1,"population":821,'
    '"victoryPoints":null,"victoryPointsPerDay":null,"x":69,"y":114,'
    '"occupiedOases":['
    '{"bonus":[{"amount":25,"resourceType":{"id":3,"code":"iron"}},'
    '{"amount":25,"resourceType":{"id":4,"code":"crop"}}]},'
    '{"bonus":[{"amount":25,"resourceType":{"id":1,"code":"lumber"}}]}],'
    '"region":null,"typeText":"(Capital)","typeTitle":""},'
    '{"id":69062,"name":"Dupeczki","tribeId":3,"mapId":2,"population":590,'
    '"x":67,"y":114,"occupiedOases":[],"region":null,"typeText":"","typeTitle":""}'
    "]"
)


def _stub_client(server: str = "https://ts2.x1.europe.travian.com") -> MagicMock:
    client = MagicMock(name="http_client")
    client.base_url = server
    client.get_html = AsyncMock(return_value="<html></html>")
    client.post_json = AsyncMock(return_value={"html": ""})
    client.navigator = MagicMock(enabled=False)
    return client


# ─────────────────────── _extract_village_oases ──────────────────────


def test_extract_village_oases_aggregates_bonus_from_profile() -> None:
    by_id = {v["village_id"]: v for v in _extract_village_oases(DEMO_PROFILE_SLICE)}
    cap = by_id[49948]
    # iron25 + crop25 (oasis 1) + wood25 (oasis 2) = 75% across 2 oases.
    assert cap["breakdown"] == {"iron": 25, "crop": 25, "wood": 25}
    assert sum(cap["breakdown"].values()) == 75
    assert cap["oasis_count"] == 2
    assert cap["name"] == "Dupcie Wielkie"
    assert (cap["x"], cap["y"]) == (69, 114)
    # Village with no oases -> empty breakdown, zero count.
    assert by_id[69062]["breakdown"] == {}
    assert by_id[69062]["oasis_count"] == 0


def test_extract_resource_code_fallback_when_id_missing() -> None:
    """If resourceType has no numeric id, fall back to the textual code."""
    html = (
        '"villages":[{"id":7,"name":"X","x":1,"y":2,"typeText":"",'
        '"occupiedOases":[{"bonus":[{"amount":50,"resourceType":{"code":"clay"}}]}]}]'
    )
    v = _extract_village_oases(html)[0]
    assert v["breakdown"] == {"clay": 50}
    assert v["oasis_count"] == 1


def test_extract_village_oases_no_array_returns_empty() -> None:
    assert _extract_village_oases("<html>no villages here</html>") == []


def test_coerce_int_never_crashes_on_garbage() -> None:
    # Honors its "returns 0 on anything unparseable" contract — no ValueError.
    assert _coerce_int("--5") == 0
    assert _coerce_int("-") == 0
    assert _coerce_int("abc") == 0
    assert _coerce_int("-5") == -5
    assert _coerce_int(7) == 7
    assert _coerce_int(True) == 0


# ─────────────── profile-info: one fetch -> capital + oases ──────────


@pytest.mark.asyncio
async def test_profile_info_includes_villages_no_extra_request() -> None:
    client = _stub_client()
    client.get_html = AsyncMock(return_value=DEMO_PROFILE_SLICE)
    svc = AutoScoutService(client)
    info = await svc.get_player_profile_info(player_id=8639)
    assert client.get_html.await_count == 1  # single profile fetch
    assert {v["village_id"] for v in info["villages"]} == {49948, 69062}


@pytest.mark.asyncio
async def test_profile_info_yields_both_capital_and_oasis_bonus_from_one_fetch() -> None:
    """The combined non-capital + oasis-bonus search relies on ONE
    /profile/{id} GET answering BOTH questions — capital id and each
    village's oasis bonus — with zero extra requests (no tile-details)."""
    client = _stub_client()
    client.get_html = AsyncMock(return_value=DEMO_PROFILE_SLICE)
    svc = AutoScoutService(client)
    info = await svc.get_player_profile_info(player_id=8639)
    assert client.get_html.await_count == 1
    client.post_json.assert_not_called()  # NO tile-details fetches at all
    assert info["capital_id"] == 49948
    cap = next(v for v in info["villages"] if v["village_id"] == 49948)
    assert sum(cap["breakdown"].values()) == 75


# ───────── combined non-capital + village-oasis-bonus filter ─────────


def _apply_combined_filters(
    tiles: list[MapTileInfo],
    *,
    non_capitals: bool,
    bonus_total_levels: set[int],
) -> list[MapTileInfo]:
    """Mirror of the two independent scout_ws post-filter blocks that run
    when a scan combines "Exclude capital villages" with the village
    oasis-bonus filter. Total uses MINIMUM (>=) semantics."""
    out = tiles
    if non_capitals:
        out = [t for t in out if not t.is_capital]
    if bonus_total_levels:
        min_total = min(bonus_total_levels)
        kept = []
        for t in out:
            if t.is_oasis or t.village_oasis_count == 0:
                continue
            breakdown = t.village_oasis_breakdown or {}
            if not breakdown or sum(breakdown.values()) < min_total:
                continue
            kept.append(t)
        out = kept
    return out


def _village_tile(x: int, *, is_capital: bool, breakdown: dict[str, int]) -> MapTileInfo:
    return MapTileInfo(
        x=x,
        y=0,
        is_oasis=False,
        village_id=x,
        player_id=1,
        is_capital=is_capital,
        village_oasis_breakdown=breakdown,
        village_oasis_count=len(breakdown),
    )


def test_combined_keeps_only_noncapital_above_min_total() -> None:
    tiles = [
        _village_tile(
            1, is_capital=False, breakdown={"iron": 25, "crop": 25, "wood": 25}
        ),  # 75 keep
        _village_tile(
            2, is_capital=True, breakdown={"iron": 50, "crop": 50}
        ),  # 100 but capital -> drop
        _village_tile(3, is_capital=False, breakdown={"wood": 25}),  # 25 < 50 -> drop
    ]
    out = _apply_combined_filters(tiles, non_capitals=True, bonus_total_levels={50})
    assert [t.x for t in out] == [1]


def test_combined_without_capital_exclusion_keeps_capital() -> None:
    tiles = [
        _village_tile(1, is_capital=False, breakdown={"iron": 25, "crop": 25, "wood": 25}),  # 75
        _village_tile(2, is_capital=True, breakdown={"iron": 50, "crop": 50}),  # 100
    ]
    out = _apply_combined_filters(tiles, non_capitals=False, bonus_total_levels={50})
    assert sorted(t.x for t in out) == [1, 2]
