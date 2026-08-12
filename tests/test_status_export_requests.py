"""Request-cost contract for the status export.

Troops come from the account-wide /village/statistics/troops table -- one
request regardless of village count. Resources are per-village on purpose:
only the dorf1 ``var resources`` blob carries NET crop (minus troop feeding)
and free crop, so the default export costs one dorf1 per village. Opting into
buildings adds one dorf2 per village -- resources must then reuse the dorf1
fetch the snapshot already makes, never fetch it twice.
"""

import asyncio
from types import SimpleNamespace

from travian_api.services.building_service import BuildingService
from travian_api.services.military_service import MilitaryService
from travian_api.web.routes.status_export import export_player_status

TROOPS_URL = "/village/statistics/troops"

# Non-zero stocks so get_resources() never falls back to a second (dorf2) fetch.
DORF1_BLOB = """
<html><script>
var resources = {
    storage: {l1: 100, l2: 100, l3: 100, l4: 100},
    production: {l1: 10, l2: 10, l3: 10, l4: 10, l5: 5},
    maxStorage: {l1: 8000, l2: 8000, l3: 8000, l4: 8000}
};
</script></html>
"""


class _RecordingHttp:
    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        self.urls.append(url)
        return DORF1_BLOB


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


def test_default_export_costs_one_troops_plus_one_dorf1_per_village():
    http = _RecordingHttp()

    asyncio.run(export_player_status(include_buildings=False, session=_session(http, [11])))

    assert http.urls == [TROOPS_URL, "/dorf1.php?newdid=11"]


def test_default_export_fetches_each_village_page_exactly_once():
    http = _RecordingHttp()
    twenty = list(range(1, 21))

    asyncio.run(export_player_status(include_buildings=False, session=_session(http, twenty)))

    assert http.urls[0] == TROOPS_URL
    assert sorted(http.urls[1:]) == sorted(f"/dorf1.php?newdid={vid}" for vid in twenty)
    assert len(set(http.urls)) == len(http.urls)


def test_including_buildings_adds_one_dorf2_per_village_without_refetching_dorf1():
    http = _RecordingHttp()

    asyncio.run(export_player_status(include_buildings=True, session=_session(http, [11, 12])))

    assert http.urls[0] == TROOPS_URL
    assert sorted(http.urls[1:]) == [
        "/dorf1.php?newdid=11",
        "/dorf1.php?newdid=12",
        "/dorf2.php?newdid=11",
        "/dorf2.php?newdid=12",
    ]
    assert len(http.urls) == 1 + 2 * 2
    assert len(set(http.urls)) == len(http.urls)


DORF1_STARVING = """
<html><script>
var resources = {
    storage: {l1: 81, l2: 66, l3: 93, l4: 20831},
    production: {l1: 745, l2: 745, l3: 745, l4: -3292, l5: -6536},
    maxStorage: {l1: 80000, l2: 80000, l3: 80000, l4: 240000}
};
</script></html>
"""

# The account-wide production table only knows GROSS crop production.
GROSS_PRODUCTION_HTML = """
<table id="production"><tbody>
<tr><td class="vil"><a href="/dorf1.php?newdid=20003">03</a></td>
<td>745</td><td>745</td><td>745</td><td>3244</td></tr>
</tbody></table>
"""


class _StarvingVillageHttp:
    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        if url.startswith("/dorf1.php"):
            return DORF1_STARVING
        if url == "/village/statistics/resources/production":
            return GROSS_PRODUCTION_HTML
        return "<html></html>"


def test_export_reports_net_crop_from_the_village_page():
    """Crop must be NET of troop feeding, as the game shows it.

    The /village/statistics production table only carries gross crop (+3,244
    for this village); the real net rate lives in the per-village ``var
    resources`` blob and is negative (-3,292) when troops outgrow production.
    """
    result = asyncio.run(
        export_player_status(
            include_buildings=False, session=_session(_StarvingVillageHttp(), [20003])
        )
    )

    res = result["villages"][0]["resources"]
    assert res["crop_per_hour"] == -3292
    assert res["free_crop"] == -6536


def test_export_reports_whether_buildings_were_included():
    http = _RecordingHttp()

    result = asyncio.run(
        export_player_status(include_buildings=False, session=_session(http, [11]))
    )

    assert result["include_buildings"] is False
    assert "buildings" not in result["villages"][0]
