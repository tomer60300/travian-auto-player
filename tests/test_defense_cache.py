"""Defense-cache isolation: entries belong to one server+account, never shared.

The cache key used to be bare coordinates. Two users on different worlds (or
two accounts on one world) scanning the same (x, y) would read each other's
defense reports -- and raid decisions made from another account's report are
unsafe. These tests pin that every path (L1, L2, in-flight coalescing) is
scoped.
"""

import asyncio

from travian_api.services.defense_cache import TieredDefenseCache

SCOPE_A = "https://ts20.x2.europe.travian.com|Attacker"
SCOPE_B = "https://ts6.x1.america.travian.com|OtherPlayer"


class TestScopedKeys:
    def test_another_scope_never_sees_the_entry(self, tmp_path):
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE_A, 12, 120, last_raid_time=1_000, defense_data={"defender_total": 55})

        assert cache.get(SCOPE_A, 12, 120, last_raid_time=1_000) is not None
        assert cache.get(SCOPE_B, 12, 120, last_raid_time=1_000) is None, (
            "same coordinates on another world/account must be a miss"
        )

    def test_another_scope_misses_even_after_l1_expiry(self, tmp_path):
        """The L2 (disk) path is a separate lookup; it must be scoped too."""
        cache = TieredDefenseCache(disk_dir=tmp_path)
        cache.put(SCOPE_A, 12, 120, last_raid_time=1_000, defense_data={"defender_total": 55})
        cache._l1.clear()  # force the read down to disk

        assert cache.get(SCOPE_B, 12, 120, last_raid_time=1_000) is None
        assert cache.get(SCOPE_A, 12, 120, last_raid_time=1_000) is not None

    def test_inflight_coalescing_is_scoped(self, tmp_path):
        """A fetch in flight for one account must not hand its future -- and
        with it, that account's defense report -- to another account asking
        about the same coordinates."""

        async def scenario():
            cache = TieredDefenseCache(disk_dir=tmp_path)
            cache.set_inflight(SCOPE_A, 12, 120)
            try:
                assert cache.get_inflight(SCOPE_B, 12, 120) is None
                assert cache.get_inflight(SCOPE_A, 12, 120) is not None
            finally:
                cache.clear_inflight(SCOPE_A, 12, 120)
            assert cache.get_inflight(SCOPE_A, 12, 120) is None

        asyncio.run(scenario())
