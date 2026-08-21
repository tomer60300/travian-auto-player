"""The defense scan must not do SQLite work on the event loop.

``TieredDefenseCache`` falls through a 5-minute in-memory L1 to a diskcache
(SQLite) L2. ``/api/farm/defense-scan`` used to walk that fall-through once per
farm-list slot -- up to 100 sequential queries before the scan's first network
await, plus a second lookup of every hit and another read inside every ``put``
for the adaptive TTL. This app exists to imitate human request timing, so a
blocked loop delays every other operation's stealth-timed request.

These tests pin the two guarantees: the L2 work happens in a worker thread, and
the route reads each slot exactly once.
"""

import asyncio
import json
import time
from types import SimpleNamespace

from travian_api.models.farm_list import FarmList, FarmListSlot
from travian_api.services.defense_cache import TieredDefenseCache
from travian_api.web.routes import farm as farm_routes
from travian_api.web.routes.farm import DefenseInfoRequest, scan_defense_strength

SCOPE = "https://ts20.x2.europe.travian.com|Attacker"
OTHER_SCOPE = "https://ts6.x1.america.travian.com|OtherPlayer"
DEFENSE = {"defender_troops": {"t1": 40}, "defender_total": 40, "defender_combat_strength": 900}


async def _ticker(counter: list[int]) -> None:
    """Counts how many times the loop got to run."""
    while True:
        await asyncio.sleep(0)
        counter.append(1)


async def _ran_while(coro_factory) -> tuple[object, int]:
    """Await *coro_factory* and report how often the loop ran meanwhile."""
    counter: list[int] = []
    task = asyncio.ensure_future(_ticker(counter))
    await asyncio.sleep(0)
    ticks_before = len(counter)
    result = await coro_factory()
    ticks = len(counter) - ticks_before
    task.cancel()
    return result, ticks


class TestBatchedLookupMatchesTheSingleLookup:
    async def test_hits_misses_and_stale_entries_agree_with_get(self, tmp_path):
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE, 12, 120, last_raid_time=1_000, defense_data=DEFENSE)
        cache.put(SCOPE, 13, 121, last_raid_time=2_000, defense_data=DEFENSE)
        cache._l1.clear()  # restart or >5min idle: L2 survives, L1 does not

        batch = await cache.get_many(
            SCOPE,
            [
                (12, 120, 1_000),  # hit
                (13, 121, 9_999),  # raided since the entry was written -> stale
                (14, 122, 3_000),  # never scanned
            ],
        )

        assert batch[(12, 120, 1_000)] is not None
        assert batch[(12, 120, 1_000)]["defender_total"] == 40
        assert batch[(13, 121, 9_999)] is None
        assert batch[(14, 122, 3_000)] is None

        # A stale entry is dropped from L2 by either path.
        assert cache.get(SCOPE, 13, 121, 2_000) is None

    async def test_a_hit_is_promoted_into_l1(self, tmp_path):
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE, 12, 120, last_raid_time=1_000, defense_data=DEFENSE)
        cache._l1.clear()

        await cache.get_many(SCOPE, [(12, 120, 1_000)])

        assert cache._l1.get(f"defense:{SCOPE}:12:120") is not None
        assert cache.get_stats()["l2_hits"] == 1

    async def test_repeated_targets_are_read_once(self, tmp_path):
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE, 12, 120, last_raid_time=1_000, defense_data=DEFENSE)
        cache._l1.clear()

        batch = await cache.get_many(SCOPE, [(12, 120, 1_000)] * 5)

        assert len(batch) == 1
        assert cache.get_stats()["l2_hits"] == 1

    async def test_another_account_never_sees_the_entry(self, tmp_path):
        """Same guarantee as the single-lookup path: keys carry the scope."""
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE, 12, 120, last_raid_time=1_000, defense_data=DEFENSE)
        cache._l1.clear()

        batch = await cache.get_many(OTHER_SCOPE, [(12, 120, 1_000)])

        assert batch[(12, 120, 1_000)] is None

    async def test_put_many_stores_what_put_stores(self, tmp_path):
        one = TieredDefenseCache(disk_dir=tmp_path / "sync")
        two = TieredDefenseCache(disk_dir=tmp_path / "batch")

        one.put(SCOPE, 12, 120, 1_000, DEFENSE)
        one.put(SCOPE, 12, 120, 1_000, DEFENSE)
        await two.put_many(SCOPE, [(12, 120, 1_000)], DEFENSE)
        await two.put_many(SCOPE, [(12, 120, 1_000)], DEFENSE)

        synchronous = one.get(SCOPE, 12, 120, 1_000)
        batched = await two.get_many(SCOPE, [(12, 120, 1_000)])
        entry = batched[(12, 120, 1_000)]

        assert entry is not None
        assert entry["check_count"] == synchronous["check_count"] == 2
        assert entry["change_count"] == synchronous["change_count"] == 0
        assert entry["defender_total"] == synchronous["defender_total"]

    async def test_put_many_writes_every_target_in_the_group(self, tmp_path):
        cache = TieredDefenseCache(disk_dir=tmp_path)

        await cache.put_many(SCOPE, [(12, 120, 1_000), (13, 121, 2_000)], DEFENSE)
        cache._l1.clear()

        batch = await cache.get_many(SCOPE, [(12, 120, 1_000), (13, 121, 2_000)])
        assert all(entry is not None for entry in batch.values())


class TestTheLoopKeepsRunning:
    async def test_get_many_reads_l2_in_a_worker_thread(self, tmp_path, monkeypatch):
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE, 12, 120, last_raid_time=1_000, defense_data=DEFENSE)
        cache._l1.clear()

        real = cache._l2_get_batch

        def slow_l2(scope, targets):
            time.sleep(0.05)  # stands in for 100 SQLite queries
            return real(scope, targets)

        monkeypatch.setattr(cache, "_l2_get_batch", slow_l2)

        batch, ticks = await _ran_while(lambda: cache.get_many(SCOPE, [(12, 120, 1_000)]))

        assert batch[(12, 120, 1_000)] is not None
        assert ticks > 1, "the L2 read blocked the event loop"

    async def test_put_many_writes_l2_in_a_worker_thread(self, tmp_path, monkeypatch):
        cache = TieredDefenseCache(disk_dir=tmp_path)
        real = cache._l2_put_batch

        def slow_l2(scope, targets, defense_data):
            time.sleep(0.05)
            return real(scope, targets, defense_data)

        monkeypatch.setattr(cache, "_l2_put_batch", slow_l2)

        _, ticks = await _ran_while(lambda: cache.put_many(SCOPE, [(12, 120, 1_000)], DEFENSE))

        assert ticks > 1, "the L2 write blocked the event loop"
        assert cache.get(SCOPE, 12, 120, 1_000) is not None


def _slot(slot_id: int, x: int, y: int, last_raid: int | None) -> FarmListSlot:
    return FarmListSlot.model_validate(
        {
            "id": slot_id,
            "target": {"x": x, "y": y, "name": f"V{slot_id}"},
            "lastRaid": None if last_raid is None else {"time": last_raid},
        }
    )


class _Cache:
    """Stands in for the singleton: records lookups, serves a fixed L2."""

    def __init__(self, entries: dict[tuple[int, int, int], dict]) -> None:
        self.entries = entries
        self.batches: list[list[tuple[int, int, int]]] = []
        self.stored: list[list[tuple[int, int, int]]] = []

    async def get_many(self, scope, targets):
        self.batches.append(list(targets))
        return {t: self.entries.get(t) for t in targets}

    def get(self, *args, **kwargs):
        raise AssertionError("the scan must not read the cache slot-by-slot on the loop")

    def put(self, *args, **kwargs):
        raise AssertionError("the scan must not write the cache slot-by-slot on the loop")

    async def put_many(self, scope, targets, defense_data):
        self.stored.append(list(targets))

    def get_inflight(self, scope, x, y):
        return None

    def set_inflight(self, scope, x, y):
        return asyncio.get_event_loop().create_future()

    def clear_inflight(self, scope, x, y):
        return None


async def _scan(monkeypatch, slots, entries, fetched=None):
    cache = _Cache(entries)
    monkeypatch.setattr(farm_routes, "defense_cache", cache)

    async def fake_fetch(session, x, y):
        return fetched

    monkeypatch.setattr(farm_routes, "_fetch_defense_for_coord", fake_fetch)

    session = SimpleNamespace(
        server_url="https://ts20.x2.europe.travian.com/",
        player_name="Attacker",
        farm_service=SimpleNamespace(get_farm_list=_farm_list(slots)),
    )
    response = await scan_defense_strength(
        DefenseInfoRequest(list_id=7), session=session, user=SimpleNamespace(id=1)
    )
    lines = [json.loads(chunk) async for chunk in response.body_iterator]
    return cache, lines


def _farm_list(slots):
    async def get_farm_list(list_id):
        return FarmList(id=list_id, slots=slots)

    return get_farm_list


class TestTheScanReadsEachSlotOnce:
    async def test_one_batched_lookup_serves_both_phases(self, monkeypatch):
        slots = [_slot(1, 12, 120, 1_000), _slot(2, 13, 121, 2_000)]
        entries = {(12, 120, 1_000): DEFENSE, (13, 121, 2_000): DEFENSE}

        cache, lines = await _scan(monkeypatch, slots, entries)

        assert len(cache.batches) == 1, "one lookup for the whole farm list"
        assert cache.batches[0] == [(12, 120, 1_000), (13, 121, 2_000)]

        results = [line for line in lines if line["type"] == "result"]
        assert len(results) == 2
        assert {r["slot_id"] for r in results} == {1, 2}
        assert all(r["defender_combat_strength"] == 900 for r in results)
        assert lines[0] == {
            "type": "progress",
            "total": 2,
            "cached": 2,
            "to_fetch": 0,
            "fetched": 0,
        }

    async def test_un_raided_slots_are_not_looked_up(self, monkeypatch):
        slots = [_slot(1, 12, 120, None), _slot(2, 13, 121, 2_000)]
        cache, lines = await _scan(monkeypatch, slots, {(13, 121, 2_000): DEFENSE})

        assert cache.batches[0] == [(13, 121, 2_000)]
        never_raided = [line for line in lines if line.get("never_raided")]
        assert [line["slot_id"] for line in never_raided] == [1]

    async def test_force_refresh_skips_the_lookup_entirely(self, monkeypatch):
        slots = [_slot(1, 12, 120, 1_000)]
        cache = _Cache({(12, 120, 1_000): DEFENSE})
        monkeypatch.setattr(farm_routes, "defense_cache", cache)

        async def fake_fetch(session, x, y):
            return DEFENSE

        monkeypatch.setattr(farm_routes, "_fetch_defense_for_coord", fake_fetch)
        session = SimpleNamespace(
            server_url="https://ts20.x2.europe.travian.com/",
            player_name="Attacker",
            farm_service=SimpleNamespace(get_farm_list=_farm_list(slots)),
        )
        response = await scan_defense_strength(
            DefenseInfoRequest(list_id=7, force_refresh=True),
            session=session,
            user=SimpleNamespace(id=1),
        )
        lines = [json.loads(chunk) async for chunk in response.body_iterator]

        assert cache.batches == []
        assert cache.stored == [[(12, 120, 1_000)]]
        assert [line["type"] for line in lines].count("result") == 1

    async def test_a_twin_slot_forcing_a_refetch_still_shadows_the_cached_one(self, monkeypatch):
        """Two slots on one coord, one raided since the cached report: the coord
        goes to the fetch phase for the stale slot, and the cached slot -- whose
        coord is now claimed by that phase -- is emitted by neither phase.

        That drop predates the batched lookup (verified against the pre-change
        route, which streams the same single result line). It is pinned here so
        the batching cannot be blamed for it, and so a fix for it is a visible
        change to this expectation rather than a silent one.
        """
        slots = [_slot(1, 12, 120, 1_000), _slot(2, 12, 120, 5_000)]
        entries = {(12, 120, 1_000): DEFENSE}
        fetched = {"defender_troops": {"t1": 7}, "defender_total": 7}

        cache, lines = await _scan(monkeypatch, slots, entries, fetched=fetched)

        results = [line for line in lines if line["type"] == "result"]
        assert [(r["slot_id"], r["defender_total"]) for r in results] == [(2, 7)]
        assert cache.stored == [[(12, 120, 5_000)]]

    async def test_a_fetched_group_is_written_in_one_batch(self, monkeypatch):
        slots = [_slot(1, 12, 120, 1_000), _slot(2, 13, 121, 2_000)]
        fetched = {"defender_total": 3}

        cache, lines = await _scan(monkeypatch, slots, {}, fetched=fetched)

        assert cache.stored == [[(12, 120, 1_000)], [(13, 121, 2_000)]]
        assert [line["type"] for line in lines].count("result") == 2
