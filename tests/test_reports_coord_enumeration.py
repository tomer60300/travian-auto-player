"""The coords the operator holds intel on must be enumerable, and honest.

The analyzer's phase 1A already unions report-derived coords, but it does so
inside a scoring run — so an operator rebuilding farm lists could not ask the
question on its own. `list_unique_target_coords` lifts it out.

Two properties matter more than the enumeration itself:

* It inherits `fetch_report_batch_metadata`'s refusal. `0331c54` changed that
  batch to RAISE instead of returning `{}`, precisely so a caller cannot read
  "the request never landed" as "there is nothing there". A coord enumeration
  that swallowed a failed batch would hand back a SHORT list that looks
  complete, and the operator would drop targets they still hold intel on. The
  exception has to travel, and the route has to turn it into a 502.
* The fan-out is bounded at the edge. One GET walks up to `max_pages` report
  pages plus a batched GraphQL call per 250 reports, and every one of those is
  a real request to the game. `max_pages` is capped at 10 to match
  `list_reports`, and `max_age_hours` at 720 (30 days) — the draft this came
  from left the window unbounded behind a 100-page cap.

The client here never reaches the network: `fetch_reports_robust` is replaced
on the instance (page-walking has its own tests), while the GraphQL batch runs
for real against a fake `post_json`, so the batching and the raise are the
code under test rather than a stub of it.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from travian_api.exceptions import ReportError
from travian_api.services.reports_service import ReportsService
from travian_api.web.auth import get_current_user
from travian_api.web.sessions import get_travian_session

COORDS = "/api/reports/coords"


def _report(rid, report_type):
    return SimpleNamespace(report_id=rid, report_type=report_type)


def _village(x, y):
    return {"defender": {"village": {"x": x, "y": y}}}


def _aliased(*villages):
    """One GraphQL batch answer, keyed by the aliases the service builds."""
    return {"data": {f"r{i}": v for i, v in enumerate(villages)}}


def _service(reports, answers):
    """A service whose page walk is canned and whose GraphQL batch is real.

    *answers* is one entry per expected batch: an `_aliased(...)` payload, or
    an exception `post_json` should raise.
    """
    pending = list(answers)

    async def post_json(url, payload, **kwargs):
        answer = pending.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = SimpleNamespace(
        post_json=post_json,
        base_url="https://example.invalid",
        settings=SimpleNamespace(base_url="https://example.invalid"),
    )
    svc = ReportsService(client)

    async def robust(max_age_hours=None, max_pages=100):
        return reports, 1, 0, []

    svc.fetch_reports_robust = robust
    return svc


def test_only_scout_and_battle_reports_contribute_coords():
    """A trade or adventure report names a defender village too, and that
    village is not a target — carrying it through would put the operator's own
    trade partners and the hero's adventure tiles on a raid candidate list."""
    reports = [
        _report("r1", "battle"),
        _report("r2", "trade"),
        _report("r3", "scout"),
        _report("r4", "adventure"),
    ]
    svc = _service(reports, [_aliased(_village(1, 1), _village(2, 2))])

    assert asyncio.run(svc.list_unique_target_coords()) == [(1, 1), (2, 2)]


def test_the_coords_are_distinct_and_sorted():
    """Twenty raids on one village are twenty reports and one coord. The
    operator unions this with their farm-list coords, so a stable order makes
    the two comparable without re-sorting at every call site."""
    reports = [_report(f"r{i}", "battle") for i in range(1, 6)]
    svc = _service(
        reports,
        [
            _aliased(
                _village(9, -3),
                _village(-5, 12),
                _village(9, -3),
                _village(-5, 12),
                _village(0, 0),
            )
        ],
    )

    assert asyncio.run(svc.list_unique_target_coords()) == [(-5, 12), (0, 0), (9, -3)]


def test_a_village_the_game_named_no_coords_for_is_left_out():
    """A metadata row that arrived but carries no village — or a village with
    no x/y — is not a coord. Reading either as 0 would put the map centre, or
    a whole axis of it, on the operator's target list."""
    reports = [_report("r1", "battle"), _report("r2", "battle"), _report("r3", "battle")]
    svc = _service(reports, [_aliased(_village(4, 4), {"defender": {}}, {})])

    assert asyncio.run(svc.list_unique_target_coords()) == [(4, 4)]


def test_a_failed_metadata_batch_raises_rather_than_returning_a_short_list():
    """The second of two batches never lands. Returning the first batch's
    coords would be a list that looks complete while missing half the
    account's intel — the exact confusion `0331c54` closed one level down by
    making the batch raise. It has to keep raising through here."""
    reports = [_report(f"r{i}", "battle") for i in range(300)]
    svc = _service(
        reports,
        [_aliased(*[_village(i, i) for i in range(250)]), RuntimeError("connection reset")],
    )

    with pytest.raises(ReportError) as exc:
        asyncio.run(svc.list_unique_target_coords())
    assert "connection reset" in str(exc.value)


# ── the route ────────────────────────────────────────────────────────────


@pytest.fixture
def client():
    """The real ASGI app with auth and the Travian session replaced.

    Overriding `get_travian_session` short-circuits its `get_current_user` and
    `get_db` sub-dependencies, so this endpoint needs no database — hence
    `TestClient(app)` without its context manager, and no lifespan.
    """
    from travian_api.web.app import app

    calls: list[dict] = []

    async def list_unique_target_coords(max_age_hours, max_pages):
        calls.append({"max_age_hours": max_age_hours, "max_pages": max_pages})
        return [(3, -7), (11, 2)]

    session = SimpleNamespace(
        reports_service=SimpleNamespace(list_unique_target_coords=list_unique_target_coords)
    )
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[get_travian_session] = lambda: session
    yield TestClient(app), calls
    app.dependency_overrides.clear()


def test_the_coord_route_returns_the_coords_the_service_found(client):
    """Also pins the route ORDER: `/coords` is declared before `/{report_id}`,
    so without that ordering this request resolves as a fetch of the report
    whose id is the string "coords" — a wasted game request that 404s."""
    c, calls = client

    response = c.get(COORDS, params={"max_age_hours": 48, "max_pages": 3})

    assert response.status_code == 200
    assert response.json() == {"coords": [{"x": 3, "y": -7}, {"x": 11, "y": 2}]}
    assert calls == [{"max_age_hours": 48, "max_pages": 3}]


@pytest.mark.parametrize(
    "params",
    [
        {"max_pages": 0},
        {"max_pages": 11},
        {"max_pages": 100},
        {"max_age_hours": 0},
        {"max_age_hours": 721},
    ],
)
def test_the_coord_route_refuses_a_fan_out_beyond_its_bounds(client, params):
    """Every page and every batch behind this GET is a request to the game, so
    the ceiling belongs on the wire and not in the caller's good manners. 10
    pages matches `list_reports`; 720 hours is the 30-day window the service
    defaults to."""
    c, calls = client

    assert c.get(COORDS, params=params).status_code == 422
    assert calls == [], "a refused request must not reach the game"
