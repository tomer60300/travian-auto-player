"""Tests for the "villages by oasis bonus" Auto-Scout feature.

Covers the three novel pieces:
  * ``_extract_village_oases`` — projecting per-village occupied-oasis coords
    out of a profile page (NO bonus data lives there).
  * ``AutoScoutService.aggregate_village_oasis_bonuses`` — summing each
    village's oasis bonuses with the fewest requests (dedup + immutable
    cache), routed through the read client.
  * ``OasisBonusCache`` — present-but-empty vs miss semantics, and the
    "never cache a failed fetch" guarantee.

Network is stubbed at the ``get_tile_details`` boundary so these tests make
zero HTTP calls and assert the exact request budget via call counts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from travian_api.models.farm_list import MapTileInfo
from travian_api.services import auto_scout_service as ass
from travian_api.services.auto_scout_service import (
    AutoScoutService,
    _extract_village_oases,
)
from travian_api.services.oasis_bonus_cache import OasisBonusCache

# A profile slice mirroring the user's demo: village 34756 occupies two
# oases, village 34754 occupies none.
DEMO_PROFILE_SLICE = (
    '"villages":['
    '{"id":34756,"name":"Cap","tribeId":1,"mapId":1,"population":900,'
    '"x":5,"y":7,"occupiedOases":[{"id":1,"x":6,"y":7},{"id":2,"x":5,"y":8}],'
    '"region":null,"typeText":"(Capital)","typeTitle":""},'
    '{"id":34754,"name":"Two","tribeId":1,"mapId":2,"population":300,'
    '"x":9,"y":9,"occupiedOases":[],"region":null,"typeText":"","typeTitle":""}'
    "]"
)


def _stub_client(server: str = "https://ts2.x1.europe.travian.com") -> MagicMock:
    client = MagicMock(name="http_client")
    client.base_url = server
    client.get_html = AsyncMock(return_value="<html></html>")
    client.post_json = AsyncMock(return_value={"html": ""})
    client.navigator = MagicMock(enabled=False)
    return client


def _tile_with_bonus(breakdown: dict[str, int]) -> MapTileInfo:
    return MapTileInfo(is_oasis=True, bonus_breakdown=breakdown)


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    """Point the module singleton at a throwaway dir so tests neither hit
    nor pollute the persistent on-disk cache, and stay independent."""
    fresh = OasisBonusCache(disk_dir=tmp_path / "oasis_cache")
    monkeypatch.setattr(ass, "oasis_bonus_cache", fresh)
    return fresh


# ─────────────────────── _extract_village_oases ──────────────────────


def test_extract_village_oases_populated_and_empty() -> None:
    out = _extract_village_oases(DEMO_PROFILE_SLICE)
    by_id = {v["village_id"]: v for v in out}
    assert by_id[34756]["oases"] == [(6, 7), (5, 8)]
    assert by_id[34756]["name"] == "Cap"
    assert by_id[34756]["population"] == 900
    assert by_id[34754]["oases"] == []


def test_extract_village_oases_no_array_returns_empty() -> None:
    assert _extract_village_oases("<html>no villages here</html>") == []


# ──────────────── aggregate_village_oasis_bonuses ────────────────


@pytest.mark.asyncio
async def test_aggregate_sums_across_oases_demo() -> None:
    svc = AutoScoutService(_stub_client())
    bonuses = {
        (6, 7): {"iron": 25, "crop": 25},
        (5, 8): {"wood": 25},
    }
    svc.get_tile_details = AsyncMock(side_effect=lambda x, y: _tile_with_bonus(bonuses[(x, y)]))
    villages = [
        {"village_id": 34756, "oases": [(6, 7), (5, 8)]},
        {"village_id": 34754, "oases": []},
    ]
    result = await svc.aggregate_village_oasis_bonuses(villages)
    assert result[34756] == {"iron": 25, "crop": 25, "wood": 25}
    assert result[34754] == {}
    # One request per unique oasis; the zero-oasis village adds none.
    assert svc.get_tile_details.await_count == 2


@pytest.mark.asyncio
async def test_aggregate_dedups_shared_and_repeated_coords() -> None:
    svc = AutoScoutService(_stub_client())
    svc.get_tile_details = AsyncMock(return_value=_tile_with_bonus({"iron": 50}))
    villages = [
        # same coord twice in one village + shared with another village
        {"village_id": 1, "oases": [(2, 2), (2, 2)]},
        {"village_id": 2, "oases": [(2, 2)]},
    ]
    result = await svc.aggregate_village_oasis_bonuses(villages)
    # Per-village dedup: (2,2) counted once → iron 50, not 100.
    assert result[1] == {"iron": 50}
    assert result[2] == {"iron": 50}
    # Op-wide dedup: the single unique coord is fetched exactly once.
    assert svc.get_tile_details.await_count == 1


@pytest.mark.asyncio
async def test_aggregate_uses_cache_no_refetch(_isolate_cache) -> None:
    svc = AutoScoutService(_stub_client())
    _isolate_cache.put(svc.http_client.base_url, 6, 7, {"iron": 25, "crop": 25})
    svc.get_tile_details = AsyncMock()
    result = await svc.aggregate_village_oasis_bonuses([{"village_id": 9, "oases": [(6, 7)]}])
    assert result[9] == {"iron": 25, "crop": 25}
    svc.get_tile_details.assert_not_awaited()


@pytest.mark.asyncio
async def test_aggregate_failed_fetch_not_cached(_isolate_cache) -> None:
    svc = AutoScoutService(_stub_client())
    svc.get_tile_details = AsyncMock(side_effect=RuntimeError("network"))
    result = await svc.aggregate_village_oasis_bonuses([{"village_id": 9, "oases": [(6, 7)]}])
    # Failure contributes nothing and must NOT poison the cache.
    assert result[9] == {}
    assert _isolate_cache.get(svc.http_client.base_url, 6, 7) is None


# ─────────────────────── OasisBonusCache ─────────────────────────


def test_cache_miss_vs_present_empty(tmp_path) -> None:
    cache = OasisBonusCache(disk_dir=tmp_path / "c")
    server = "https://s"
    assert cache.get(server, 1, 1) is None  # true miss
    cache.put(server, 1, 1, {})  # genuine empty parse
    assert cache.get(server, 1, 1) == {}  # hit, not None


def test_cache_server_scoped(tmp_path) -> None:
    cache = OasisBonusCache(disk_dir=tmp_path / "c")
    cache.put("https://a", 1, 1, {"iron": 25})
    assert cache.get("https://b", 1, 1) is None


# ─────────────── profile-info piggybacks villages ───────────────


@pytest.mark.asyncio
async def test_profile_info_includes_villages_no_extra_request() -> None:
    client = _stub_client()
    client.get_html = AsyncMock(return_value=DEMO_PROFILE_SLICE)
    svc = AutoScoutService(client)
    info = await svc.get_player_profile_info(player_id=8639)
    assert client.get_html.await_count == 1  # single profile fetch
    vids = {v["village_id"] for v in info["villages"]}
    assert vids == {34756, 34754}
