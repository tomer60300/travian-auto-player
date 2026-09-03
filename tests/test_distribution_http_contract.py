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
