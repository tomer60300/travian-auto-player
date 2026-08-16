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


def _session(http: _SnapshotHttp, tribe_id: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        auth_state=SimpleNamespace(
            villages=[
                SimpleNamespace(id=20003, name="03", x=42, y=17),
                SimpleNamespace(id=20011, name="11", x=30, y=90),
            ]
        ),
        tribe_id=tribe_id,
        http_client=http,
        building_service=BuildingService(http),
    )


class TestSnapshotPricing:
    def test_snapshot_never_spends_an_implicit_login(self):
        """The endpoint prices itself at 3-4 requests; auto-reconnect would
        spend unreported login traffic before the handler even starts. It must
        depend on the live session only and 403 otherwise."""
        from travian_api.web.sessions import get_travian_session as reconnecting

        for parameter in inspect.signature(get_snapshot).parameters.values():
            dependency = getattr(parameter.default, "dependency", None)
            assert dependency is not reconnecting

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

    def test_merchant_speed_comes_from_the_connected_tribe(self):
        """Merchant travel speed is tribe-specific; hardcoding Teuton's 12
        f/h inflates travel times and merchant counts for Romans/Gauls."""
        assert (
            asyncio.run(get_snapshot(_session(_SnapshotHttp(), tribe_id=2))).speed_fields_per_hour
            == 12
        )  # Teuton
        assert (
            asyncio.run(get_snapshot(_session(_SnapshotHttp(), tribe_id=1))).speed_fields_per_hour
            == 16
        )  # Roman
        assert (
            asyncio.run(get_snapshot(_session(_SnapshotHttp(), tribe_id=3))).speed_fields_per_hour
            == 24
        )  # Gaul

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


class TestStorageSafety:
    """The plan must say when a village will starve or overflow.

    A route set can balance perfectly as rates and still be wrong in the game:
    a granary emptying inside the day kills troops, and a store that reaches its
    cap silently discards everything above it. Both are computed from the
    snapshot the caller already holds, so the check costs no game requests.
    """

    def _village(self, vid, **overrides):
        base = dict(
            village_id=vid,
            name=str(vid),
            x=0,
            y=0,
            merchants_total=20,
            merchants_free=20,
            lumber_per_hour=1000,
            clay_per_hour=1000,
            iron_per_hour=1000,
            crop_per_hour=1000,
            crop_stock=50_000,
            lumber_stock=10_000,
            clay_stock=10_000,
            iron_stock=10_000,
            warehouse_capacity=800_000,
            granary_capacity=800_000,
        )
        base.update(overrides)
        return base

    def test_a_starving_village_is_reported(self):
        body = PlanRequest(
            snapshot=[
                self._village(1, x=0, y=0),
                # Eats far more crop than it grows, with hours of stock left.
                self._village(2, x=5, y=0, crop_per_hour=-9000, crop_stock=9000),
            ],
        )

        result = asyncio.run(post_plan(body, SimpleNamespace(id=1)))

        starving = [w for w in result.warnings if "runs out" in w and "village 2" in w]
        assert starving, f"no starvation warning in {result.warnings}"
        assert "crop" in starving[0]

    def test_a_village_without_a_capacity_reading_is_skipped_not_guessed(self):
        """The capacity page is only fetched when the crop derivation needs it.
        Where it was not read, the plan must stay quiet rather than invent a cap."""
        body = PlanRequest(
            snapshot=[
                self._village(1, warehouse_capacity=None, granary_capacity=None),
                self._village(2, x=5, warehouse_capacity=None, granary_capacity=None),
            ],
        )

        result = asyncio.run(post_plan(body, SimpleNamespace(id=1)))

        assert not [w for w in result.warnings if "fills its store" in w or "hits the cap" in w]

    def test_route_flows_are_not_counted_twice(self):
        """The two checks take different inputs and must not be crossed.

        store_status works on the post-plan NET rate, with routes folded into a
        continuous average. simulate_day applies the routes itself, as discrete
        dispatches and arrivals. Handing the net rate to the simulation banks
        every delivery twice -- a receiver ends the day holding double what
        arrived -- and invents overflows that do not exist.
        """
        # Everything nets to zero: village 1 grows 8,000/h of lumber and ships
        # all of it; village 2 burns exactly what arrives. Nothing accumulates,
        # so a correctly-wired simulation settles and reports nothing. Counting
        # the deliveries twice cancels village 2's consumption, so it banks
        # 192,000 a day out of nowhere and any finite store fills.
        #
        # Village 1 is given a deep reserve on purpose. Under the doubled wiring
        # its own production also cancels, and with a shallow store it would
        # simply run dry, stop shipping, and hide the very bug this pins.
        quiet = dict(clay_per_hour=0, iron_per_hour=0, crop_per_hour=0)
        body = PlanRequest(
            snapshot=[
                self._village(
                    1,
                    x=0,
                    y=0,
                    lumber_per_hour=8000,
                    lumber_stock=3_000_000,
                    warehouse_capacity=5_000_000,
                    **quiet,
                ),
                self._village(
                    2,
                    x=3,
                    y=0,
                    lumber_per_hour=-8000,
                    lumber_stock=100_000,
                    warehouse_capacity=400_000,
                    **quiet,
                ),
            ],
            allocations={
                "lumber": {1: {"mode": "absolute", "value": 0}, 2: {"mode": "remainder"}},
            },
        )

        result = asyncio.run(post_plan(body, SimpleNamespace(id=1)))

        bogus = [w for w in result.warnings if "hits the cap" in w]
        assert not bogus, f"phantom overflow from double-counted deliveries: {bogus}"

    def test_storage_checks_cost_no_game_requests(self):
        """post_plan takes no Travian session at all, so the whole check runs on
        state the caller already has."""
        for parameter in inspect.signature(post_plan).parameters.values():
            assert getattr(parameter.default, "dependency", None) is not get_travian_session


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

    def test_one_unreadable_crop_village_does_not_fail_the_whole_plan(self):
        """A village whose granary countdown could not be read is excluded from
        crop productions; an allocation pointing at it must be dropped with a
        warning, not 400 the entire account's plan."""
        body = _plan_request(
            {
                "crop": {
                    "20003": {"mode": "absolute", "value": 500},
                    "20011": {"mode": "remainder"},
                }
            }
        )
        # Village 03 keeps its crop rate; village 11's granary was unreadable.
        body.snapshot[0].crop_per_hour = 1000.0
        body.snapshot[1].crop_per_hour = None

        res = asyncio.run(post_plan(body))

        assert any("20011" in w and "ignor" in w for w in res.warnings)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"merchant_base_capacity": 0},
            {"merchant_base_capacity": -100},
            {"trade_office_bonus_per_level": -0.2},
        ],
    )
    def test_merchant_model_overrides_are_validated_as_request_errors(self, overrides):
        """merchant_base_capacity <= 0 must be a validation error (422), not a
        ValueError escaping MerchantModel deep inside the planner as a 500."""
        from pydantic import ValidationError

        payload = _plan_request({}).model_dump()
        payload.update(overrides)

        with pytest.raises(ValidationError):
            PlanRequest.model_validate(payload)

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
