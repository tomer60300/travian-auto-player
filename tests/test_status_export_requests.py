"""Regression: the status export must not fetch the same village page twice.

``get_village_buildings`` and ``get_resources`` both GET ``dorf1.php``. The
export needs both, so every village cost an extra round trip through the
stealth throttler -- on a 20-village account that is 20-40 wasted requests.
"""

import asyncio
from types import SimpleNamespace

from travian_api.services.building_service import BuildingService
from travian_api.services.military_service import MilitaryService
from travian_api.web.routes.status_export import export_player_status


class _RecordingHttp:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        self.urls.append(url)
        return "<html></html>"


def _session(http: _RecordingHttp, village_ids: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        auth_state=SimpleNamespace(
            villages=[SimpleNamespace(id=vid, name=f"V{vid}", x=vid, y=-vid) for vid in village_ids]
        ),
        tribe_id=2,
        player_name="Tester",
        building_service=BuildingService(http),
        military_service=MilitaryService(http, None),
    )


def test_export_fetches_each_village_page_once():
    http = _RecordingHttp()

    asyncio.run(export_player_status(_session(http, [11])))

    assert http.urls.count("/dorf1.php?newdid=11") == 1
    assert http.urls.count("/dorf2.php?newdid=11") == 1
    assert http.urls.count("/village/statistics/troops?newdid=11") == 1
    assert len(http.urls) == 3


def test_export_costs_three_requests_per_village():
    http = _RecordingHttp()

    asyncio.run(export_player_status(_session(http, [1, 2, 3, 4, 5])))

    assert len(http.urls) == 15
    assert len(set(http.urls)) == len(http.urls)
