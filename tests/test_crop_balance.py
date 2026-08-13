"""Net crop for the whole account from the granary countdown.

Markup is trimmed from a real /village/statistics/resources/warehouse capture.
Inverting the countdown avoids the upkeep model entirely, which matters because
consumption is charged where troops *stand*: a village's own army may be
reinforcing elsewhere while foreign troops eat its crop, so no arithmetic over
"own troops" can produce the right answer.
"""

import asyncio

import pytest

from travian_api.parsers.html_parser import parse_village_stats_warehouse
from travian_api.services.building_service import BuildingService, derive_net_crop_per_hour

WAREHOUSE_HTML = """
<table cellpadding="1" cellspacing="1" id="warehouse">
    <thead><tr>
        <td onclick="sortByColumnOrder('sortIndex')">Village</td>
        <td><i class="r1"></i></td><td><i class="r2"></i></td><td><i class="r3"></i></td>
        <td><img class="clock" alt="Duration"></td>
        <td><i class="r4"></i></td>
        <td><img class="clock" alt="Duration"></td>
    </tr></thead>
    <tbody>
    <tr class="hover" "="">
        <td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
        <td class="lum">‭‭55‬%‬</td><td class="clay">‭‭53‬%‬</td><td class="iron">‭‭58‬%‬</td>
        <td class="max123"><span class="timer" counting="down" value="72065" data-value="72065">20:01:05</span></td>
        <td class="crop">‭‭28‬%‬</td>
        <td class="max4 lc"><span class="crit">−</span>&nbsp;<span class="timer crit" counting="down" value="43899" data-value="43899">12:11:39</span></td>
    </tr><tr class="hover" "="">
        <td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
        <td class="lum">‭‭18‬%‬</td><td class="clay">‭‭35‬%‬</td><td class="iron">‭‭0‬%‬</td>
        <td class="max123"><span class="timer" counting="down" value="88017" data-value="88017">24:26:57</span></td>
        <td class="crop">‭‭56‬%‬</td>
        <td class="max4 lc"><span class="timer" counting="down" value="211328" data-value="211328">58:42:08</span></td>
    </tr>
    </tbody>
</table>
"""

RESOURCES_HTML = """
<table id="ressources"><tbody>
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
        <td class="lum">‭1‬</td><td class="clay">‭1‬</td><td class="iron">‭1‬</td>
        <td class="crop">‭67,397‬</td><td class="tra lc"><a href="#">‭20‬/‭20‬</a></td></tr>
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
        <td class="lum">‭1‬</td><td class="clay">‭1‬</td><td class="iron">‭1‬</td>
        <td class="crop">‭89,600‬</td><td class="tra lc"><a href="#">‭20‬/‭20‬</a></td></tr>
</tbody></table>
"""

CAPACITY_HTML = """
<table id="capacity"><tbody>
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
        <td class="max123">‭160,000‬</td><td class="max4">‭240,000‬</td></tr>
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
        <td class="max123">‭160,000‬</td><td class="max4">‭160,000‬</td></tr>
</tbody></table>
"""


class TestWarehouseParser:
    def test_reads_raw_seconds_and_direction(self):
        out = parse_village_stats_warehouse(WAREHOUSE_HTML)

        assert out[20003]["crop_seconds"] == 43899
        assert out[20003]["crop_draining"] is True
        assert out[20011]["crop_seconds"] == 211328
        assert out[20011]["crop_draining"] is False

    def test_reads_percentages_and_the_warehouse_timer(self):
        out = parse_village_stats_warehouse(WAREHOUSE_HTML)

        assert out[20003]["crop_percent"] == 28
        assert out[20003]["warehouse_seconds"] == 72065

    def test_tolerates_the_malformed_row_markup(self):
        """Real rows are emitted as `<tr class="hover" "="">`."""
        assert set(parse_village_stats_warehouse(WAREHOUSE_HTML)) == {20003, 20011}

    def test_missing_table_yields_nothing(self):
        assert parse_village_stats_warehouse("<html>nope</html>") == {}


class TestDerivation:
    def test_draining_village_matches_the_rate_the_game_reports(self):
        """V03: 67,397 crop draining at -5,556/h empties in 43,670s.

        Cross-checked against that village's own dorf1 blob, which reports
        production.l4 = -5556.
        """
        net = derive_net_crop_per_hour(stock=67397, seconds_remaining=43670, draining=True)

        assert net == pytest.approx(-5556, rel=1e-3)

    def test_draining_village_needs_no_granary_capacity(self):
        """Which is why a starving-village report costs only two requests."""
        assert derive_net_crop_per_hour(67397, 43899, draining=True) is not None

    def test_filling_village_uses_headroom(self):
        net = derive_net_crop_per_hour(
            stock=89600, seconds_remaining=211328, draining=False, granary_capacity=160000
        )

        assert net == pytest.approx((160000 - 89600) / (211328 / 3600))
        assert net > 0

    def test_filling_village_without_capacity_returns_none_not_zero(self):
        """A silent zero reads as a healthy village. This codebase has shipped
        that bug three times; do not add a fourth."""
        assert derive_net_crop_per_hour(89600, 211328, draining=False) is None

    def test_a_stopped_countdown_is_underivable(self):
        assert derive_net_crop_per_hour(1000, 0, draining=True) is None


class _StatsHttp:
    """Serves the three statistics tables and records what was requested."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        self.urls.append(url)
        if url.endswith("/warehouse"):
            return WAREHOUSE_HTML
        if url.endswith("/capacity"):
            return CAPACITY_HTML
        return RESOURCES_HTML


class TestAccountWideFetch:
    def test_costs_three_requests_when_capacity_is_unknown(self):
        http = _StatsHttp()

        balances = asyncio.run(BuildingService(http).get_all_villages_net_crop())

        assert http.urls == [
            "/village/statistics/resources",
            "/village/statistics/resources/warehouse",
            "/village/statistics/resources/capacity",
        ]
        assert balances[20003].net_per_hour == pytest.approx(-5527, rel=1e-3)
        assert balances[20003].draining is True
        assert balances[20011].net_per_hour > 0

    def test_costs_two_requests_when_capacity_is_cached(self):
        """Granary capacity changes only on upgrade, so it caches."""
        http = _StatsHttp()

        balances = asyncio.run(
            BuildingService(http).get_all_villages_net_crop(
                granary_capacity={20003: 240000, 20011: 160000}
            )
        )

        assert http.urls == [
            "/village/statistics/resources",
            "/village/statistics/resources/warehouse",
        ]
        assert balances[20011].net_per_hour > 0

    def test_an_all_draining_account_never_fetches_capacity(self):
        """Capacity only matters for filling villages."""
        http = _StatsHttp()
        http.get_html = _only_draining(http)

        asyncio.run(BuildingService(http).get_all_villages_net_crop())

        assert not any("capacity" in url for url in http.urls)


def _only_draining(http: _StatsHttp):
    draining_only = WAREHOUSE_HTML.replace(
        '<span class="timer" counting="down" value="211328" data-value="211328">',
        '<span class="timer crit" counting="down" value="211328" data-value="211328">',
    )

    async def get_html(url: str, skip_reauth: bool = True) -> str:
        http.urls.append(url)
        if url.endswith("/warehouse"):
            return draining_only
        if url.endswith("/capacity"):
            raise AssertionError("capacity must not be fetched when nothing is filling")
        return RESOURCES_HTML

    return get_html
