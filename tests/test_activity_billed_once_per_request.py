"""N game requests, of any kind, cost exactly N bills against the ceiling.

Billing moved into ``HttpClient._billed`` so that every request the transport
issues is charged once, from a ``finally``. What that fix left behind was an
overlap: ``trade_route_service`` still ran its own ``_log_activity`` around each
write and around both marketplace reads, so a trade-route request was billed
twice -- once for the service call's whole wall clock, once for the request
underneath. Over-billing a ceiling stops the account early rather than late, so
the direction was safe, but "how much have I spent today" was simply wrong, and
the error grew with every route.

The property this file pins is the one that survives a refactor: count the
requests, count the bills, and they agree. It is stated at the transport seam --
the fake's request methods bill exactly as ``_billed`` does -- because that is
where billing now lives, and a test that pins it anywhere else stops meaning
anything the moment a service stops doing its own accounting.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.exceptions import NetworkError
from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    ExistingRoute,
    PlannedRoute,
    TradeRoutePayloadUnverified,
    TradeRouteService,
)

from .activity_billing import billing

EMPTY_MARKETPLACE = (
    "<html><body><script>window.Travian.React.TradeRoutes.render("
    '{viewData: {"ownPlayer":{"village":{"marketplace":{"tradeRoutes":[]}}}}}'
    ");</script></body></html>"
)

EMPTY_READBACK = {
    "data": {
        "ownPlayer": {
            "id": 1,
            "currentVillageId": 20003,
            "village": {"marketplace": {"tradeRoutes": []}},
        }
    }
}


class _BillingTransport:
    """A fake client that bills once per request, like the real one.

    Failures are injected as *data* (``refusals``), not by replacing a request
    method: replacing one removes its bill, and the whole point here is to count
    bills against requests.
    """

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.bills: list[float] = []
        self.refusals: dict[str, Exception] = {}
        self.settings = SimpleNamespace(base_url="https://example.invalid")
        self.human_delay = SimpleNamespace(wait=self._wait)
        self.activity_scheduler = SimpleNamespace(log_activity=self.bills.append)
        bill = billing(self.bills)
        self.get_html = bill(self._get_html)
        self.post_json = bill(self._post_json)
        self.put_json = bill(self._put_json)
        self.delete_json = bill(self._delete_json)

    async def _wait(self, _action, reason: str = "") -> None:
        return None

    def _issue(self, verb: str, path: str) -> None:
        self.requests.append(f"{verb} {path}")
        refusal = self.refusals.get(verb)
        if refusal is not None:
            raise refusal

    async def _get_html(self, path: str, **_kwargs) -> str:
        self._issue("get_html", path)
        return EMPTY_MARKETPLACE

    async def _post_json(self, path: str, payload: dict, **_kwargs) -> dict:
        self._issue("post_json", path)
        return dict(EMPTY_READBACK) if "graphql" in path else {}

    async def _put_json(self, path: str, payload: dict, **_kwargs) -> dict:
        self._issue("put_json", path)
        return {}

    async def _delete_json(self, path: str, *, data: dict | None = None, **_kwargs) -> dict:
        self._issue("delete_json", path)
        return {}


def _service() -> tuple[TradeRouteService, _BillingTransport]:
    client = _BillingTransport()
    return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client


def _route() -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20003,
        dest_village_id=20044,
        dest_x=23,
        dest_y=88,
        dest_name="capital",
        cargo={Resource.CROP: 1000},
        cycle_hours=24,
        merchants=1,
        dispatch_minute=58,
    )


def _existing() -> list[ExistingRoute]:
    return [ExistingRoute(route_id=1, dest_village_id=20044, dest_x=23, dest_y=88)]


@pytest.mark.parametrize(
    "invoke",
    [
        lambda s: s.create_route(_route()),
        lambda s: s.disable_routes(20003, _existing()),
        lambda s: s.enable_routes(20003, _existing()),
        lambda s: s.update_cargo(20003, _existing(), {Resource.CROP: 1}, dest_x=23, dest_y=88),
        lambda s: s.delete_routes(20003, _existing()),
        lambda s: s.refresh_marketplace(20003),
    ],
    ids=["create", "disable", "enable", "cargo-update", "delete", "read-back"],
)
def test_a_one_request_call_is_billed_once(invoke):
    service, client = _service()
    asyncio.run(invoke(service))

    assert len(client.requests) == 1, "precondition: this call is one request"
    assert len(client.bills) == 1, f"{len(client.requests)} requests, {len(client.bills)} bills"


def test_opening_the_marketplace_bills_both_of_its_gets():
    # Two navigations, two throttler gaps, two bills. The service used to bill
    # the pair as one visit AND the transport billed each -- three for two.
    service, client = _service()
    asyncio.run(service.open_marketplace(20003))

    assert len(client.requests) == 2
    assert len(client.bills) == 2


def test_a_refused_write_is_billed_exactly_once():
    # The request went out and came back refused, at full price -- once.
    service, client = _service()
    client.refusals["post_json"] = NetworkError("HTTP 500: the game said no")

    result = asyncio.run(service.create_route(_route()))

    assert result.status == "failed"
    assert len(client.requests) == 1
    assert len(client.bills) == 1


def test_a_write_refused_before_it_is_issued_is_not_billed():
    client = _BillingTransport()
    service = TradeRouteService(client, live_enabled=False, reconciler_verified=True)

    with pytest.raises(TradeRoutePayloadUnverified):
        asyncio.run(service.create_route(_route()))

    assert client.requests == []
    assert client.bills == []
