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
from pydantic import ValidationError

from travian_api.services.building_service import BuildingService
from travian_api.services.distribution.allocation import Resource
from travian_api.web.routes import distribution as dist
from travian_api.web.routes.distribution import (
    DayCheckRequest,
    ExecuteRequest,
    ForeignTarget,
    NightProfileRequest,
    PlanRequest,
    VillageConfig,
    VillageSnapshot,
    get_snapshot,
    post_day_check,
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

# Both villages draining: crop derivation needs no capacity, but the capacity
# page is still fetched for storage-overflow safety (see the pricing test).
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

    def test_reports_four_requests_including_capacity_when_nothing_is_filling(self):
        """The capacity page is fetched even on an all-draining account — its
        warehouse figures are the only source for storage overflow safety, so it
        must not be skipped just because crop derivation didn't need it. The
        reported price matches what was actually spent."""
        http = _SnapshotHttp(warehouse=WAREHOUSE_ALL_DRAINING_HTML)

        res = asyncio.run(get_snapshot(_session(http)))

        assert any("capacity" in url for url in http.urls)
        assert len(http.urls) == 4
        assert res.requests_used == 4

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
        # Hun merchants travel at 20 f/h on x1 — the table shipped with 12
        # (Teuton's), overstating every Hun plan's travel time and merchant
        # counts, and potentially flipping feasible routes to over-budget.
        assert (
            asyncio.run(get_snapshot(_session(_SnapshotHttp(), tribe_id=7))).speed_fields_per_hour
            == 20
        )  # Hun

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


class TestForeignTribute:
    """Crop owed to a village outside the account (profile section 7.3).

    Supplied by hand, because nothing in the game tells us about it. It is a
    sink, not a village: it grows nothing, holds nothing and can never ship
    anything, and the crop it is owed has to come out of the account's pool
    rather than appearing from nowhere.
    """

    def _body(self, crop_per_hour=500.0, margin=0.0, **kw):
        body = _plan_request(
            {"crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}},
            crop=3000.0,
        )
        body.foreign_targets = [
            ForeignTarget(
                name="Ally-Keep",
                x=40,
                y=40,
                crop_per_hour=crop_per_hour,
                safety_margin_pct=margin,
                # These tests exercise a tribute that IS shipped by a route, so
                # the target must be one Travian allows a route to (own / WW /
                # alliance-artifact). Ineligible targets are covered separately.
                route_eligible=kw.pop("route_eligible", True),
                **kw,
            )
        ]
        return body

    def test_the_tribute_is_shipped_and_comes_out_of_the_pool(self):
        with_tribute = asyncio.run(post_plan(self._body(crop_per_hour=500.0)))
        without = asyncio.run(
            post_plan(self._body(crop_per_hour=500.0).model_copy(update={"foreign_targets": []}))
        )

        delivered = sum(
            row.cargo.get(Resource.CROP, 0) for row in with_tribute.rows if row.destination < 0
        )
        assert delivered > 0, "the tribute was never actually shipped"

        # It is deducted, not conjured: the remainder village keeps less crop
        # than it would have with no obligation.
        crop_before = next(u for u in without.unallocated if u.resource is Resource.CROP)
        crop_after = next(u for u in with_tribute.unallocated if u.resource is Resource.CROP)
        assert crop_after.unallocated < crop_before.unallocated

    def test_the_safety_margin_ships_above_the_promise(self):
        plain = asyncio.run(post_plan(self._body(crop_per_hour=500.0, margin=0.0)))
        padded = asyncio.run(post_plan(self._body(crop_per_hour=500.0, margin=20.0)))

        def to_target(res):
            return sum(row.cargo.get(Resource.CROP, 0) for row in res.rows if row.destination < 0)

        assert to_target(padded) > to_target(plain)

    def test_a_tribute_can_never_be_asked_to_ship(self):
        """It has no merchants, so any plan that made it an origin would be
        proposing something the game cannot execute."""
        res = asyncio.run(post_plan(self._body()))

        assert not [row for row in res.rows if row.origin < 0]

    def test_sheet_rows_carry_the_targets_name_not_a_negative_id(self):
        """Reported from the UI: foreign villages rendered as '-1' in the plan.

        The frontend resolved names from its own snapshot, which cannot know
        foreign tributes -- their negative ids fell through the lookup and the
        raw id was shown. Names are now resolved server-side on every row, so
        no client needs to know how sink ids are generated.
        """
        res = asyncio.run(post_plan(self._body()))

        tribute_rows = [row for row in res.rows if row.destination < 0]
        assert tribute_rows, "the tribute was never shipped; fixture drifted"
        for row in tribute_rows:
            assert row.destination_name == "Ally-Keep"
            assert row.origin_name, "real villages must carry their name too"

        for shortfall in res.shortfalls:
            assert shortfall.village_name, "shortfalls must be named, never numbered"

    def test_the_cold_start_is_reported(self):
        """The first delivery can take up to a full cycle plus travel (worst case
        being the route created just after its scheduled send time); a tribute
        that silently starts late looks like a broken promise. The warning states
        this as an upper bound on the manual-coverage window, using the minimum
        startup across suppliers as the first-crop bound."""
        res = asyncio.run(post_plan(self._body()))

        cold = [w for w in res.warnings if "first crop can take up to" in w and "Ally-Keep" in w]
        assert cold, f"warnings: {res.warnings}"
        tribute_firsts = [row.first_delivery_hours for row in res.rows if row.destination < 0]
        assert f"{min(tribute_firsts):.1f}h" in cold[0]

    def test_an_unsuppliable_tribute_says_so(self):
        """Promising more crop than the account grows must be reported, not
        quietly under-shipped."""
        res = asyncio.run(post_plan(self._body(crop_per_hour=10_000_000.0)))

        assert any("Ally-Keep" in w for w in res.warnings)
        assert res.shortfalls or not res.feasible

    def test_two_tributes_at_the_same_coords_never_relay_through_each_other(self):
        """Two foreign obligations at identical coordinates are plausible operator
        input. The zero-distance leg between them must not let the crop relay
        adopt a sink as a hub — no route may originate at a foreign (negative) id,
        and the plan may not claim feasible while emitting an impossible row."""
        body = self._body()
        body.foreign_targets = [
            ForeignTarget(name="A", x=40, y=40, crop_per_hour=500.0, route_eligible=True),
            ForeignTarget(name="B", x=40, y=40, crop_per_hour=500.0, route_eligible=True),
        ]
        res = asyncio.run(post_plan(body))
        assert all(row.origin > 0 for row in res.rows), (
            f"a route originates at a foreign sink: {[(r.origin, r.destination) for r in res.rows]}"
        )

    def test_an_ineligible_target_is_a_manual_transfer_not_a_route(self):
        """Travian only allows routes to own / WW / alliance-artifact villages.
        An ordinary foreign village must not be emitted as an executable route;
        it is reported as a manual transfer instead."""
        res = asyncio.run(post_plan(self._body(route_eligible=False)))
        assert not [row for row in res.rows if row.destination < 0], (
            "an ordinary foreign village must not become a Gold Club route row"
        )
        assert any("manual transfer" in w.lower() and "Ally-Keep" in w for w in res.warnings)


class TestOverAllocation:
    def test_over_allocation_is_infeasible_not_a_green_plan(self):
        """Explicit targets exceeding production drive the remainder village's
        target negative — an unsustainable "ship more than you make" allocation.
        The routing may still show diagnostic rows, but the plan must NOT be
        reported feasible, and the over-allocation must be surfaced in words."""
        body = _plan_request(
            {
                "lumber": {
                    # 3000 + 500(remainder floor) far exceeds total 2500/h produced
                    "20003": {"mode": "absolute", "value": 3000},
                    "20011": {"mode": "remainder"},
                }
            }
        )
        res = asyncio.run(post_plan(body))
        assert res.feasible is False, "an over-allocated sheet must not read as feasible"
        assert any("exceed production" in w for w in res.warnings)


class TestWarningsNameVillages:
    """No message a human reads may identify a village by its internal id.

    An id is a database handle. Told that "village 53629 would have to send more
    than it has", the operator has to go and look up which village that even is
    before they can act -- and a warning nobody can act on is barely better than
    no warning at all.
    """

    def test_no_warning_leaks_a_village_id(self):
        # Deliberately provoke several warnings at once: an over-allocated
        # remainder, a sustain on a village that is not in deficit, and a
        # merchant budget that cannot cover what the plan commits.
        body = _plan_request(
            {
                "iron": {
                    "20003": {"mode": "absolute", "value": 50_000},
                    "20011": {"mode": "remainder"},
                },
                "crop": {
                    "20003": {"mode": "sustain", "value": 10},
                    "20011": {"mode": "remainder"},
                },
            },
            merchants_free=2,
            crop=1000.0,
        )

        res = asyncio.run(post_plan(body))

        assert res.warnings, "fixture produced no warnings; the test proves nothing"
        for warning in res.warnings:
            for village in body.snapshot:
                assert str(village.village_id) not in warning, (
                    f"warning identifies a village by id instead of name: {warning}"
                )

    def test_a_village_with_no_name_still_gets_an_intelligible_warning(self):
        """Falling back to the id is fine when there is genuinely no name --
        silently dropping the identity would be worse."""
        body = _plan_request(
            {
                "iron": {
                    "20003": {"mode": "absolute", "value": 50_000},
                    "20011": {"mode": "remainder"},
                }
            }
        )
        for village in body.snapshot:
            village.name = ""

        res = asyncio.run(post_plan(body))

        assert any("village " in w for w in res.warnings)


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

        # The fixture names village 2 "2", so the warning leads with the name.
        starving = [w for w in result.warnings if "runs out" in w and w.startswith("2:")]
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

        dropped = [w for w in res.warnings if "ignor" in w]
        assert dropped, f"the dropped allocation must be reported: {res.warnings}"
        # Named, not numbered. An operator knows village "11"; nobody knows 20011.
        assert any("11" in w for w in dropped)
        assert not any("20011" in w for w in dropped), (
            f"warning leaks the internal village id: {dropped}"
        )

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


class TestPlanRespectsTheProfileWindow:
    """A plan belonging to a part-day profile must be phased into its hours.

    /execute recomputes the plan server-side and turns its rows into REAL trade
    routes, and the captured wire format carries an explicit hour and minute --
    so an unphased send time is not a display quirk. It is a route that fires
    while a different profile is meant to be running.
    """

    WINDOW = (20 * 60, 22 * 60)

    @staticmethod
    def _covers(window, minute):
        start, end = window
        return start <= minute < end if start < end else (minute >= start or minute < end)

    @staticmethod
    def _minutes(clock):
        hours, minutes = clock.split(":")
        return int(hours) * 60 + int(minutes)

    def _plan(self, window):
        body = _plan_request(
            {"lumber": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}}
        )
        if window is not None:
            body = body.model_copy(update={"dispatch_window": window})
        return asyncio.run(post_plan(body))

    def test_every_send_time_lands_inside_a_part_day_window(self):
        res = self._plan(self.WINDOW)

        assert res.rows, "expected at least one route to reason about"
        outside = [
            r.dispatch for r in res.rows if not self._covers(self.WINDOW, self._minutes(r.dispatch))
        ]
        assert not outside, f"send times outside the profile's 20:00-22:00 hours: {outside}"

    def test_omitting_the_window_keeps_the_round_the_clock_behaviour(self):
        # Nothing changed for a caller that sends nothing: the field is optional
        # and /plan still phases across the whole day.
        assert self._plan(None).rows

    def test_a_zero_width_window_is_rejected_not_silently_ignored(self):
        # No minute of the day is inside it, which build_beat refuses outright.
        # Accepting it here would turn a client typo into a 500.
        base = _plan_request({"lumber": {"20011": {"mode": "remainder"}}})
        with pytest.raises(ValidationError, match="zero-width"):
            PlanRequest.model_validate(
                {**base.model_dump(mode="json"), "dispatch_window": (600, 600)}
            )

    def test_a_window_outside_the_day_is_rejected_and_names_its_field(self):
        base = _plan_request({"lumber": {"20011": {"mode": "remainder"}}})
        with pytest.raises(ValidationError, match="dispatch_window minutes must be"):
            PlanRequest.model_validate(
                {**base.model_dump(mode="json"), "dispatch_window": (0, 1440)}
            )


class TestPlanDiagnostics:
    """/plan must answer "should I care?" before it answers "about what?".

    The live 25-village account returned 153 warnings in one flat list and the
    operator refused to read it, which made every one of them worthless -- the
    1.9M/day crop loss included. These tests are about the whole response
    staying honest while it gets shorter: nothing may be dropped from
    ``warnings``, and ``diagnostics`` must be derived from exactly that content.
    """

    # Ids are stand-ins in the same 5-digit shape a real world uses, so a
    # message that leaks one is distinguishable from a message that formats a
    # number -- "1" would appear inside every thousands separator.
    HUB = 20001
    SINK = 20002

    def _village(self, vid, **overrides):
        index = vid - self.HUB + 1
        base = dict(
            village_id=vid,
            name=f"{index:02d}",
            x=(index % 9) * 7 - 30,
            y=(index // 9) * 11 + 6,
            merchants_total=20,
            merchants_free=20,
            lumber_per_hour=827,
            clay_per_hour=926,
            iron_per_hour=891,
            crop_per_hour=3200,
            lumber_stock=799_000,
            clay_stock=799_000,
            iron_stock=799_000,
            crop_stock=200_000,
            warehouse_capacity=800_000,
            granary_capacity=800_000,
        )
        base.update(overrides)
        return base

    def _crowded_account(self, village_count: int = 19) -> PlanRequest:
        """Many identical villages keeping everything, all crop into one hub.

        The shape that produced the flood: every village's warehouse is full and
        nothing ships materials, while the whole account's crop is aimed at one
        granary that cannot hold a fraction of it.
        """
        snapshot = [
            self._village(
                self.HUB, x=0, y=0, warehouse_capacity=2_000_000, granary_capacity=2_000_000
            ),
            self._village(self.SINK, x=8, y=0, crop_per_hour=-2000),
            *[self._village(self.HUB + offset) for offset in range(2, village_count)],
        ]
        allocations = {"crop": {self.SINK: {"mode": "remainder"}}}
        for village in snapshot:
            if village["village_id"] != self.SINK:
                allocations["crop"][village["village_id"]] = {"mode": "absolute", "value": 0}
        return PlanRequest(snapshot=snapshot, allocations=allocations)

    def test_every_warning_is_a_finding_and_every_finding_is_a_warning(self):
        """The page renders `diagnostics` and nothing else, so a warning missing
        from it would be invisible -- silently dropped rather than triaged."""
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))

        grouped = [f.message for g in res.diagnostics.groups for f in g.findings]
        assert sorted(grouped) == sorted(res.warnings)
        assert res.warnings, "the fixture produced no warnings; the test proves nothing"

    def test_the_flood_collapses_to_a_handful_of_groups(self):
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))

        assert len(res.warnings) > 50, "the fixture must reproduce the flood"
        assert len(res.diagnostics.groups) <= 10, (
            f"{len(res.warnings)} warnings still read as "
            f"{len(res.diagnostics.groups)} things to look at"
        )

    def test_villages_losing_the_same_resource_are_one_group_with_a_total(self):
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))

        clay = next(
            g
            for g in res.diagnostics.groups
            if g.resource is Resource.CLAY and g.category == "overflow_structural"
        )
        assert clay.count > 5, "one line per village is the bug, not the fix"
        assert clay.loss_per_day == pytest.approx(sum(f.loss_per_day for f in clay.findings))
        assert str(clay.count) in clay.headline

    def test_the_headline_leads_with_the_total_and_the_worst_offender(self):
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))
        diagnostics = res.diagnostics

        assert diagnostics.total_loss_per_day > 0
        assert f"{diagnostics.total_loss_per_day:,.0f}" in diagnostics.headline
        # Crop into a granary that cannot hold it dwarfs everything else, and it
        # is the line that used to be invisible in the middle of the list.
        assert diagnostics.loss_by_resource[0].resource is Resource.CROP
        assert "crop" in diagnostics.headline

    def test_the_biggest_loss_is_the_first_group(self):
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))
        groups = res.diagnostics.groups

        assert groups[0].loss_per_day == max(g.loss_per_day for g in groups)

    def test_every_group_says_what_to_do(self):
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))

        for group in res.diagnostics.groups:
            assert group.action.strip(), f"{group.category} implies no action"

    def test_the_same_store_is_not_described_twice(self):
        """Both storage checks look at the same store from different angles --
        "full in 1.1h" and "full, costing 22,224/day". Emitting both made half
        the real list a restatement of the other half, so the priced line wins
        and the fill-time note for that store is dropped."""
        res = asyncio.run(post_plan(self._crowded_account(), SimpleNamespace(id=1)))

        capped = {
            (f.village, f.resource)
            for g in res.diagnostics.groups
            if g.category.startswith("overflow_")
            for f in g.findings
        }
        filling = {
            (f.village, f.resource)
            for g in res.diagnostics.groups
            if g.category == "store_filling"
            for f in g.findings
        }
        assert capped, "the fixture must produce overflows"
        assert not capped & filling

    def test_a_timing_note_is_a_note_not_a_warning(self):
        """A tribute's cold-start window is a fact about how Gold Club routes
        fire, not a problem to fix. Mixed in with real losses it dilutes them."""
        body = self._crowded_account(village_count=4)
        body.foreign_targets = [
            ForeignTarget(name="Ally-Keep", x=12, y=9, crop_per_hour=500, route_eligible=True)
        ]

        res = asyncio.run(post_plan(body, SimpleNamespace(id=1)))

        notes = [g for g in res.diagnostics.groups if g.category == "tribute_cold_start"]
        assert notes, f"no cold-start note in {res.warnings}"
        assert all(g.severity == "note" for g in notes)
        assert all(g.loss_per_day == 0 for g in notes)

    def test_a_clean_plan_reports_no_loss_rather_than_an_empty_panel(self):
        """No capacity was fetched, so no store can be judged, and there is
        nothing else to report. The panel still answers the question."""
        body = PlanRequest(
            snapshot=[
                self._village(self.HUB, x=0, y=0, warehouse_capacity=None, granary_capacity=None),
                self._village(self.SINK, x=3, y=0, warehouse_capacity=None, granary_capacity=None),
            ],
            max_latency_hours=None,
        )

        res = asyncio.run(post_plan(body, SimpleNamespace(id=1)))

        assert res.warnings == []
        assert res.diagnostics.total_loss_per_day == 0
        assert res.diagnostics.loss_by_resource == []
        assert res.diagnostics.headline == "No problems found."

    def test_no_finding_leaks_a_village_id(self):
        """Same rule as the prose warnings: an id nobody can act on is worse
        than no message. The structured fields must obey it too."""
        body = self._crowded_account()
        res = asyncio.run(post_plan(body, SimpleNamespace(id=1)))

        for group in res.diagnostics.groups:
            for finding in group.findings:
                for village in body.snapshot:
                    assert str(village.village_id) not in finding.detail, (
                        f"detail identifies a village by id: {finding.detail}"
                    )


def _own_village(vid, name, x, y, *, lumber=0.0, crop=0.0, merchants=20, **overrides):
    """One snapshot row. ``**overrides`` matches the file's other three builders.

    It was the only one without them, so a caller wanting a warehouse capacity
    had to reach into the returned dict and mutate it -- which reads as if the
    capacity were being corrected rather than supplied.
    """
    base = {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": merchants,
        "merchants_free": merchants,
        "lumber_per_hour": lumber,
        "clay_per_hour": 0,
        "iron_per_hour": 0,
        "crop_per_hour": crop,
    }
    base.update(overrides)
    return base


def _findings(res, category):
    """Every finding of one category, flattened out of the grouped view."""
    return [
        finding
        for group in res.diagnostics.groups
        for finding in group.findings
        if finding.category == category
    ]


class TestShipOnlyTo:
    """A per-origin whitelist of OWN destinations.

    The operator's spec says "village 02 must not send to any village except
    03, 18, 14, 13, 01 Hammer". Exclusions were built from `foreign_targets`
    alone, so that could not be said at all -- and unconstrained, the day plan
    had 02 shipping 36,000/h to village 11. `ship_only_to` is a second INPUT
    onto the exclusion the foreign denylist already feeds; the mechanism binds
    the seed, the swaps and the relays, so what these pin is that the whitelist
    reaches it on every planning path and restricts exactly what it says: own
    villages, never foreign targets.
    """

    HUB, NEAR, FAR, SPARE = 20002, 20011, 20003, 20019

    def _payload(self, *, ship_only_to=None, spare=False):
        """`02` has the lumber and sits between `11` and `03`, both wanting it.

        Unconstrained the optimizer feeds both from 02 -- 11 is as close as 03
        and 02 is the only source. With `spare` a second source `19` stands on
        11's far side: a whitelist keeping 02 off 11 then leaves 19 as the one
        village that can cover it, where without `spare` nothing can.
        """
        snapshot = [
            _own_village(self.HUB, "02", 0, 0, lumber=6000),
            _own_village(self.NEAR, "11", -5, 0),
            _own_village(self.FAR, "03", 5, 0),
        ]
        allocations = {
            str(self.HUB): {"mode": "absolute", "value": 0},
            str(self.NEAR): {"mode": "absolute", "value": 3000},
            str(self.FAR): {"mode": "absolute", "value": 3000},
        }
        if spare:
            snapshot.append(_own_village(self.SPARE, "19", -20, 0, lumber=3000))
            allocations[str(self.SPARE)] = {"mode": "absolute", "value": 0}
        config = []
        if ship_only_to is not None:
            config.append({"village_id": self.HUB, "ship_only_to": ship_only_to})
        return {"snapshot": snapshot, "allocations": {"lumber": allocations}, "config": config}

    def _plan(self, **kw):
        return asyncio.run(post_plan(PlanRequest.model_validate(self._payload(**kw))))

    @staticmethod
    def _lumber(res, origin=None, destination=None):
        return sum(
            row.cargo.get(Resource.LUMBER, 0)
            for row in res.rows
            if (origin is None or row.origin == origin)
            and (destination is None or row.destination == destination)
        )

    def test_the_fixture_really_does_tempt_the_optimizer(self):
        # Guards the fixture: if the unconstrained plan did not put 02 onto 11,
        # the whitelist tests below would pass while forbidding nothing.
        res = self._plan(spare=True)
        assert self._lumber(res, origin=self.HUB, destination=self.NEAR) > 0, (
            "fixture no longer creates the temptation"
        )

    @pytest.mark.parametrize("spare", [False, True])
    def test_no_row_leaves_the_origin_for_a_village_off_the_list(self, spare):
        res = self._plan(ship_only_to=[self.FAR], spare=spare)

        strayed = [(r.origin, r.destination) for r in res.rows if r.origin == self.HUB]
        assert all(destination == self.FAR for _, destination in strayed), strayed

    def test_the_listed_destination_still_receives_from_it(self):
        # The whitelist restricts; it does not silence the village.
        res = self._plan(ship_only_to=[self.FAR])

        assert self._lumber(res, origin=self.HUB, destination=self.FAR) == pytest.approx(3000)

    def test_demand_it_can_no_longer_serve_is_met_elsewhere(self):
        res = self._plan(ship_only_to=[self.FAR], spare=True)

        assert self._lumber(res, origin=self.SPARE, destination=self.NEAR) == pytest.approx(3000)
        assert not res.shortfalls, "19 could cover 11; nothing is short"

    def test_demand_nobody_else_can_serve_is_a_shortfall_not_a_silence(self):
        res = self._plan(ship_only_to=[self.FAR])

        short = [s for s in res.shortfalls if s.village_id == self.NEAR]
        assert short and short[0].village_name == "11", res.shortfalls
        assert self._lumber(res, destination=self.NEAR) + short[0].per_hour == pytest.approx(3000)

    def test_the_shortfall_blames_the_whitelist_and_not_the_account(self):
        """ "No village has surplus left to cover this demand" is false here: 02
        has 3,000/h of it left and the whitelist is what keeps it off 11. Told
        the false version, the operator goes looking for lumber production when
        the fix is one line of their own config."""
        res = self._plan(ship_only_to=[self.FAR])

        short = [s for s in res.shortfalls if s.village_id == self.NEAR]
        assert short, res.shortfalls
        assert "excluded" in short[0].reason, short[0].reason
        assert "02" in short[0].reason, "the origin whose list to edit must be named"

    def test_an_unknown_destination_is_refused_and_named(self):
        with pytest.raises(HTTPException) as exc:
            self._plan(ship_only_to=[self.FAR, 99999])

        assert exc.value.status_code == 422
        assert "99999" in exc.value.detail
        assert "02" in exc.value.detail, "the origin whose list is wrong must be named"

    def test_foreign_targets_keep_their_own_denylist(self):
        """`ship_only_to` is about OWN villages. A tribute without
        `exclude_origins` must still be fed by the whitelisted village -- the
        operator already has a lever for foreign targets, and an empty
        whitelist silently starving every tribute would be a second one
        nobody asked for."""
        body = _plan_request(
            {"crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}},
            crop=3000.0,
        )
        body.foreign_targets = [
            ForeignTarget(name="Ally-Keep", x=40, y=40, crop_per_hour=500.0, route_eligible=True)
        ]
        body.config = [VillageConfig(village_id=20003, ship_only_to=[])]

        res = asyncio.run(post_plan(body))

        tribute_rows = [row for row in res.rows if row.destination < 0]
        assert tribute_rows, "the tribute was never shipped"
        assert {row.origin for row in tribute_rows} == {20003}
        assert not [row for row in res.rows if row.origin == 20003 and row.destination == 20011]

    def test_the_whitelist_binds_crop_as_well_as_the_materials(self):
        """A deliberate DECISION, pinned so it is not flipped by accident.

        Exempting crop was considered: crop starvation kills troops, where a
        material shortfall only slows building. It was rejected. The operator's
        own spec sentence -- "02 must not send to any village except 03, 18,
        14, 13, 01 Hammer" -- names the ARMY village in the list, which is
        direct evidence they expect the restriction to bind crop and have
        already accounted for it. An exemption would silently overrule a
        declared restriction, and it would be invisible: nothing in the picker,
        the file or the response would say why 02 shipped crop somewhere its
        list forbids. What the operator gets instead is a shortfall that names
        the whitelist as the cause, which is the honest failure mode.
        """
        body = _plan_request(
            {"crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}},
            crop=3000.0,
        )
        body.config = [VillageConfig(village_id=20003, ship_only_to=[])]

        res = asyncio.run(post_plan(body))

        assert not [row for row in res.rows if row.origin == 20003], (
            "the whitelist let crop out of a village restricted to nobody"
        )
        assert VillageConfig.model_fields["ship_only_to"].description is not None
        assert "crop" in VillageConfig.model_fields["ship_only_to"].description, (
            "the field must say the restriction covers crop; a reader who assumes "
            "materials-only writes a config that starves an army village"
        )

    def test_a_tribute_fed_from_a_restricted_village_is_reported(self):
        """The exemption above is right but silent on the plan that used it.

        `ship_only_to` exists for merchant thrift, and a distant tribute is the
        most expensive destination on the map -- so a village restricted to
        nobody being put on a tribute haul is precisely the outcome the operator
        was trying to avoid, arrived at by a rule they cannot see from the
        picker. The rule stays (extending the whitelist to tributes would make
        "keeps paying the tribute" unexpressible: `ship_only_to` takes own
        village ids and a target has none). The plan says it happened.
        """
        body = _plan_request(
            {"crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}},
            crop=3000.0,
        )
        body.foreign_targets = [
            ForeignTarget(name="Ally-Keep", x=40, y=40, crop_per_hour=500.0, route_eligible=True)
        ]
        body.config = [VillageConfig(village_id=20003, ship_only_to=[])]

        res = asyncio.run(post_plan(body))

        flagged = _findings(res, "whitelist_vs_tribute")
        assert flagged, [g.category for g in res.diagnostics.groups]
        assert "Ally-Keep" in flagged[0].message
        assert "03" in flagged[0].message, "the restricted origin must be named"

    def test_a_one_merchant_haul_is_not_reported_as_one_merchants(self):
        """This finding is the operator's only sight of a rule the village picker
        cannot show them, and the canonical case is a single merchant.

        Asserted on both the message and the detail, because the detail is what
        the grouped headline quotes as "worst: ...".
        """
        body = _plan_request(
            {"crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}},
            crop=3000.0,
        )
        body.foreign_targets = [
            ForeignTarget(name="Ally-Keep", x=40, y=40, crop_per_hour=500.0, route_eligible=True)
        ]
        body.config = [VillageConfig(village_id=20003, ship_only_to=[])]

        res = asyncio.run(post_plan(body))

        flagged = _findings(res, "whitelist_vs_tribute")
        assert len(flagged) == 1
        assert [row.merchants for row in res.rows if row.destination < 0] == [1]
        assert "on 1 merchant." in flagged[0].message, flagged[0].message
        assert flagged[0].detail.endswith("1 merchant"), flagged[0].detail

    def test_an_unrestricted_village_feeding_a_tribute_is_not_reported(self):
        """No whitelist, nothing surprising -- and a finding raised on every
        tribute would be noise in a list the operator already stopped reading
        once."""
        body = _plan_request(
            {"crop": {"20003": {"mode": "absolute", "value": 0}, "20011": {"mode": "remainder"}}},
            crop=3000.0,
        )
        body.foreign_targets = [
            ForeignTarget(name="Ally-Keep", x=40, y=40, crop_per_hour=500.0, route_eligible=True)
        ]

        res = asyncio.run(post_plan(body))

        assert [row for row in res.rows if row.destination < 0], "the tribute was never shipped"
        assert not _findings(res, "whitelist_vs_tribute")

    def _crosswise(self, *, whitelist):
        """`_crosswise_plan` from test_tribute_supplier_choice, as a request.

        `hub` is one field from `near` and `spare` one field from `other`, so the
        cheap pairing is hub->near. Keeping hub off `near` forces the seed to
        cross the two hauls at ~100 fields each, and uncrossing them is a large
        improvement the 2x2 swap will take unless the whitelist binds the swap
        too. All four are OWN villages -- the case a foreign target's
        `exclude_origins` could never express.
        """
        snapshot = [
            _own_village(1, "hub", 0, 0, crop=5000, merchants=40),
            _own_village(2, "spare", 100, 0, crop=6000, merchants=40),
            _own_village(8, "other", 101, 0, merchants=40),
            _own_village(9, "near", 1, 0, merchants=40),
        ]
        allocations = {
            "1": {"mode": "absolute", "value": 0},
            "2": {"mode": "absolute", "value": 0},
            "8": {"mode": "absolute", "value": 5000},
            "9": {"mode": "absolute", "value": 6000},
        }
        body = PlanRequest.model_validate(
            {
                "snapshot": snapshot,
                "allocations": {"crop": allocations},
                "config": [{"village_id": 1, "ship_only_to": [8]}] if whitelist else [],
                "max_latency_hours": None,
                "map_span": 401,
                "speed_fields_per_hour": 12.0,
            }
        )
        return asyncio.run(post_plan(body))

    def test_the_crosswise_fixture_really_does_tempt_the_search(self):
        res = self._crosswise(whitelist=False)
        assert 1 in {r.origin for r in res.rows if r.destination == 9}, (
            "fixture no longer creates the temptation"
        )

    def test_a_swap_may_not_land_the_origin_on_a_village_off_the_list(self):
        res = self._crosswise(whitelist=True)

        assert {r.origin for r in res.rows if r.destination == 9} == {2}
        assert not res.shortfalls, "the demand is coverable without hub"

    def test_the_day_check_plans_every_profile_under_the_same_whitelist(self):
        """The day check and the segmented execute plan each profile through
        `_plan_account`; a whitelist honoured by /plan alone would leave the
        day picture built from routes the operator forbade."""
        payload = self._payload(ship_only_to=[self.FAR])
        body = DayCheckRequest.model_validate(
            {
                "snapshot": payload["snapshot"],
                "config": payload["config"],
                "segments": [
                    {
                        "name": "Day",
                        "window": [8 * 60, 20 * 60],
                        "allocations": payload["allocations"],
                    }
                ],
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        assert any("Day: 11 is short" in w for w in res.warnings), res.warnings


class TestStockFundedSupply:
    """Stock-funded supply: a village ships more than it makes, from a warehouse
    it keeps topped up by NPC.

    The operator's spec: "02 always performs NPC trading ... Treat 02's
    warehouses as always at least 30% full, meaning even though the total wood
    account is negative, 02 has extra wood to distribute." The planner refused
    that day profile as over-allocated by 12,210/h of lumber because it drew
    every village's supply from production alone. `stock_floor_fraction` names
    the stock; the allowance it funds over the profile window is extra supply
    of lumber, clay and iron -- never crop, and never production.
    """

    HUB, NEAR, FAR = 20002, 20011, 20003
    DAY = [7 * 60, 23 * 60]  # 16 hours: 0.30 x 1,200,000 / 16 = 22,500/h

    def _payload(self, *, floor=0.30, capacity=1_200_000, hub_crop=20_000.0, near=10_000, far=8000):
        """02 makes 6,000/h of lumber against 18,000/h claimed: short by 12,000.

        A 1,200,000 warehouse kept 30% full is 360,000 in stock, 22,500/h over
        the 16-hour day -- enough to fund the shortfall with room to spare.
        Lower the claims below 6,000 and nothing is short, so the allowance goes
        undrawn.
        """
        snapshot = [
            _own_village(
                self.HUB,
                "02",
                0,
                0,
                lumber=6000,
                crop=hub_crop,
                merchants=200,
                warehouse_capacity=capacity,
            ),
            _own_village(self.NEAR, "11", -5, 0),
            _own_village(self.FAR, "03", 5, 0),
        ]
        allocations = {
            "lumber": {
                str(self.HUB): {"mode": "remainder"},
                str(self.NEAR): {"mode": "absolute", "value": near},
                str(self.FAR): {"mode": "absolute", "value": far},
            }
        }
        config = []
        if floor is not None:
            config.append({"village_id": self.HUB, "stock_floor_fraction": floor})
        return {
            "snapshot": snapshot,
            "allocations": allocations,
            "config": config,
            "dispatch_window": self.DAY,
            "prune_to_window": True,
        }

    def _plan(self, **kw):
        return asyncio.run(post_plan(PlanRequest.model_validate(self._payload(**kw))))

    @staticmethod
    def _findings(res, category):
        return [
            f
            for group in res.diagnostics.groups
            if group.category == category
            for f in group.findings
        ]

    def test_the_fixture_really_is_over_allocated_without_a_floor(self):
        res = self._plan(floor=None)

        assert res.feasible is False
        assert any("exceed production" in w for w in res.warnings), res.warnings

    def test_a_stock_floor_makes_the_operators_day_profile_feasible(self):
        res = self._plan()

        assert res.feasible is True, res.warnings
        assert not any("exceed production" in w for w in res.warnings), res.warnings
        assert not res.shortfalls

    def test_the_reported_production_does_not_move(self):
        """The stock is supply, not production: inflating the production figure
        would have the response claim the account makes more than it does."""
        without = next(
            u for u in self._plan(floor=None).unallocated if u.resource is Resource.LUMBER
        )
        with_floor = next(u for u in self._plan().unallocated if u.resource is Resource.LUMBER)

        assert with_floor.total_production == without.total_production == 6000
        assert with_floor.total_supplement == pytest.approx(22_500)
        assert without.total_supplement == 0

    def test_stock_funded_shipping_is_a_visible_dependency(self):
        res = self._plan()

        funded = self._findings(res, "stock_funded")
        assert len(funded) == 1, res.warnings
        assert funded[0].severity == "warning"
        assert funded[0].village == "02"
        assert "02 ships 12,000/h of lumber beyond its production" in funded[0].message
        assert "30% full" in funded[0].message

    def test_an_allowance_that_is_not_drawn_on_is_not_reported(self):
        # 1,000 + 2,000 claimed against 6,000 made: production alone covers it.
        res = self._plan(near=1000, far=2000)
        assert not any("exceed production" in w for w in res.warnings)

        assert self._findings(res, "stock_funded") == [], res.warnings
        assert self._findings(res, "stock_floor_unsustainable") == []

    def test_a_floor_drawn_faster_than_the_crop_surplus_will_not_hold(self):
        """NPC converts crop to materials one for one, so the stock can only be
        replenished as fast as the village's crop surplus."""
        res = self._plan(hub_crop=5000.0)

        unsustainable = self._findings(res, "stock_floor_unsustainable")
        assert len(unsustainable) == 1, res.warnings
        assert unsustainable[0].severity == "warning"
        assert "02's stock floor is drawn down 7,000/h faster" in unsustainable[0].message
        assert "will not hold" in unsustainable[0].message

    def test_a_crop_surplus_that_covers_the_draw_is_not_warned_about(self):
        res = self._plan(hub_crop=20_000.0)

        assert self._findings(res, "stock_floor_unsustainable") == [], res.warnings

    def test_a_floor_without_a_capacity_reading_is_refused_and_named(self):
        with pytest.raises(HTTPException) as exc:
            self._plan(capacity=None)

        assert exc.value.status_code == 422
        assert "02" in exc.value.detail
        assert "capacit" in exc.value.detail

    @pytest.mark.parametrize("fraction", [-0.1, 0.96, 1.0])
    def test_the_fraction_is_bounded(self, fraction):
        with pytest.raises(ValidationError):
            VillageConfig(village_id=1, stock_floor_fraction=fraction)

    def test_the_day_check_honours_the_floor(self):
        """Each profile is planned through `_plan_account`, so the day picture
        must be built from the same stock-funded routes /plan shows."""
        payload = self._payload()
        body = DayCheckRequest.model_validate(
            {
                "snapshot": payload["snapshot"],
                "config": payload["config"],
                "prune_to_window": True,
                "segments": [
                    {"name": "Day", "window": self.DAY, "allocations": payload["allocations"]}
                ],
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        assert not any("exceed production" in w for w in res.warnings), res.warnings
        assert any("Day: 02 ships 12,000/h of lumber" in w for w in res.warnings), res.warnings


class TestTheMerchantModelSaysWhenItIsUnpinned:
    """`EUROPE2_TEUTON` is measured on one end only, and nothing said so.

    The base was re-read as 2,500 on 2026-09-02, superseding a 7,920-at-TO-13
    reading that fitted base 2,200 exactly. The +20%-per-level bonus is carried
    over from the profile and has never been measured against the new base -- so
    every capacity the plan computes for a village with a Trade Office rests on
    an unverified multiplier. By this module's own rule, understating capacity
    over-provisions merchants (safe) while overstating it breaches the merchant
    budget invisibly, so being wrong here is wrong in the unsafe direction, and
    the operator had no way to know the model was unpinned.

    A Trade Office 0 village settles the base with no inversion at all --
    `calibrate` prefers exactly that sample -- so the finding names the TO 0
    villages in the snapshot, which is the one reading that would close it.
    """

    def _plan(self, *, levels, bonus=None):
        payload = {
            "snapshot": [
                {
                    "village_id": vid,
                    "name": name,
                    "x": x,
                    "y": 0,
                    "merchants_total": 20,
                    "merchants_free": 20,
                    "lumber_per_hour": rate,
                    "clay_per_hour": 0,
                    "iron_per_hour": 0,
                    "crop_per_hour": 0,
                }
                for vid, name, x, rate in ((20003, "03", 0, 3000), (20026, "26", 10, 0))
            ],
            "allocations": {
                "lumber": {
                    "20003": {"mode": "absolute", "value": 0},
                    "20026": {"mode": "remainder"},
                }
            },
            "config": [
                {"village_id": vid, "trade_office_level": level} for vid, level in levels.items()
            ],
        }
        if bonus is not None:
            payload["trade_office_bonus_per_level"] = bonus
        return asyncio.run(post_plan(PlanRequest.model_validate(payload)))

    def test_a_trade_office_village_on_the_default_bonus_is_flagged(self):
        res = self._plan(levels={20003: 13, 20026: 0})

        flagged = _findings(res, "merchant_model_uncalibrated")
        assert flagged, [g.category for g in res.diagnostics.groups]
        assert "26" in flagged[0].message, "the TO 0 village that would settle it must be named"

    def test_an_account_with_no_trade_office_anywhere_is_not_flagged(self):
        """At level 0 the capacity IS the base, so the unmeasured multiplier is
        never applied and nothing about the plan depends on it."""
        res = self._plan(levels={20003: 0, 20026: 0})

        assert not _findings(res, "merchant_model_uncalibrated")

    def test_a_measured_bonus_is_taken_at_its_word(self):
        """The finding is about the DEFAULT carried over from the profile. An
        operator who calibrated and sent their own number has already done the
        thing it asks for."""
        res = self._plan(levels={20003: 13, 20026: 0}, bonus=0.175)

        assert not _findings(res, "merchant_model_uncalibrated")


class TestConsumptionProfiles:
    """Section 2: per-village consumption targets, entered as their own number.

    An allocation target was doing two jobs -- what must LAND at a village, and
    what the village then holds -- and the storage layer read the survivor as
    permanent accumulation. On the operator's own account that produced
    6,422,904 resources a day of reported loss and 27 CRITICAL findings, every
    one of them exactly ``target x 24``: the planner assuming an army village
    stockpiles what it in fact spends. So `clean` was unreachable while the
    account ran correctly.

    ``VillageConfig.consumption_per_hour`` is that second number. It changes
    the store's net and nothing else: the cargo is still target minus own
    production (spec 2.2, known issue #1).

    Note for whoever lands P9: a WRONG consumption figure now silences a real
    overflow. The drift flag is its detector -- these findings going quiet is
    only trustworthy while the declared profile matches the account.
    """

    HUB, ARMY = 20002, 20001
    # The army village lands 5,000/h of lumber because it burns 5,000/h.
    BURN = 5_000.0

    def _payload(
        self, *, consumption=None, hub_consumption=None, target=BURN, army_own=0.0, tribute=False
    ):
        snapshot = [
            _own_village(
                self.HUB,
                "02",
                0,
                0,
                lumber=20_000,
                merchants=200,
                lumber_stock=2_000_000,
                warehouse_capacity=5_000_000,
                granary_capacity=5_000_000,
            ),
            _own_village(
                self.ARMY,
                "01",
                4,
                0,
                lumber=army_own,
                lumber_stock=40_000,
                warehouse_capacity=80_000,
                granary_capacity=80_000,
            ),
        ]
        config = [{"village_id": self.HUB}, {"village_id": self.ARMY}]
        if consumption is not None:
            config[1]["consumption_per_hour"] = consumption
        if hub_consumption is not None:
            config[0]["consumption_per_hour"] = hub_consumption
        payload = {
            "snapshot": snapshot,
            "config": config,
            "allocations": {
                "lumber": {
                    str(self.HUB): {"mode": "remainder"},
                    str(self.ARMY): {"mode": "absolute", "value": target},
                }
            },
        }
        if tribute:
            # A route-eligible foreign target joins the optimizer as a
            # pseudo-village with a NEGATIVE id, which is how `village_nets`
            # came to carry a "store" for something that has none.
            payload["foreign_targets"] = [
                {
                    "name": "Ally-Keep",
                    "x": 8,
                    "y": 0,
                    "crop_per_hour": 500.0,
                    "route_eligible": True,
                }
            ]
        return payload

    def _plan(self, **kw):
        return asyncio.run(post_plan(PlanRequest.model_validate(self._payload(**kw))))

    @staticmethod
    def _overflow(res, name):
        return [
            f
            for f in _findings(res, "overflow_structural") + _findings(res, "overflow_burst")
            if f.village == name
        ]

    def test_without_consumption_the_target_reads_as_a_daily_loss(self):
        """The artifact, reproduced at the endpoint before it is removed:
        5,000/h landing becomes 120,000/day 'lost at the store cap'."""
        res = self._plan()
        lost = self._overflow(res, "01")

        assert lost, "expected the phantom overflow this feature removes"
        assert sum(f.loss_per_day for f in lost) == pytest.approx(self.BURN * 24, abs=1.0)
        assert res.verdict.clean is False

    def test_declaring_the_consumption_silences_it(self):
        """Target equals consumption: the store is level, so there is no loss to
        report. Nothing was weakened -- the arithmetic changed.

        Both villages declare, because both genuinely spend: 02 makes 20,000/h,
        sends 5,000/h on and burns the 15,000/h it keeps. That is the state the
        operator says the account is in, and `clean` was unreachable while it
        was in it -- which is the whole complaint this feature answers.
        """
        res = self._plan(consumption={"lumber": self.BURN}, hub_consumption={"lumber": 15_000})

        assert self._overflow(res, "01") == []
        assert self._overflow(res, "02") == []
        assert res.diagnostics.total_loss_per_day == pytest.approx(0.0)
        assert res.verdict.clean is True, res.verdict.unweighed

    def test_the_hubs_own_surplus_is_still_reported(self):
        """Only what is DECLARED goes quiet. 02 keeping 15,000/h it never spends
        is a real overflow and stays one -- consumption is not a mute button."""
        res = self._plan(consumption={"lumber": self.BURN})

        assert self._overflow(res, "01") == []
        hub = self._overflow(res, "02")
        assert hub, "the remainder village really does accumulate 15,000/h"
        assert sum(f.loss_per_day for f in hub) == pytest.approx(15_000 * 24, abs=1.0)

    def test_a_real_surplus_still_overflows_at_the_surplus_rate(self):
        """Over-supply is a genuine finding and must survive, reported at the
        rate of the SURPLUS rather than of the target."""
        res = self._plan(target=self.BURN, consumption={"lumber": 3_000})
        lost = self._overflow(res, "01")

        assert lost, "a village landing more than it spends does overflow"
        assert sum(f.loss_per_day for f in lost) == pytest.approx((5_000 - 3_000) * 24, abs=1.0)

    def test_the_cargo_is_still_the_gap_not_the_target(self):
        """Spec 2.2. A consuming village that produces some of its own still
        receives only the difference."""
        res = self._plan(army_own=1_200.0, consumption={"lumber": self.BURN})
        inbound = [r for r in res.rows if r.destination == self.ARMY]

        assert inbound, res.warnings
        landing = sum(
            amount / row.cycle_hours
            for row in inbound
            for resource, amount in row.cargo.items()
            if resource is Resource.LUMBER
        )
        assert landing == pytest.approx(self.BURN - 1_200.0, rel=0.02)

    def test_r7_still_fires_when_the_village_really_is_draining(self):
        """Section 9: 01 is permanently crop-negative by design and starving.
        The whole point of R7 is that a store emptying gets a countdown.

        This case used to declare `consumption={"crop": 9_000}` against a
        positive crop reading, which the ruling on R3-D1 now refuses: the
        snapshot's `crop_per_hour` is NET of upkeep already, so the drain IS
        the reading. Restated against the field that carries it -- a negative
        net crop -- which is the shape a real starving village arrives in.
        """
        payload = self._payload()
        payload["snapshot"][1]["crop_per_hour"] = -8_000.0
        payload["snapshot"][1]["crop_stock"] = 20_000

        res = asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        starving = [w for w in res.warnings if "runs out" in w and w.startswith("01:")]
        assert starving, f"no starvation warning in {res.warnings}"
        assert "crop" in starving[0]

    def test_a_crop_spend_is_refused_because_the_snapshot_already_nets_it(self):
        """The ruling on R3-D1. `crop_per_hour` is derived by
        `get_all_villages_net_crop` and is net of upkeep by construction, so a
        declared crop spend subtracts the same troops twice -- which deleted a
        REAL 204,456/day overflow on the operator's own account.

        Refused at the schema, so it cannot reach any planning path.
        """
        with pytest.raises(ValidationError) as exc:
            PlanRequest.model_validate(self._payload(consumption={"crop": 9_000}))

        detail = str(exc.value)
        assert "already net" in detail
        assert "crop_ceiling" not in detail  # points at the TARGET, not the alert
        assert "target" in detail

    def test_a_crop_spend_beside_a_material_one_is_still_refused(self):
        """No partial acceptance: a map with one bad key is a map the operator
        must correct, not one the planner silently trims."""
        with pytest.raises(ValidationError):
            PlanRequest.model_validate(
                self._payload(consumption={"lumber": self.BURN, "crop": 9_000})
            )

    def test_the_plan_exposes_the_net_the_grid_used_to_recompute(self):
        """R3-D7. `VillageAllocation.net_per_hour` had no production reader, so
        the page redid `target - consumption` in JavaScript. The plan now says
        it, and every part of the figure travels with it so the grid never has
        to derive any of it."""
        res = self._plan(consumption={"lumber": self.BURN}, hub_consumption={"lumber": 15_000})

        army = next(
            n
            for n in res.village_nets
            if n.village_id == self.ARMY and n.resource is Resource.LUMBER
        )
        assert army.target_per_hour == pytest.approx(self.BURN)
        assert army.consumption_per_hour == pytest.approx(self.BURN)
        assert army.own_per_hour == pytest.approx(0.0)
        assert army.ship_per_hour == pytest.approx(self.BURN)
        # Target equals spend, so the store is LEVEL -- the whole point of the
        # feature, and now a number the page is handed rather than one it works
        # out for itself.
        assert army.net_per_hour == pytest.approx(0.0)

        hub = next(
            n
            for n in res.village_nets
            if n.village_id == self.HUB and n.resource is Resource.LUMBER
        )
        assert hub.net_per_hour == pytest.approx(hub.target_per_hour - 15_000)

    def test_the_exposed_net_is_the_allocation_layers_own_figure(self):
        """Not a second implementation of the same sum: every row must equal
        `VillageAllocation.net_per_hour`, which is what stops the two drifting
        the way the backend and the grid could."""
        res = self._plan(consumption={"lumber": self.BURN})

        for row in res.village_nets:
            assert row.net_per_hour == pytest.approx(
                row.own_per_hour
                + row.supplement_per_hour
                + row.ship_per_hour
                - row.consumption_per_hour
            )
            assert row.target_per_hour == pytest.approx(
                row.own_per_hour + row.supplement_per_hour + row.ship_per_hour
            )

    def test_every_planned_village_and_resource_gets_a_row(self):
        """R4-P3-3. With a tribute in the account too: `village_nets` is
        documented as what one VILLAGE'S STORE does, and a foreign target has
        no store -- it is a sink that grows nothing and holds nothing. It
        reached the optimizer as a pseudo-village with a negative id and came
        back out as a row reading `own 0 / target 500 / net 500`, which a
        server-side reader (P2's templates, P6's fill floor) would take for a
        village accumulating 500/h forever."""
        res = self._plan(tribute=True)

        assert {(n.village_id, n.resource) for n in res.village_nets} == {
            (vid, resource) for resource in Resource for vid in (self.HUB, self.ARMY)
        }

    def test_the_tribute_is_still_planned_it_just_has_no_store(self):
        """The other half, so the filter above cannot pass by dropping the
        obligation: a shortfall is about the OBLIGATION rather than a store, and
        that channel still names the target by its synthetic id."""
        res = self._plan(tribute=True)

        assert [(s.village_id, s.village_name) for s in res.shortfalls] == [(-1, "Ally-Keep")]

    def test_no_declared_spend_can_be_dropped_for_an_unreadable_rate(self):
        """R3-D8 reported that a spend on a resource with an unreadable rate is
        set aside in silence. R3-D1's ruling closed it, and this is the guard
        that keeps it closed.

        The reason is structural, so it is asserted rather than argued: the
        three material rates are plain floats in `VillageSnapshot`, so
        `productions` always holds every village for them, and `crop_per_hour`
        -- the ONE nullable rate, and the case the reviewer reproduced -- can no
        longer be declared as a spend at all.

        If P14 makes a material rate nullable, this test fails, and the silent
        drop below `_resolve_roles` must then be given the voice D8 asked
        for before that change can land.
        """
        for resource in (Resource.LUMBER, Resource.CLAY, Resource.IRON):
            annotation = VillageSnapshot.model_fields[f"{resource.value}_per_hour"].annotation
            assert annotation is float, (
                f"{resource.value}_per_hour became nullable: a declared spend can now be "
                f"dropped in silence again, so R3-D8 is reopened"
            )
        assert VillageSnapshot.model_fields["crop_per_hour"].annotation == float | None
        assert Resource.CROP in VillageConfig.model_fields["consumption_per_hour"].description

    def test_every_declared_spend_reaches_the_planner(self, monkeypatch):
        """The same closure, observed instead of reasoned: what the operator
        typed is what `craft_plan` is handed, with nothing filtered out."""
        seen = {}
        real = dist.craft_plan

        def spy(villages, productions, allocations, config, supplements, consumption):
            seen["consumption"] = consumption
            return real(villages, productions, allocations, config, supplements, consumption)

        monkeypatch.setattr(dist, "craft_plan", spy)
        payload = self._payload(consumption={"lumber": self.BURN, "clay": 250, "iron": 125})
        asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        assert seen["consumption"] == {
            Resource.LUMBER: {self.ARMY: self.BURN},
            Resource.CLAY: {self.ARMY: 250.0},
            Resource.IRON: {self.ARMY: 125.0},
        }

    def test_the_field_binds_all_four_planning_paths(self):
        """The standing rule, made checkable.

        `consumption_per_hour` must reach `/plan`, `/day-check`, `/execute`
        AND `/night-profile`. It became four rather than three because the
        night endpoint inherits the field from `PlanRequest` and SEEDS the
        other three -- the page writes its derived allocations into the active
        profile -- yet ignored the figure entirely (R3-D2).

        What this pins is the refusal every one of the four shares, so none can
        quietly stop carrying the field again. Each path's own behaviour is
        exercised where it lives: `/plan` and `/day-check` in this class,
        `/execute` in TestConsumptionReachesTheThirdPlanningPath, and
        `/night-profile` in TestConsumptionReachesTheFourthPlanningPath.
        """
        paths = (PlanRequest, DayCheckRequest, ExecuteRequest, NightProfileRequest)

        def shaped(**kw):
            """`/day-check` carries its allocations per segment, not at the top
            level, so the same body has to be restated for it. Nothing about
            the consumption field changes -- it lives on `config` either way."""
            payload = self._payload(**kw)
            if model is DayCheckRequest:
                allocations = payload.pop("allocations")
                payload |= {
                    "prune_to_window": False,
                    "segments": [
                        {"name": "All day", "window": [0, 1439], "allocations": allocations}
                    ],
                }
            return payload

        for model in paths:
            carried = model.model_validate(shaped(consumption={"lumber": self.BURN}))
            spend = [c.consumption_per_hour for c in carried.config if c.consumption_per_hour]
            assert spend == [{Resource.LUMBER: self.BURN}], (
                f"{model.__name__} did not carry the declared spend"
            )

            with pytest.raises(ValidationError, match="already net"):
                model.model_validate(shaped(consumption={"crop": 9_000}))

    def test_a_crop_spend_of_zero_is_refused_too(self):
        """Zero is a claim, not silence -- and the claim is still about a figure
        the snapshot already applied."""
        with pytest.raises(ValidationError):
            PlanRequest.model_validate(self._payload(consumption={"crop": 0}))

    def test_an_empty_consumption_map_plans_identically_to_none(self):
        """`{}` and absent are ONE state, not two.

        Named for what it pins, because the old name -- "byte for byte the old
        plan" -- claimed a guarantee it cannot give: both sides of this
        comparison run today's code, so a change that neutered consumption
        entirely would leave the two equal and this test green (it did, under
        mutation M4). What it does pin is real and worth pinning: the file, the
        request and the input all have to treat a cleared profile as silence,
        or one of them starts claiming something the others do not.

        The BYTE-FOR-BYTE guard against the pre-P1 planner is the frozen
        fixture in tests/test_distribution_golden.py, which is compared to
        recorded output rather than to another run of the same code.
        """
        absent = self._plan()
        empty = self._plan(consumption={})

        assert empty.model_dump() == absent.model_dump()

    def test_a_consumption_for_a_village_not_in_the_snapshot_is_refused(self):
        payload = self._payload()
        payload["config"].append({"village_id": 999, "consumption_per_hour": {"lumber": 10}})

        with pytest.raises(HTTPException) as exc:
            asyncio.run(post_plan(PlanRequest.model_validate(payload)))

        assert exc.value.status_code == 422
        assert "999" in str(exc.value.detail)

    def test_a_negative_consumption_is_refused(self):
        """Not read as extra production. Inferring consumption from a rate's
        sign was the rejected alternative: the statistics page reports materials
        gross, so a consuming material village reads positive."""
        with pytest.raises(HTTPException) as exc:
            self._plan(consumption={"lumber": -500})

        assert exc.value.status_code == 400
        assert "consumption cannot be negative" in str(exc.value.detail)

    def test_an_unknown_resource_key_is_refused_by_the_schema(self):
        with pytest.raises(ValidationError):
            PlanRequest.model_validate(self._payload(consumption={"gold": 500}))

    def test_the_day_check_agrees_with_the_plan_on_a_consuming_village(self):
        """Both endpoints share `_plan_account`, and the composite replay needs
        the same second number: a parameter threaded into one simulation and not
        the other is how the two came to answer the same account differently."""
        payload = self._payload(
            consumption={"lumber": self.BURN}, hub_consumption={"lumber": 15_000}
        )
        allocations = payload.pop("allocations")

        plan = asyncio.run(
            post_plan(PlanRequest.model_validate(payload | {"allocations": allocations}))
        )
        day = asyncio.run(
            post_day_check(
                DayCheckRequest.model_validate(
                    payload
                    | {
                        "prune_to_window": False,
                        "segments": [
                            {"name": "All day", "window": [0, 1439], "allocations": allocations}
                        ],
                    }
                )
            )
        )

        army = [
            row
            for row in day.villages
            if row.village_id == self.ARMY and row.resource is Resource.LUMBER
        ]
        assert army, "the army village has no lumber trajectory"
        assert army[0].daily_net == pytest.approx(0.0, abs=self.BURN)
        assert plan.diagnostics.total_loss_per_day == pytest.approx(0.0)
        assert not [w for w in day.warnings if "hits its store cap" in w and w.startswith("01:")]
