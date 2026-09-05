"""The distribution endpoints over real HTTP, not called as Python functions.

Every other distribution test in this suite calls `dist.post_plan(...)` or
`dist.post_execute(...)` directly. That is fast and it is how the planner logic
gets exercised, but it skips the half of FastAPI that only runs on the wire:

* `response_model` validation. A field the handler returns that the declared
  model does not have -- or a value of the wrong type -- is dropped or raises
  ONLY when FastAPI serialises the response. In-process the handler just returns
  its object and every assertion about it passes, so a broken response contract
  is invisible to 1,700 green tests and produces a 500 on the operator's first
  real click.
* request parsing and validation error SHAPE. A 422 from a validator reaches the
  UI as a JSON body it has to render; in-process a pydantic ValidationError is
  raised as an exception instead.
* the auth dependency, the router prefix, and the JSON round trip of every enum,
  tuple and float the models carry.

These are contract tests, not logic tests: the planner is exercised with the
smallest input that produces a real answer, and what is asserted is that the
wire response validates and carries the fields the frontend reads. Live writes
are never possible here -- `dry_run` is set on every execute, and the suite-wide
pin keeps TRAVIAN_TRADE_ROUTE_LIVE false regardless.
"""

import pytest
from fastapi.testclient import TestClient

from travian_api.web.auth import get_current_user

from .test_distribution_audit import USER


@pytest.fixture(scope="module")
def client():
    """The real ASGI app with only the auth dependency replaced.

    Overriding auth rather than minting a token keeps this a test of the
    DISTRIBUTION contract: a change to login has its own tests, and should not
    be able to turn every endpoint test here red.
    """
    from travian_api.web.app import app

    app.dependency_overrides[get_current_user] = lambda: USER
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _village(vid, name, x, y, *, crop=2000):
    return {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": 20,
        "merchants_free": 20,
        "lumber_per_hour": 2000,
        "clay_per_hour": 1000,
        "iron_per_hour": 1000,
        "crop_per_hour": crop,
        "lumber_stock": 10000,
        "clay_stock": 10000,
        "iron_stock": 10000,
        "crop_stock": 10000,
        "warehouse_capacity": 80000,
        "granary_capacity": 80000,
    }


def _plan_body(**extra):
    return {
        "snapshot": [_village(1, "hub", 0, 0), _village(2, "farm", 20, 0, crop=9000)],
        "config": [
            {"village_id": 1, "trade_office_level": 10},
            {"village_id": 2, "trade_office_level": 10},
        ],
        "allocations": {
            "crop": {
                "1": {"mode": "remainder", "value": 0},
                "2": {"mode": "absolute", "value": 0},
            }
        },
        **extra,
    }


def _segments_body(**extra):
    """A whole-day body: allocations live inside each segment, never top level.

    The endpoint refuses a top-level `allocations` alongside `segments` -- it
    would be silently ignored otherwise -- so the first version of this test,
    which reused _plan_body, was correctly rejected with a 422. Kept as a
    separate helper so the distinction stays visible.
    """
    body = _plan_body(**extra)
    body.pop("allocations", None)
    return body


class TestThePlanContract:
    def test_plan_serialises_through_its_response_model(self, client):
        res = client.post("/api/distribution/plan", json=_plan_body())

        assert res.status_code == 200, res.text
        body = res.json()
        # Exactly what ResourcePlanner.jsx dereferences off the plan response --
        # grepped from the page rather than guessed, because the wire model's
        # names differ from the internal plan object's (it is `feasible` here,
        # not `is_feasible`, and budgets rather than merchants_committed). That
        # divergence is the whole reason a contract test earns its place.
        for field in (
            "rows",
            "warnings",
            "feasible",
            "budgets",
            "shortfalls",
            "diagnostics",
            "total_merchants",
            "village_nets",
        ):
            assert field in body, f"{field} missing from the wire response: {sorted(body)}"

    def test_a_row_carries_everything_the_sheet_renders(self, client):
        res = client.post("/api/distribution/plan", json=_plan_body())
        rows = res.json()["rows"]

        assert rows, "the fixture must produce at least one route"
        for field in ("origin", "destination", "cargo", "cycle_hours", "merchants"):
            assert field in rows[0], f"{field} missing from a plan row: {sorted(rows[0])}"

    def test_resource_keys_survive_json(self, client):
        """Cargo is keyed by a Resource enum in Python and by a string on the
        wire. A change to the enum's value would rename every key the frontend
        indexes by, silently."""
        res = client.post("/api/distribution/plan", json=_plan_body())

        cargo = res.json()["rows"][0]["cargo"]
        assert set(cargo) <= {"lumber", "clay", "iron", "crop"}, (
            f"unexpected cargo keys on the wire: {sorted(cargo)}"
        )

    def test_a_declared_consumption_reaches_the_planner_over_the_wire(self, client):
        """VillageConfig does not forbid extra fields, so a name the backend does
        not know is DROPPED in silence -- the frontend would go on sending a
        spend nobody applied, and every consuming village would keep reading as
        stockpiling its whole allocation. Only a real request can catch that,
        which is what this file is for.

        Posted twice on one body: the same account with and without the field.
        The figures must differ, and the direction must be down.
        """
        body = _plan_body()
        without = client.post("/api/distribution/plan", json=body)
        assert without.status_code == 200, without.text

        spending = _plan_body()
        # The hub is the material remainder, so it absorbs the account's whole
        # lumber surplus and its warehouse fills. Say that it spends that
        # surplus. Materials only: the crop half of this figure was refused by
        # the ruling on R3-D1, because the snapshot already nets crop.
        spending["config"][0]["consumption_per_hour"] = {"lumber": 2000}
        with_spend = client.post("/api/distribution/plan", json=spending)

        assert with_spend.status_code == 200, with_spend.text
        before = without.json()["diagnostics"]["total_loss_per_day"]
        after = with_spend.json()["diagnostics"]["total_loss_per_day"]
        assert before > 0, "the fixture must overflow without a declared spend"
        assert after < before, f"the declared spend never reached the plan: {before} -> {after}"

    def test_the_net_the_grid_reads_travels_over_the_wire(self, client):
        """R3-D7. The allocation grid reads `village_nets[].net_per_hour` from
        the JSON instead of recomputing `target - spend` in JavaScript, so the
        field names and the resource keys are part of the contract. A rename
        here silently sends the page back to its own arithmetic."""
        body = _plan_body()
        body["config"][0]["consumption_per_hour"] = {"lumber": 2000}

        res = client.post("/api/distribution/plan", json=body)

        assert res.status_code == 200, res.text
        nets = res.json()["village_nets"]
        assert nets, "the grid has nothing to read"
        assert set(nets[0]) == {
            "village_id",
            "resource",
            "own_per_hour",
            # Two figures where there was one `supplement_per_hour`: section 7's
            # allowance is a CEILING and the draw is what the plan spent of it,
            # and the grid's `ship = target - own - draw` needs the second one.
            "npc_allowance_per_hour",
            "npc_draw_per_hour",
            "target_per_hour",
            "ship_per_hour",
            "consumption_per_hour",
            "net_per_hour",
        }
        assert {n["resource"] for n in nets} <= {"lumber", "clay", "iron", "crop"}
        spender = body["config"][0]["village_id"]
        row = next(n for n in nets if n["village_id"] == spender and n["resource"] == "lumber")
        assert row["consumption_per_hour"] == pytest.approx(2000)
        assert row["net_per_hour"] == pytest.approx(row["target_per_hour"] - 2000)

    def test_a_stock_floor_with_a_window_and_no_attendance_is_a_422(self, client):
        """Section 7. Over the wire, because this is the refusal the operator's
        night profile depends on and a 500 or a silent default would both be
        worse than the 422."""
        body = _plan_body(dispatch_window=[420, 1380], prune_to_window=True)
        body["config"][0]["stock_floor_fraction"] = 0.30
        body["snapshot"][0]["warehouse_capacity"] = 1_200_000

        res = client.post("/api/distribution/plan", json=body)

        assert res.status_code == 422, res.text
        assert "npc_attended" in res.text

    def test_the_npc_tables_travel_over_the_wire(self, client):
        """`npc_reserves` and `npc_triggers` are what the page renders instead
        of parsing the prose, so their field names are part of the contract."""
        body = _plan_body(dispatch_window=[420, 1380], prune_to_window=True, npc_attended=True)
        body["config"][0]["stock_floor_fraction"] = 0.30
        body["snapshot"][0]["warehouse_capacity"] = 1_200_000

        res = client.post("/api/distribution/plan", json=body)

        assert res.status_code == 200, res.text
        reserves = res.json()["npc_reserves"]
        assert len(reserves) == 1, reserves
        assert set(reserves[0]) == {
            "village_id",
            "village_name",
            "floor_level",
            "allowance_per_day",
            "allowance_per_hour",
            "feedstock",
            "feedstock_shares",
            "drawn",
        }
        assert reserves[0]["floor_level"] == pytest.approx(0.30 * 1_200_000)
        # The hub holds no wood at all against a 360,000 floor, so section 7's
        # first trigger is due.
        triggers = res.json()["npc_triggers"]
        assert [t["kind"] for t in triggers] == ["wood_low"], triggers
        assert set(triggers[0]) == {
            "village_id",
            "village_name",
            "kind",
            "resource",
            "level",
            "threshold",
            "projected",
        }

    def test_a_consumption_under_an_unknown_resource_is_a_readable_422(self, client):
        body = _plan_body()
        body["config"][0]["consumption_per_hour"] = {"gold": 500}

        res = client.post("/api/distribution/plan", json=body)

        assert res.status_code == 422, res.text
        assert "gold" in res.text

    def test_a_crop_consumption_is_a_readable_422_over_the_wire(self, client):
        """R3-D1's ruling, at the endpoint the frontend actually posts to. The
        operator has to be told WHY, because their spec lists a crop figure per
        role village and the answer is to enter it as a target instead."""
        body = _plan_body()
        body["config"][0]["consumption_per_hour"] = {"lumber": 2000, "crop": 11000}

        res = client.post("/api/distribution/plan", json=body)

        assert res.status_code == 422, res.text
        assert "already net" in res.text
        assert "allocation target" in res.text

    def test_a_windowed_plan_round_trips_its_window(self, client):
        res = client.post(
            "/api/distribution/plan",
            json=_plan_body(dispatch_window=[1380, 420], prune_to_window=True),
        )

        assert res.status_code == 200, res.text
        assert res.json()["rows"], "a windowed plan produced no routes"


class TestTheExecuteContract:
    def test_a_dry_run_serialises_through_its_response_model(self, client):
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(dry_run=True, max_routes_per_run=5),
        )

        assert res.status_code == 200, res.text
        body = res.json()
        for field in ("actions", "created", "warnings", "problems"):
            assert field in body, f"{field} missing from the execute response: {sorted(body)}"

    def test_the_request_forecast_reaches_the_wire(self, client):
        """The preview panel shows this before the operator commits. It is
        computed only on dry runs, so it is exactly the kind of field that can
        be dropped by a response model without any in-process test noticing."""
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(dry_run=True, max_routes_per_run=5),
        )

        forecast = res.json().get("requests_forecast")
        assert forecast is not None, "requests_forecast did not survive serialisation"
        for field in ("marketplace_reads", "creates", "estimated_total"):
            assert field in forecast, f"{field} missing from the forecast: {sorted(forecast)}"

    def test_a_whole_day_segments_request_is_accepted_on_the_wire(self, client):
        """`segments` is the newest and least-exercised part of the request
        model, and the one the operator's whole-day run depends on."""
        res = client.post(
            "/api/distribution/execute",
            json=_segments_body(
                dry_run=True,
                prune_to_window=True,
                max_routes_per_run=5,
                segments=[
                    {"name": "Day", "window": [420, 1380], "allocations": {}},
                    {"name": "Night", "window": [1380, 420], "allocations": {}},
                ],
            ),
        )

        assert res.status_code == 200, res.text
        assert "actions" in res.json()

    def test_overlapping_segments_are_refused_as_readable_json(self, client):
        """A validator rejection has to arrive as a body the UI can show, not as
        an unhandled exception. In-process this path raises instead."""
        res = client.post(
            "/api/distribution/execute",
            json=_segments_body(
                dry_run=True,
                prune_to_window=True,
                segments=[
                    {"name": "A", "window": [420, 1380], "allocations": {}},
                    {"name": "B", "window": [600, 1000], "allocations": {}},
                ],
            ),
        )

        assert res.status_code == 422, res.text
        assert "detail" in res.json(), "a refusal with no detail tells the operator nothing"


class TestConsentToWriteIsNamedNotInferred:
    """`dry_run: false` is the ABSENCE of a preview, not the presence of consent.

    A field whose default makes a preview and whose falsity makes a live write
    puts the whole safety of this endpoint on one boolean that a client can flip
    by accident -- a stale form, a JSON serialiser that emits every default, a
    hand-typed curl. `execution_mode` is the canonical control and it is
    positive: nothing writes unless the request SAYS "live". `dry_run` survives
    for older callers, but it never decides, and the two disagreeing is a 422
    rather than a guess about which one the operator meant.

    The env brake (TRAVIAN_TRADE_ROUTE_LIVE) is unchanged and still answers 409:
    it says whether this server may write at all, which is a different question
    from what this request asked for. The suite pins it false, so a request that
    gets past the matrix below stops there.
    """

    def test_a_body_with_neither_field_previews(self, client):
        res = client.post("/api/distribution/execute", json=_plan_body(max_routes_per_run=5))

        assert res.status_code == 200, res.text
        assert res.json()["dry_run"] is True

    def test_preview_with_dry_run_true_previews(self, client):
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(execution_mode="preview", dry_run=True, max_routes_per_run=5),
        )

        assert res.status_code == 200, res.text
        assert res.json()["dry_run"] is True

    def test_dry_run_false_alone_is_refused_and_names_the_field(self, client):
        """The whole point: an old client that only knows `dry_run` cannot start
        a live run by omission of a preview."""
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(dry_run=False, max_routes_per_run=5),
        )

        assert res.status_code == 422, res.text
        detail = str(res.json()["detail"])
        assert "execution_mode" in detail, detail
        assert "dry_run alone is not consent" in detail, detail

    def test_live_with_dry_run_true_is_refused_as_contradictory(self, client):
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(execution_mode="live", dry_run=True, max_routes_per_run=5),
        )

        assert res.status_code == 422, res.text
        detail = str(res.json()["detail"])
        assert "execution_mode" in detail and "dry_run" in detail, detail

    @pytest.mark.parametrize("extra", [{}, {"dry_run": False}])
    def test_live_takes_the_live_branch_and_never_previews(self, client, extra):
        """`execution_mode: live` resolves to a live run -- with or without an
        explicit `dry_run: false` -- so the request leaves the preview path.

        There is no connected session on this ASGI fixture, so the first gate it
        meets is the session one and the answer is 403. That is the assertion
        worth making here: a 200 would mean it previewed. The env brake (409 on
        TRAVIAN_TRADE_ROUTE_LIVE) is the NEXT gate and is pinned in
        `test_distribution_execute`, where a session exists to reach it.
        """
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(execution_mode="live", max_routes_per_run=5, **extra),
        )

        assert res.status_code == 403, res.text
        assert "Not connected" in str(res.json()["detail"])

    def test_an_unknown_mode_is_refused(self, client):
        res = client.post(
            "/api/distribution/execute",
            json=_plan_body(execution_mode="LIVE", max_routes_per_run=5),
        )

        assert res.status_code == 422, res.text


class TestTheRunHistoryContract:
    def test_run_history_serialises(self, client):
        res = client.get("/api/distribution/run-history", params={"limit": 5})

        assert res.status_code == 200, res.text
        body = res.json()
        assert "runs" in body and "rollup" in body, sorted(body)

    def test_the_rollup_carries_the_fields_the_panel_reads(self, client):
        res = client.get("/api/distribution/run-history", params={"limit": 5})

        rollup = res.json()["rollup"]
        for field in ("runs", "total_created", "total_problems", "repeat_problem_villages"):
            assert field in rollup, f"{field} missing from the rollup: {sorted(rollup)}"


class TestAuthIsActuallyRequired:
    def test_every_distribution_endpoint_refuses_an_unauthenticated_caller(self):
        """The override above makes every test in this file authenticated, which
        would happily hide an endpoint that forgot its dependency. So this one
        builds a client WITHOUT the override."""
        from travian_api.web.app import app

        saved = dict(app.dependency_overrides)
        app.dependency_overrides.clear()
        try:
            with TestClient(app) as anon:
                for method, path in (
                    ("post", "/api/distribution/plan"),
                    ("post", "/api/distribution/execute"),
                    ("post", "/api/distribution/night-profile"),
                    ("get", "/api/distribution/run-history"),
                ):
                    res = anon.get(path) if method == "get" else anon.post(path, json={})
                    assert res.status_code in (401, 403), (
                        f"{method.upper()} {path} answered {res.status_code} with no "
                        f"credentials -- it is reachable by anyone who can see the port"
                    )
        finally:
            app.dependency_overrides.update(saved)
