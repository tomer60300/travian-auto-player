"""In-memory TTL cache for village report data.

Avoids re-fetching tile-details for targets whose reports haven't changed.
Scoped per TravianSession — cleaned up on disconnect.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class VillageReportCache:
    """TTL-based in-memory cache keyed by (x, y) coordinates."""

    def __init__(self, ttl_seconds: int = 1800) -> None:
        self._ttl = ttl_seconds
        self._store: Dict[Tuple[int, int], Tuple[float, Dict[str, Any]]] = {}

    def get(self, x: int, y: int) -> Optional[Dict[str, Any]]:
        key = (x, y)
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            return None
        return data

    def put(self, x: int, y: int, data: Dict[str, Any]) -> None:
        self._store[(x, y)] = (time.monotonic(), data)

    def invalidate(self, x: int, y: int) -> None:
        self._store.pop((x, y), None)

    def clear(self) -> None:
        self._store.clear()

    def prune_expired(self) -> int:
        """Remove all expired entries. Returns count removed."""
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        return len(expired)

    @property
    def size(self) -> int:
        return len(self._store)
