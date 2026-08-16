"""Scan-cache age math must survive SQLite's naive datetimes.

The column is declared timezone=True, but SQLite has no timezone storage:
rows come back naive. Subtracting a naive `updated_at` from an aware
`datetime.now(UTC)` raises TypeError, turning GET /api/farm-builder/scan-cache
into a 500 for every user who has ever saved a scan.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from travian_api.web.routes.farm_builder import get_scan_cache


class _Db:
    def __init__(self, row):
        self._row = row

    async def execute(self, _stmt):
        row = self._row
        return SimpleNamespace(scalar_one_or_none=lambda: row)


def _row(updated_at):
    return SimpleNamespace(updated_at=updated_at, scan_json=json.dumps({"targets": []}))


class TestNaiveTimestamps:
    def test_a_fresh_naive_row_is_served_not_500(self):
        naive_now = datetime.now(UTC).replace(tzinfo=None)  # what SQLite returns
        out = asyncio.run(get_scan_cache(SimpleNamespace(id=1), _Db(_row(naive_now))))
        assert out.has_cache is True

    def test_a_stale_naive_row_is_expired_not_500(self):
        naive_old = (datetime.now(UTC) - timedelta(hours=2)).replace(tzinfo=None)
        out = asyncio.run(get_scan_cache(SimpleNamespace(id=1), _Db(_row(naive_old))))
        assert out.has_cache is False

    def test_an_aware_row_still_works(self):
        out = asyncio.run(get_scan_cache(SimpleNamespace(id=1), _Db(_row(datetime.now(UTC)))))
        assert out.has_cache is True
