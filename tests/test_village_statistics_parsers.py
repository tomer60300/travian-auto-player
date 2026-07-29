"""Tests for the account-wide /village/statistics/* table parsers.

Markup is trimmed from real pages (20 villages cut to 2), keeping the bidi
override marks and thousands separators Travian wraps every number in.
"""

from travian_api.parsers.html_parser import (
    parse_village_stats_capacity,
    parse_village_stats_production,
    parse_village_stats_resources,
    parse_village_stats_troops,
)

RESOURCES_HTML = """
<table cellpadding="1" cellspacing="1" id="ressources">
    <thead><tr>
        <td>Village</td><td><i class="r1"></i></td><td><i class="r2"></i></td>
        <td><i class="r3"></i></td><td><i class="r4"></i></td><td>Merchants</td>
    </tr></thead>
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=20001">01 Hammer</a></td>
            <td class="lum">‭95,506‬</td>
            <td class="clay">‭87,834‬</td>
            <td class="iron">‭89,677‬</td>
            <td class="crop">‭43,254‬</td>
            <td class="tra lc"><a href="/build.php?gid=17&amp;z=43925">‭‭19‬/‭20‬‬</a></td>
        </tr>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=20002">02</a></td>
            <td class="lum">‭620,875‬</td>
            <td class="clay">‭506,707‬</td>
            <td class="iron">‭407,559‬</td>
            <td class="crop">‭98,307‬</td>
            <td class="tra lc"><a href="/build.php?gid=17&amp;z=45135">‭‭15‬/‭20‬‬</a></td>
        </tr>
        <tr><td colspan="6" class="empty"></td></tr>
        <tr class="sum">
            <td class="vil">Sum</td>
            <td class="lum">‭1,501,395‬</td>
            <td class="clay">‭1,375,859‬</td>
            <td class="iron">‭1,296,788‬</td>
            <td class="crop">‭1,083,927‬</td>
            <td class="tra">‭‭276‬/‭381‬‬</td>
        </tr>
    </tbody>
</table>
"""

PRODUCTION_HTML = """
<table cellpadding="1" cellspacing="1" id="production">
    <thead><tr><td>Village</td><td><i class="r1"></i></td><td><i class="r2"></i></td>
        <td><i class="r3"></i></td><td><i class="r4"></i></td></tr></thead>
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=20001">01 Hammer</a></td>
            <td class="lum">‭1,750‬</td>
            <td class="clay">‭2,100‬</td>
            <td class="iron">‭1,750‬</td>
            <td class="crop">‭2,848‬</td>
        </tr>
        <tr class="hl">
            <td class="vil fc"><a href="/dorf1.php?newdid=20015">15</a></td>
            <td class="lum">‭219‬</td>
            <td class="clay">‭88‬</td>
            <td class="iron">‭88‬</td>
            <td class="crop">‭10,998‬</td>
        </tr>
        <tr><td colspan="5" class="empty"></td></tr>
        <tr class="sum">
            <td class="vil">Sum: <span class="total">‭248,616‬</span></td>
            <td class="lum">‭42,298‬</td>
            <td class="clay">‭42,703‬</td>
            <td class="iron">‭35,230‬</td>
            <td class="crop">‭128,385‬</td>
        </tr>
    </tbody>
</table>
"""

CAPACITY_HTML = """
<table cellpadding="1" cellspacing="1" id="capacity">
    <thead><tr><td>Village</td><td>Warehouse</td><td>Granary</td></tr></thead>
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=20001">01 Hammer</a></td>
            <td class="max123">‭240,000‬</td>
            <td class="max4">‭80,000‬</td>
        </tr>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=20018">18</a></td>
            <td class="max123">‭55,100‬</td>
            <td class="max4">‭31,300‬</td>
        </tr>
        <tr><td colspan="3" class="empty"></td></tr>
        <tr class="sum">
            <td class="vil">Sum</td>
            <td class="max123">‭4,222,300‬</td>
            <td class="max4">‭3,761,500‬</td>
        </tr>
    </tbody>
</table>
"""

TROOPS_HTML = """
<table cellpadding="1" cellspacing="1" id="troops">
<thead>
<tr>
    <th class="villageName">Village</th>
    <td class="unit"><img class="unit u11" src="/img/x.gif" alt="Clubswinger"></td>
    <td class="unit"><img class="unit u12" src="/img/x.gif" alt="Spearman"></td>
    <td class="unit"><img class="unit u13" src="/img/x.gif" alt="Axeman"></td>
    <td class="unit"><img class="unit u14" src="/img/x.gif" alt="Scout"></td>
    <td class="unit"><img class="unit u15" src="/img/x.gif" alt="Paladin"></td>
    <td class="unit"><img class="unit u16" src="/img/x.gif" alt="Teutonic Knight"></td>
    <td class="unit"><img class="unit u17" src="/img/x.gif" alt="Ram"></td>
    <td class="unit"><img class="unit u18" src="/img/x.gif" alt="Catapult"></td>
    <td class="unit"><img class="unit u19" src="/img/x.gif" alt="Chief"></td>
    <td class="unit"><img class="unit u20" src="/img/x.gif" alt="Settler"></td>
    <td class="unit"><img class="unit uhero" src="/img/x.gif" alt="Hero"></td>
</tr>
</thead>
<tbody>
    <tr class="hover">
        <td class="villageName"><a href="/build.php?newdid=20001&amp;id=39#td">01 Hammer</a></td>
        <td>1064</td><td>2</td><td>102</td><td>1385</td><td>39</td><td>83</td>
        <td class="none">0</td><td class="none">0</td><td class="none">0</td>
        <td class="none">0</td><td class="none">0</td>
    </tr>
    <tr class="hover">
        <td class="villageName"><a href="/build.php?newdid=20003&amp;id=39#td">03</a></td>
        <td>58689</td><td>242</td><td>2829</td><td>19</td><td>232</td><td>14996</td>
        <td>4113</td><td>1499</td><td>2</td><td class="none">0</td><td>1</td>
    </tr>
    <tr><td colspan="12" class="empty"></td></tr>
    <tr class="sum small">
        <td class="vil">Sum</td>
        <td>60405</td><td>261</td><td>2931</td><td>1443</td><td>311</td><td>15119</td>
        <td>4113</td><td>1590</td><td>3</td><td class="none">0</td><td>1</td>
    </tr>
</tbody>
</table>
"""


def test_resources_table_maps_village_id_to_stocks():
    out = parse_village_stats_resources(RESOURCES_HTML)

    assert set(out) == {20001, 20002}
    assert out[20001] == {"lumber": 95506, "clay": 87834, "iron": 89677, "crop": 43254}
    assert out[20002]["lumber"] == 620875


def test_production_table_maps_village_id_to_hourly_rates():
    out = parse_village_stats_production(PRODUCTION_HTML)

    assert set(out) == {20001, 20015}
    assert out[20001] == {"lumber": 1750, "clay": 2100, "iron": 1750, "crop": 2848}
    assert out[20015]["crop"] == 10998


def test_capacity_table_maps_village_id_to_warehouse_and_granary():
    out = parse_village_stats_capacity(CAPACITY_HTML)

    assert out == {
        20001: {"warehouse": 240000, "granary": 80000},
        20018: {"warehouse": 55100, "granary": 31300},
    }


def test_troops_table_maps_unit_columns_to_tribe_slots():
    out = parse_village_stats_troops(TROOPS_HTML, tribe_id=2)

    assert set(out) == {20001, 20003}
    # Teuton offset: u11 -> t1 (Clubswinger) ... u16 -> t6 (Teutonic Knight).
    assert out[20001]["t1"] == 1064
    assert out[20001]["t4"] == 1385
    assert out[20001]["t6"] == 83
    assert out[20003]["t1"] == 58689
    assert out[20003]["t6"] == 14996
    assert out[20003]["t8"] == 1499
    # The hero column has no tN slot and must not leak into t10.
    assert out[20003]["t10"] == 0


def test_sum_row_is_never_treated_as_a_village():
    # The Sum row carries no newdid link, so it must not appear in any table.
    assert 60405 not in parse_village_stats_troops(TROOPS_HTML, tribe_id=2)
    for parsed in (
        parse_village_stats_resources(RESOURCES_HTML),
        parse_village_stats_production(PRODUCTION_HTML),
        parse_village_stats_capacity(CAPACITY_HTML),
    ):
        assert len(parsed) == 2


def test_missing_table_yields_empty_mapping():
    assert parse_village_stats_resources("<html><body>nope</body></html>") == {}
    assert parse_village_stats_troops("<html><body>nope</body></html>", tribe_id=2) == {}


def test_unknown_tribe_still_maps_unit_columns():
    """tribe_id=0 must not silently return zeros — that is the bug this replaced.

    Only the player's own tribe appears on the page, so the column ids identify
    the slots on their own; an unknown tribe must not discard every column.
    """
    out = parse_village_stats_troops(TROOPS_HTML, tribe_id=0)

    assert out[20001]["t1"] == 1064
    assert out[20001]["t6"] == 83
    assert out[20003]["t6"] == 14996


LOCALISED_HTML = """
<table id="ressources">
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=7">L</a></td>
            <td class="lum">‭1.234‬</td>
            <td class="clay">‭5 678‬</td>
            <td class="iron">‭9 012‬</td>
            <td class="crop">−345</td>
        </tr>
    </tbody>
</table>
"""


def test_localised_number_formats_are_parsed():
    """Other Travian locales group with dots or spaces and use a Unicode minus."""
    out = parse_village_stats_resources(LOCALISED_HTML)

    assert out[7] == {"lumber": 1234, "clay": 5678, "iron": 9012, "crop": -345}


NON_NUMERIC_HTML = """
<table id="capacity">
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=8">N</a></td>
            <td class="max123">‭19‬/‭20‬</td>
            <td class="max4">—</td>
        </tr>
    </tbody>
</table>
"""


def test_composite_and_placeholder_cells_do_not_invent_numbers():
    """A ratio cell must not collapse into 1920; a dash is genuinely absent."""
    out = parse_village_stats_capacity(NON_NUMERIC_HTML)

    assert out[8] == {"warehouse": 0, "granary": 0}


SIMILAR_PARAM_HTML = """
<table id="capacity">
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/x.php?oldnewdid=999">not a village row</a></td>
            <td class="max123">‭1‬</td>
            <td class="max4">‭2‬</td>
        </tr>
    </tbody>
</table>
"""


def test_lookalike_query_param_is_not_read_as_a_village_id():
    assert parse_village_stats_capacity(SIMILAR_PARAM_HTML) == {}


def test_troops_are_never_reported_as_a_uniform_zero_army():
    """Guards the failure this parser replaced.

    The predecessor matched on a ``tbody.units`` layout this page does not use,
    so it silently returned zeros for every unit — an account with 60k troops
    exported as empty. Any future rewrite must keep this impossible.
    """
    out = parse_village_stats_troops(TROOPS_HTML, tribe_id=2)

    assert out, "no villages parsed at all"
    assert any(count for village in out.values() for count in village.values())
