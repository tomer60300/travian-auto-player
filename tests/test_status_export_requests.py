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


# Real capture, village 20003, 2026-08. Note l4 is negative while l5 is POSITIVE.
DORF1_STARVING = """
<html><script>
var resources = {
    storage: {l1: 88652, l2: 85167, l3: 93880, l4: 67397},
    production: {l1: 2875, l2: 3750, l3: 2175, l4: -5556, l5: 1481},
    maxStorage: {l1: 160000, l2: 160000, l3: 160000, l4: 240000}
};
</script></html>
"""

# The account-wide production table only knows GROSS crop production.
GROSS_PRODUCTION_HTML = """
<table id="production"><tbody>
<tr><td class="vil"><a href="/dorf1.php?newdid=20003">03</a></td>
<td>2875</td><td>3750</td><td>2175</td><td>1481</td></tr>
</tbody></table>
"""


class _StarvingVillageHttp:
    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        if url.startswith("/dorf1.php"):
            return DORF1_STARVING
        if url == "/village/statistics/resources/production":
            return GROSS_PRODUCTION_HTML
        return "<html></html>"


def _starving_export() -> dict:
    return asyncio.run(
        export_player_status(
            include_buildings=False, session=_session(_StarvingVillageHttp(), [20003])
        )
    )


def test_export_reports_net_crop_from_the_village_page():
    """Crop must be NET of troop feeding, as the game shows it.

    The account-wide production table cannot supply this: it carries gross crop
    only. The net rate lives in the per-village ``var resources`` blob and is
    negative when troops outgrow the fields.
    """
    res = _starving_export()["villages"][0]["resources"]

    assert res["crop_per_hour"] == -5556


def test_export_flags_a_starving_village_from_the_net_rate():
    """The starvation flag must come from l4, never from l5.

    This village's l5 is +1481 at the same moment its granary is draining at
    -5,556/h, so a free_crop-based check would call it healthy.
    """
    village = _starving_export()["villages"][0]

    assert village["crop"]["starving"] is True
    assert village["crop"]["net_per_hour"] == -5556
    # 67,397 crop / 5,556 per hour -- matches the warehouse countdown (43,899s).
    assert village["crop"]["hours_until_empty"] == 12.13
    assert village["resources"]["free_crop"] > 0


def test_export_does_not_flag_a_healthy_village():
    http = _RecordingHttp()

    village = asyncio.run(
        export_player_status(include_buildings=False, session=_session(http, [11]))
    )["villages"][0]

    assert village["crop"]["starving"] is False
    assert village["crop"]["hours_until_empty"] is None


def test_export_reports_whether_buildings_were_included():
    http = _RecordingHttp()

    result = asyncio.run(
        export_player_status(include_buildings=False, session=_session(http, [11]))
    )

    assert result["include_buildings"] is False
    assert "buildings" not in result["villages"][0]
