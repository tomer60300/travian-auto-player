"""Section 10's output order: the readable plan first, then the YAML.

    Output order: readable plan first -> operator confirms -> then generate
    YAML/code.

The whole point of that ordering is that the file describes the plan the
operator READ. Nothing on this server holds a computed plan -- `/plan` is pure
and stateless by design, so tuning a target costs no game requests -- which
leaves exactly two ways to render one as YAML, and only one of them is honest:

* trust a plan the client posts back. That is what `/execute` refuses to do
  ("the server recomputes the exact plan rather than trust client-sent rows"),
  and the argument is stronger here, not weaker: a document the operator keeps
  as the record of a decision must be the planner's own output, or a stale tab
  produces an authoritative-looking file describing nothing.
* recompute from the same inputs -- the planner is a pure, deterministic
  function of the request, with no clock and no randomness in it -- and REFUSE
  unless the caller hands back the digest of the plan they were shown.

So the export re-plans, and the digest is what makes that safe rather than
silent: a plan that moved between the reading and the confirmation comes back
409 naming both digests, never a document. These tests are the proof of that
and of the document's own contract: every section present, and byte-identical
output for the same plan, because a diffable artefact is most of the value.

Over real HTTP rather than by calling the handler, for the reasons
tests/test_distribution_http_contract.py gives: this endpoint answers with a
media type and headers instead of a response model, and none of that runs
in-process.
"""

import pytest
import yaml
from fastapi.testclient import TestClient

from travian_api.web.auth import get_current_user

from .test_distribution_audit import USER

PLAN = "/api/distribution/plan"
EXPORT = "/api/distribution/plan/yaml"


@pytest.fixture(scope="module")
def client():
    """The real ASGI app with only auth replaced, and deliberately no lifespan.

    Both endpoints under test are auth-only and pure -- they never touch the
    database or a Travian session -- so `TestClient(app)` is used WITHOUT its
    context manager, which is what skips startup. That is not a shortcut: the
    suite shares one SQLite path across xdist workers, so every extra app
    lifespan is another `create_all` racing the others, and one has already been
    seen to lose with "table users already exists". A client that needs no
    database should not open one.
    """
    from travian_api.web.app import app

    app.dependency_overrides[get_current_user] = lambda: USER
    yield TestClient(app)
    app.dependency_overrides.clear()


def _village(vid, name, x, y, *, crop=2000, lumber=2000, cap=400_000):
    return {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": 20,
        "merchants_free": 20,
        "lumber_per_hour": lumber,
        "clay_per_hour": 1000,
        "iron_per_hour": 1000,
        "crop_per_hour": crop,
        "lumber_stock": 10_000,
        "clay_stock": 10_000,
        "iron_stock": 10_000,
        "crop_stock": 10_000,
        "warehouse_capacity": cap,
        "granary_capacity": cap,
    }


def _body(**extra):
    """Two villages, one shipping its crop surplus to the other.

    Trade Office 0 everywhere on purpose: any level with the default bonus
    raises the merchant-calibration warning, which is a real finding and would
    make "a plan with no findings" impossible to construct below.
    """
    body = {
        "snapshot": [
            _village(1, "02", 0, 0),
            _village(2, "01", 20, 0, crop=9000),
        ],
        "config": [{"village_id": 1}, {"village_id": 2}],
        "allocations": {
            "crop": {
                "1": {"mode": "remainder", "value": 0},
                "2": {"mode": "absolute", "value": 0},
            }
        },
    }
    body.update(extra)
    return body


def _quiet_body():
    """One village producing nothing: a real plan with nothing to report.

    The document has to render an empty finding list as readably as a full one,
    and an account with no production has no store to overflow, no granary to
    empty and no route to cost merchants.
    """
    village = _village(1, "02", 0, 0, crop=0, lumber=0)
    village["clay_per_hour"] = 0
    village["iron_per_hour"] = 0
    return {"snapshot": [village], "config": [{"village_id": 1}], "allocations": {}}


def _digest(client, body):
    res = client.post(PLAN, json=body)
    assert res.status_code == 200, res.text
    return res.json()["plan_digest"]


def _export(client, body, digest=None):
    return client.post(
        EXPORT, json={**body, "expected_plan_digest": digest or _digest(client, body)}
    )


def _document(client, body, digest=None):
    res = _export(client, body, digest)
    assert res.status_code == 200, res.text
    return yaml.safe_load(res.text)


class TestTheExportIsBoundToTheConfirmedPlan:
    def test_plan_returns_a_digest_of_what_it_showed(self, client):
        res = client.post(PLAN, json=_body())

        assert res.status_code == 200, res.text
        digest = res.json()["plan_digest"]
        assert len(digest) == 64 and digest == digest.lower()

    def test_the_same_inputs_digest_the_same(self, client):
        assert _digest(client, _body()) == _digest(client, _body())

    def test_a_digest_from_another_plan_is_refused(self, client):
        """The plan moved between the reading and the confirmation."""
        other = _body(snapshot=[_village(1, "02", 0, 0), _village(2, "01", 20, 0, crop=15_000)])
        stale = _digest(client, other)

        res = _export(client, _body(), stale)

        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert stale in detail, detail
        assert _digest(client, _body()) in detail, detail

    def test_a_malformed_digest_is_refused_before_anything_is_planned(self, client):
        res = client.post(EXPORT, json={**_body(), "expected_plan_digest": "not-a-digest"})

        assert res.status_code == 422, res.text

    def test_the_confirmation_is_required(self, client):
        res = client.post(EXPORT, json=_body())

        assert res.status_code == 422, res.text

    def test_the_document_carries_the_digest_it_was_confirmed_with(self, client):
        digest = _digest(client, _body())

        res = _export(client, _body(), digest)

        assert res.status_code == 200, res.text
        assert res.headers["x-plan-digest"] == digest
        assert yaml.safe_load(res.text)["meta"]["plan_digest"] == digest


class TestTheDocument:
    def test_it_is_served_as_a_downloadable_yaml_file(self, client):
        res = _export(client, _body())

        assert res.status_code == 200, res.text
        assert res.headers["content-type"].startswith("application/yaml")
        assert "attachment" in res.headers["content-disposition"]

    def test_every_section_is_present(self, client):
        doc = _document(client, _body())

        assert set(doc) == {
            "meta",
            "verdict",
            "routes",
            "villages",
            "relays",
            "shortfalls",
            "unallocated",
            "npc",
            "night_overruns",
            "role_deviations",
            "findings",
            "inputs",
        }
        assert doc["meta"]["document"] == "travian-distribution-plan"
        assert doc["meta"]["version"] >= 1

    def test_the_routes_carry_the_whole_decision(self, client):
        route = _document(client, _body())["routes"][0]

        assert route["origin"] == 2 and route["origin_name"] == "01"
        assert route["destination"] == 1 and route["destination_name"] == "02"
        assert route["cargo"]["crop"] > 0
        assert route["cycle_hours"] >= 1
        assert ":" in route["dispatch"] and ":" in route["arrival"]
        assert route["merchants"] >= 1

    def test_the_per_village_figures_are_there(self, client):
        villages = _document(client, _body())["villages"]

        row = next(v for v in villages if v["village_id"] == 2)
        assert row["name"] == "01"
        assert row["merchants"]["committed"] >= 1
        crop = next(r for r in row["resources"] if r["resource"] == "crop")
        assert crop["own_per_hour"] == 9000.0
        assert crop["ship_per_hour"] < 0

    def test_the_findings_are_there_with_their_action(self, client):
        findings = _document(client, _body())["findings"]

        assert findings["headline"]
        assert findings["groups"], findings
        group = findings["groups"][0]
        assert group["severity"] in {"critical", "warning", "note"}
        assert group["action"]
        assert group["findings"][0]["message"]

    def test_a_plan_with_nothing_to_report_still_renders_the_section(self, client):
        findings = _document(client, _quiet_body())["findings"]

        assert findings["groups"] == []
        assert findings["total_loss_per_day"] == 0.0
        assert findings["headline"]

    def test_the_inputs_are_the_plan_request_verbatim(self, client):
        """Self-describing a month later: the inputs re-plan to the same plan."""
        doc = _document(client, _body())

        assert "expected_plan_digest" not in doc["inputs"]
        assert _digest(client, doc["inputs"]) == doc["meta"]["plan_digest"]

    def test_it_carries_the_current_npc_and_night_fields(self, client):
        text = _export(client, _body()).text

        assert "npc_allowance_per_hour" in text
        assert "npc_draw_per_hour" in text
        assert "supplement_per_hour" not in text
        doc = yaml.safe_load(text)
        assert set(doc["npc"]) == {"reserves", "triggers"}
        assert doc["night_overruns"] == []

    def test_the_same_plan_renders_byte_identical_yaml(self, client):
        first = _export(client, _body())
        second = _export(client, _body())

        assert first.content == second.content

    def test_there_is_no_timestamp_to_make_two_exports_differ(self, client):
        text = _export(client, _body()).text

        assert "exported_at" not in text
        assert "generated_at" not in text
