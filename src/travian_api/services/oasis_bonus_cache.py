"""Immutable per-coordinate oasis-bonus cache: L1 in-memory + L2 disk.

An oasis's resource bonus is a fixed property of its map coordinate — it
never changes for the life of a server. So unlike the defense cache (see
``defense_cache.py``) there is NO TTL and no adaptive expiry: once a
coordinate's bonus breakdown is parsed it is cached forever. This is the
load-bearing optimization behind the "villages by oasis bonus" scan mode —
re-scanning the same area costs ~0 tile-details requests.

L1: bounded LRU in memory (hot reads).
L2: diskcache on disk (survives restarts), lazily initialized.

Cache key: ``oasisbonus:{server}:{x}:{y}`` — server-scoped so two Travian
worlds sharing a process never bleed coordinates into each other.
Cache value: canonical breakdown dict ``{"wood"|"clay"|"iron"|"crop": pct}``.

A present-but-empty ``{}`` (a genuine parse that found no usable bonus row)
is a HIT and is distinguished from a miss via a sentinel — callers must only
re-fetch on a true miss, not on an empty parse.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from cachetools import LRUCache

logger = logging.getLogger(__name__)

_DEFAULT_DISK_DIR = Path("data/oasis_bonus_cache")
_L1_MAXSIZE = 5000

# Sentinel distinguishing "key absent" from a stored empty dict. Passed as
# the default to both L1 and L2 reads so identity comparison holds (diskcache
# returns the exact default object on a miss).
_MISS = object()


class OasisBonusCache:
    """L1 (memory) + L2 (disk) cache of immutable oasis bonus breakdowns."""

    def __init__(self, disk_dir: str | Path | None = None) -> None:
        self._l1: LRUCache = LRUCache(maxsize=_L1_MAXSIZE)
        self._disk_dir = Path(disk_dir) if disk_dir else _DEFAULT_DISK_DIR
        self._l2 = None  # Lazy init to avoid import/IO cost in tests
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}

    def _ensure_l2(self):
        if self._l2 is None:
            import diskcache

            self._disk_dir.mkdir(parents=True, exist_ok=True)
            self._l2 = diskcache.Cache(str(self._disk_dir), size_limit=50 * 1024 * 1024)
        return self._l2

    @staticmethod
    def _key(server: str, x: int, y: int) -> str:
        return f"oasisbonus:{server}:{x}:{y}"

    def get(self, server: str, x: int, y: int) -> dict[str, int] | None:
        """Return the cached breakdown, or None on a true miss.

        An empty dict is a valid cached value (the bonus table parsed but
        held no usable rows) and is returned as ``{}`` — only a genuine
        absence returns None.
        """
        key = self._key(server, x, y)

        entry = self._l1.get(key, _MISS)
        if entry is not _MISS:
            self._stats["l1_hits"] += 1
            return entry

        try:
            l2 = self._ensure_l2()
            entry = l2.get(key, _MISS)
        except Exception:
            entry = _MISS

        if entry is _MISS:
            self._stats["misses"] += 1
            return None

        self._l1[key] = entry
        self._stats["l2_hits"] += 1
        return entry

    def put(self, server: str, x: int, y: int, breakdown: dict[str, int]) -> None:
        """Store a breakdown in both tiers, with no expiry (immutable data).

        Callers must only call this on a SUCCESSFUL parse — never after a
        fetch/parse exception — so a transient network failure self-heals on
        the next scan instead of poisoning the cache with a bogus empty dict.
        """
        key = self._key(server, x, y)
        self._l1[key] = breakdown
        try:
            l2 = self._ensure_l2()
            l2.set(key, breakdown)  # no expire — bonus is immutable
        except Exception as exc:
            logger.debug("Oasis bonus cache L2 write failed for (%s,%s): %s", x, y, exc)

    def get_stats(self) -> dict[str, Any]:
        return {
            "l1_size": len(self._l1),
            "l1_hits": self._stats["l1_hits"],
            "l2_hits": self._stats["l2_hits"],
            "misses": self._stats["misses"],
        }

    def clear(self) -> None:
        self._l1.clear()
        try:
            l2 = self._ensure_l2()
            l2.clear()
        except Exception:
            pass
        self._stats = {"l1_hits": 0, "l2_hits": 0, "misses": 0}


# Module-level singleton (shared across users — keys include server + coords,
# never user_id, so cross-user sharing is correct and desirable).
oasis_bonus_cache = OasisBonusCache()
