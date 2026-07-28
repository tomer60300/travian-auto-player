"""Request-cost contract for the status export.

Resources and troops come from account-wide /village/statistics tables, so the
default export is a flat four requests no matter how many villages the account
has. Building levels are the only per-village data and are opt-in, costing one
dorf1 + one dorf2 each -- and each village page must still be fetched only once.
"""

import asyncio
from types import SimpleNamespace

from travian_api.services.building_service import BuildingService
from travian_api.services.military_service import MilitaryService
from travian_api.web.routes.status_export import export_player_status

STATS_URLS = [
    "/village/statistics/resources",
    "/village/statistics/resources/production",
    "/village/statistics/resources/capacity",
    "/village/statistics/troops",
]


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


def test_default_export_costs_four_requests_for_one_village():
    http = _RecordingHttp()

    asyncio.run(export_player_status(include_buildings=False, session=_session(http, [11])))

    assert http.urls == STATS_URLS


def test_default_export_cost_does_not_grow_with_village_count():
    http = _RecordingHttp()
    twenty = list(range(1, 21))

    asyncio.run(export_player_status(include_buildings=False, session=_session(http, twenty)))

    assert http.urls == STATS_URLS
    assert not any("dorf" in url for url in http.urls)


def test_including_buildings_adds_exactly_two_requests_per_village():
    http = _RecordingHttp()

    asyncio.run(export_player_status(include_buildings=True, session=_session(http, [11, 12])))

    assert http.urls[:4] == STATS_URLS
    assert sorted(http.urls[4:]) == [
        "/dorf1.php?newdid=11",
        "/dorf1.php?newdid=12",
        "/dorf2.php?newdid=11",
        "/dorf2.php?newdid=12",
    ]
    assert len(http.urls) == 4 + 2 * 2
    assert len(set(http.urls)) == len(http.urls)


def test_export_reports_whether_buildings_were_included():
    http = _RecordingHttp()

    result = asyncio.run(
        export_player_status(include_buildings=False, session=_session(http, [11]))
    )

    assert result["include_buildings"] is False
    assert "buildings" not in result["villages"][0]
