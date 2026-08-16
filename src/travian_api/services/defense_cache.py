"""Two-tier defense scan cache: L1 in-memory + L2 disk-persistent.

L1: cachetools.TTLCache for hot reads (5-minute TTL, max 500 entries)
L2: diskcache.Cache on disk (survives restarts, adaptive TTL per target)

Cache key: "defense:{scope}:{x}:{y}" where scope is "{server_url}|{player_name}"
Cache value: dict with defense data + metadata for adaptive TTL

The scope is not optional. Defense data is read out of the ACCOUNT'S OWN raid
reports, so the same coordinates mean different data per account -- and per
world they are simply different villages. An unscoped key let one user read
another's report and raid on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)

_DEFAULT_DISK_DIR = Path("data/defense_cache")
_L1_MAXSIZE = 500
_L1_TTL = 300  # 5 minutes
_DEFAULT_TTL = 1800  # 30 minutes
_STATIC_TTL = 7200  # 2 hours (defense unchanged across 5+ checks)
_VOLATILE_TTL = 900  # 15 minutes (defense changes frequently)


class TieredDefenseCache:
    """L1 (memory) + L2 (disk) defense cache with adaptive TTL."""

    def __init__(self, disk_dir: str | Path | None = None) -> None:
        self._l1: TTLCache = TTLCache(maxsize=_L1_MAXSIZE, ttl=_L1_TTL)
        self._disk_dir = Path(disk_dir) if disk_dir else _DEFAULT_DISK_DIR
        self._l2 = None  # Lazy init to avoid import errors in tests
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}
        self._inflight: dict[str, asyncio.Future] = {}

    def _ensure_l2(self):
        if self._l2 is None:
            import diskcache

            self._disk_dir.mkdir(parents=True, exist_ok=True)
            self._l2 = diskcache.Cache(str(self._disk_dir), size_limit=50 * 1024 * 1024)
        return self._l2

    @staticmethod
    def _key(scope: str, x: int, y: int) -> str:
        return f"defense:{scope}:{x}:{y}"

    def get(self, scope: str, x: int, y: int, last_raid_time: int) -> dict[str, Any] | None:
        """Get cached defense data. Returns None if missing, expired, or stale."""
        key = self._key(scope, x, y)

        # L1 check
        entry = self._l1.get(key)
        if entry is not None:
            if entry.get("last_raid_ts") != last_raid_time:
                self._l1.pop(key, None)
            else:
                self._stats["l1_hits"] += 1
                return entry

        # L2 check
        try:
            l2 = self._ensure_l2()
            entry = l2.get(key)
        except Exception:
            entry = None

        if entry is None:
            self._stats["misses"] += 1
            return None

        if entry.get("last_raid_ts") != last_raid_time:
            try:
                l2.delete(key)
            except Exception:
                pass
            self._stats["misses"] += 1
            return None

        # Promote to L1
        self._l1[key] = entry
        self._stats["l2_hits"] += 1
        return entry

    def put(
        self, scope: str, x: int, y: int, last_raid_time: int, defense_data: dict[str, Any]
    ) -> None:
        """Store defense data in both L1 and L2."""
        key = self._key(scope, x, y)

        # Compute adaptive TTL from change history
        prev = None
        try:
            l2 = self._ensure_l2()
            prev = l2.get(key)
        except Exception:
            pass

        check_count = (prev.get("check_count", 0) if prev else 0) + 1
        change_count = prev.get("change_count", 0) if prev else 0
        if prev and prev.get("defender_combat_strength") != defense_data.get(
            "defender_combat_strength"
        ):
            change_count += 1

        entry = {
            **defense_data,
            "last_raid_ts": last_raid_time,
            "fetched_at": time.time(),
            "check_count": check_count,
            "change_count": change_count,
        }

        ttl = self._compute_ttl(check_count, change_count)

        # Write to L1
        self._l1[key] = entry

        # Write to L2
        try:
            l2 = self._ensure_l2()
            l2.set(key, entry, expire=ttl)
        except Exception as exc:
            logger.debug("Defense cache L2 write failed for (%s,%s): %s", x, y, exc)

    @staticmethod
    def _compute_ttl(check_count: int, change_count: int) -> int:
        if check_count < 2:
            return _DEFAULT_TTL
        ratio = change_count / check_count
        if ratio < 0.1:
            return _STATIC_TTL
        if ratio < 0.3:
            return _DEFAULT_TTL
        return _VOLATILE_TTL

    def get_inflight(self, scope: str, x: int, y: int) -> asyncio.Future | None:
        """Check if a fetch is already in-flight for this coordinate."""
        return self._inflight.get(self._key(scope, x, y))

    def set_inflight(self, scope: str, x: int, y: int) -> asyncio.Future:
        """Register an in-flight fetch. Returns the Future to resolve."""
        key = self._key(scope, x, y)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._inflight[key] = fut
        return fut

    def clear_inflight(self, scope: str, x: int, y: int) -> None:
        self._inflight.pop(self._key(scope, x, y), None)

    def get_stats(self) -> dict[str, Any]:
        """Return cache statistics for observability."""
        static = 0
        volatile = 0
        total = 0
        try:
            l2 = self._ensure_l2()
            for key in l2:
                entry = l2.get(key)
                if entry is None:
                    continue
                total += 1
                cc = entry.get("check_count", 0)
                chg = entry.get("change_count", 0)
                if cc >= 2:
                    ratio = chg / cc
                    if ratio < 0.1:
                        static += 1
                    elif ratio >= 0.3:
                        volatile += 1
        except Exception:
            pass

        return {
            "total_entries": total,
            "l1_size": len(self._l1),
            "l1_hits": self._stats["l1_hits"],
            "l2_hits": self._stats["l2_hits"],
            "misses": self._stats["misses"],
            "static_count": static,
            "volatile_count": volatile,
        }

    def clear(self) -> None:
        self._l1.clear()
        try:
            l2 = self._ensure_l2()
            l2.clear()
        except Exception:
            pass
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}


# Module-level singleton. Shared across all users, which is exactly why every
# key carries the caller's scope (server + player) alongside the coordinates.
defense_cache = TieredDefenseCache()
