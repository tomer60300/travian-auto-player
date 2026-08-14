"""Regression tests for the distribution planner HTTP surface.

Each test pins one finding from the feature/resource-distribution-planner code
review: request pricing, silent drops, error status codes, and the plan
endpoint's independence from a live Travian session.
"""

import asyncio
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.services.building_service import BuildingService
from travian_api.web.routes.distribution import (
    PlanRequest,
    get_snapshot,
    post_plan,
)
from travian_api.web.sessions import get_travian_session

PRODUCTION_HTML = """
<table id="production">
    <thead><tr><td>Village</td><td><i class="r1"></i></td><td><i class="r2"></i></td>
        <td><i class="r3"></i></td><td><i class="r4"></i></td></tr></thead>
    <tbody>
        <tr class="hover">
            <td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
            <td class="lum">‭2,000‬</td><td class="clay">‭1,000‬</td>
            <td class="iron">‭1,000‬</td><td class="crop">‭2,848‬</td>
        </tr>
        <tr class="hl">
            <td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
            <td class="lum">‭500‬</td><td class="clay">‭500‬</td>
            <td class="iron">‭500‬</td><td class="crop">‭1,000‬</td>
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

# Village 03 drains, village 11 fills -- the filling one forces a capacity fetch.
WAREHOUSE_HTML = """
<table id="warehouse">
    <thead><tr><td>Village</td><td><i class="r1"></i></td><td><i class="r2"></i></td>
        <td><i class="r3"></i></td><td><img class="clock" alt="Duration"></td>
        <td><i class="r4"></i></td><td><img class="clock" alt="Duration"></td></tr></thead>
    <tbody>
    <tr class="hover">
        <td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
        <td class="lum">‭55‬%</td><td class="clay">‭53‬%</td><td class="iron">‭58‬%</td>
        <td class="max123"><span class="timer" counting="down" value="72065" data-value="72065">20:01:05</span></td>
        <td class="crop">‭28‬%</td>
        <td class="max4 lc"><span class="crit">−</span>&nbsp;<span class="timer crit" counting="down" value="43899" data-value="43899">12:11:39</span></td>
    </tr><tr class="hover">
        <td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
        <td class="lum">‭18‬%</td><td class="clay">‭35‬%</td><td class="iron">‭0‬%</td>
        <td class="max123"><span class="timer" counting="down" value="88017" data-value="88017">24:26:57</span></td>
        <td class="crop">‭56‬%</td>
        <td class="max4 lc"><span class="timer" counting="down" value="211328" data-value="211328">58:42:08</span></td>
    </tr>
    </tbody>
</table>
"""

# Both villages draining: capacity is never needed, so the snapshot is cheaper.
WAREHOUSE_ALL_DRAINING_HTML = WAREHOUSE_HTML.replace(
    '<span class="timer" counting="down" value="211328" data-value="211328">',
    '<span class="timer crit" counting="down" value="211328" data-value="211328">',
)

CAPACITY_HTML = """
<table id="capacity"><tbody>
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20003">03</a></td>
        <td class="max123">‭160,000‬</td><td class="max4">‭240,000‬</td></tr>
    <tr class="hover"><td class="vil fc"><a href="/dorf1.php?newdid=20011">11</a></td>
        <td class="max123">‭160,000‬</td><td class="max4">‭160,000‬</td></tr>
</tbody></table>
"""


class _SnapshotHttp:
    """Serves the statistics pages and records every request made."""

    def __init__(self, production=PRODUCTION_HTML, warehouse=WAREHOUSE_HTML):
        self.production = production
        self.warehouse = warehouse
        self.urls: list[str] = []

    async def get_html(self, url: str, skip_reauth: bool = True) -> str:
        self.urls.append(url)
        if url.endswith("/production"):
            return self.production
        if url.endswith("/warehouse"):
            return self.warehouse
        if url.endswith("/capacity"):
            return CAPACITY_HTML
        return RESOURCES_HTML


def _session(http: _SnapshotHttp) -> SimpleNamespace:
    return SimpleNamespace(
        auth_state=SimpleNamespace(
            villages=[
                SimpleNamespace(id=20003, name="03", x=23, y=88),
                SimpleNamespace(id=20011, name="11", x=30, y=90),
            ]
        ),
        http_client=http,
        building_service=BuildingService(http),
    )


class TestSnapshotPricing:
    def test_reports_three_requests_when_nothing_is_filling(self):
        """The capacity page is only fetched for filling villages, and the
        reported price must match what was actually spent."""
        http = _SnapshotHttp(warehouse=WAREHOUSE_ALL_DRAINING_HTML)

        res = asyncio.run(get_snapshot(_session(http)))

        assert len(http.urls) == 3
        assert res.requests_used == 3

    def test_reports_four_requests_when_capacity_is_fetched(self):
        http = _SnapshotHttp()

        res = asyncio.run(get_snapshot(_session(http)))

        assert len(http.urls) == 4
        assert res.requests_used == 4

    def test_warns_when_the_production_table_is_missing(self):
        """Without Travian Plus the production table is absent; rates silently
        defaulting to 0/h must at least be said out loud."""
        http = _SnapshotHttp(production="<html>no table</html>")

        res = asyncio.run(get_snapshot(_session(http)))

        assert any("production" in w for w in res.warnings)


def _plan_request(allocations, merchants_free=20, crop=None):
    return PlanRequest.model_validate(
        {
            "snapshot": [
                {
                    "village_id": 20003,
                    "name": "03",
                    "x": 0,
                    "y": 0,
                    "merchants_total": 20,
                    "merchants_free": merchants_free,
                    "lumber_per_hour": 2000,
                    "clay_per_hour": 1000,
                    "iron_per_hour": 1000,
                    "crop_per_hour": crop,
                },
                {
                    "village_id": 20011,
                    "name": "11",
                    "x": 10,
                    "y": 0,
                    "merchants_total": 20,
                    "merchants_free": merchants_free,
                    "lumber_per_hour": 500,
                    "clay_per_hour": 500,
                    "iron_per_hour": 500,
                    "crop_per_hour": crop,
                },
            ],
            "allocations": allocations,
        }
    )


class TestPlanEndpoint:
    def test_needs_no_travian_session(self):
        """Planning is pure over the request body. A session dependency makes
        the zero-request endpoint spend real login traffic (or 403) when the
        session has expired."""
        for parameter in inspect.signature(post_plan).parameters.values():
            dependency = getattr(parameter.default, "dependency", None)
            assert dependency is not get_travian_session

    def test_out_of_range_values_return_400_not_500(self):
        body = _plan_request({"lumber": {"20003": {"mode": "percentage", "value": 150}}})

        with pytest.raises(HTTPException) as exc:
            asyncio.run(post_plan(body))

        assert exc.value.status_code == 400
        assert "percentage" in exc.value.detail

    def test_inert_keep_entries_for_unknown_villages_do_not_fail_the_plan(self):
        """The UI's remainder radio leaves `keep` husks behind, and villages get
        lost or chiefed; an entry that means 'do nothing' must not 400."""
        body = _plan_request({"lumber": {"99999": {"mode": "keep", "value": 0}}})

        res = asyncio.run(post_plan(body))

        assert res.feasible is not None

    def test_allocations_for_a_resource_with_no_production_warn_instead_of_vanishing(self):
        """All crop rates unknown: the user's crop targets cannot be planned,
        and saying nothing violates the planner's own no-silent-drop rule."""
        body = _plan_request({"crop": {"20003": {"mode": "absolute", "value": 500}}}, crop=None)

        res = asyncio.run(post_plan(body))

        assert any("crop" in w and "ignor" in w for w in res.warnings)

    def test_committing_more_than_the_free_merchants_warns(self):
        """The plan budgets against merchants_total; when in-game routes hold
        most of them, the sheet is not executable until they are released."""
        body = _plan_request(
            {
                "lumber": {
                    "20003": {"mode": "absolute", "value": 500},
                    "20011": {"mode": "remainder"},
                }
            },
            merchants_free=0,
        )

        res = asyncio.run(post_plan(body))

        assert res.total_merchants > 0
        assert any("free" in w for w in res.warnings)
