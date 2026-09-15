"""Exactly how much traffic one live trade-route run costs.

Stealth here is a budget, not a vibe. The daily activity ceiling is spent by
every loop in the app, so "roughly a few requests" is not good enough: a change
that quietly doubles the cost of a route run steals budget from the farm loops
and pushes the account's daily total somewhere a human's would not be.

These pin the count, the METHOD and the ORDER for the real service against a
counting transport, so a regression shows up as a failing test rather than as an
account-level anomaly weeks later.

The read-back is a ``POST /api/v1/graphql``, not a page load, because that is
what the game's own client does: a create's success handler calls
``e.onSuccess()``, which on the list page refetches the route list over GraphQL.
The count did not change when it moved -- the shape did.
"""

import asyncio
import json
import re
from types import SimpleNamespace

import pytest

from travian_api.exceptions import NetworkError
from travian_api.services.distribution.allocation import Resource
from travian_api.services.trade_route_service import (
    MARKETPLACE_DESTINATIONS_QUERY,
    MARKETPLACE_READBACK_QUERY,
    PlannedRoute,
    TradeRouteService,
)

from .activity_billing import billing


def empty_marketplace(village: int) -> str:
    """A page carrying an empty but VALID trade-route model for *village*.

    The parser recognises it (no routes) rather than reporting the page as
    unreadable. It states `currentVillageId` and the server's clock offset, as
    the real Europe 2 page does:
    since the 2026-09-08 review the read refuses a model that describes a
    village other than the one asked for, and a fixture that omitted the field
    was quietly exercising a page the game never serves.
    """
    return (
        "<html><body>"
        # The server's clock, as every real page states it. The read refuses a
        # page without it rather than assuming UTC (#76), so a fixture that
        # omitted it was exercising a page the game never serves -- the same
        # reason `currentVillageId` is here.
        "<script>Travian.Game.timezoneOffsetToUTC = -3600;</script>"
        "<script>window.Travian.React.TradeRoutes.render("
        '{viewData: {"ownPlayer":{"currentVillageId":' + str(village) + ","
        '"village":{"marketplace":{"tradeRoutes":[]}}}}}'
        ");</script></body></html>"
    )


EMPTY_MARKETPLACE = empty_marketplace(20003)

# The slot this fixture's village keeps its marketplace on. Arbitrary, and
# deliberately not 17: the slot is a per-village fact with no relationship to
# the building's gid, and a fixture where the two coincide would let a bug that
# confuses them pass.
MARKETPLACE_SLOT = 30


def village_view(slot: int = MARKETPLACE_SLOT) -> str:
    """A dorf2 page in the markup ``parse_dorf2`` reads, with a marketplace.

    This used to be another copy of the marketplace page, which parsed to no
    buildings at all -- so every URL pinned below was the one we emit when the
    village view is UNREADABLE, not the one a live run emits. The file's whole
    claim is "this is exactly what goes on the wire", so the village view has to
    be a village view.
    """
    return (
        "<html><body><div id='village_map'>"
        f"<a href='/build.php?id={slot}' class='level colorLayer gid17' "
        "data-gid='17' title='Marketplace Level 5'></a>"
        "<a href='/build.php?id=19' class='emptyBuildingSlot'></a>"
        "</div></body></html>"
    )


VILLAGE_VIEW = village_view()

GRAPHQL = "/api/v1/graphql"
MARKETPLACE_URL = f"/build.php?id={MARKETPLACE_SLOT}&gid=17&t=3&newdid=20003"


def _readback(village: int, *routes: dict) -> dict:
    """A GraphQL answer shaped exactly like the marketplace query's.

    Same ``ownPlayer.village.marketplace.tradeRoutes`` path the page model uses,
    plus the ``currentVillageId`` the query asks for so the caller can tell which
    village it was actually answered about.
    """
    return {
        "data": {
            "ownPlayer": {
                "id": 1,
                "currentVillageId": village,
                "village": {"marketplace": {"tradeRoutes": list(routes)}},
            }
        }
    }


def _route_row(route_id: int, dest: int) -> dict:
    return {
        "to": {"id": dest, "name": "x", "mapId": 1},
        "routes": [
            {
                "id": route_id,
                "enabled": True,
                "sendOnce": False,
                "carriedResources": {"lumber": 0, "clay": 0, "iron": 0, "crop": 1},
                "departureAt": 1,
                "arrivalAt": 2,
                "repeat": 1,
                "merchants": 1,
                "ships": 1,
                "useTradeShips": False,
            }
        ],
    }


class _CountingClient:
    """Records every request in order. Serves whatever the test queues.

    Bills the activity ceiling once per request, on every exit path, exactly as
    ``HttpClient._billed`` does -- billing lives in the transport, so a fake
    that stands in for the transport has to charge for the requests it issues.
    A queued page or read-back may be an ``Exception``, which this raises after
    recording and billing the call: that is how a test makes one request in a
    sequence fail without replacing a method and losing its bill.
    """

    def __init__(self, pages, readbacks=()):
        self.calls: list[tuple[str, str]] = []
        self.referers: list[tuple[str, str | None]] = []
        self.bodies: list[tuple[str, dict]] = []
        self._pages = list(pages)
        self._readbacks = list(readbacks)
        self.waits: list[str] = []
        self.settings = SimpleNamespace(base_url="https://example.invalid")
        self.human_delay = SimpleNamespace(wait=self._wait)
        # Recorded, not discarded: every request this service makes is billed
        # to the daily activity ceiling, and "how much traffic did that cost"
        # is exactly what this file is about.
        self.logged_activity: list[float] = []
        self.activity_scheduler = SimpleNamespace(log_activity=self.logged_activity.append)
        bill = billing(self.logged_activity)
        self.get_html = bill(self._get_html)
        self.post_json = bill(self._post_json)
        self.put_json = bill(self._put_json)

    async def _wait(self, _action, reason=""):
        # Pacing is what keeps a burst from looking like a burst. Recorded so a
        # write that skipped its delay is visible here.
        self.waits.append(reason)

    @staticmethod
    def _served(queued):
        if isinstance(queued, Exception):
            raise queued
        return queued

    async def _get_html(self, path, **kw):
        self.calls.append(("GET", path))
        self.referers.append((path, kw.get("referer")))
        if self._pages:
            return self._served(self._pages.pop(0))
        # Answer for the village actually asked for. A canned page for one
        # village handed to a request for another is exactly what the read now
        # refuses, and the fake must not be the thing that fails that check.
        asked = re.search(r"newdid=(\d+)", path)
        return empty_marketplace(int(asked.group(1)) if asked else 20003)

    async def _post_json(self, path, payload, **kw):
        self.calls.append(("POST", path))
        self.referers.append((path, kw.get("referer")))
        self.bodies.append((path, payload))
        if path == GRAPHQL:
            assert self._readbacks, "queue a read-back payload for every confirm"
            # A confirm after a write makes TWO graphql calls with different
            # bodies -- the marketplace/destinations refresh and the route-list
            # read-back (HAR, 2026-09-15). Only the second one's answer is used,
            # so serving the last queued payload to both is faithful; popping for
            # the first would make every test queue a throwaway to say nothing.
            if len(self._readbacks) == 1:
                return self._served(self._readbacks[0])
            return self._served(self._readbacks.pop(0))
        return {}

    async def _put_json(self, path, payload, **kw):
        self.calls.append(("PUT", path))
        self.referers.append((path, kw.get("referer")))
        return {}


def _service(pages, readbacks=()):
    client = _CountingClient(pages, readbacks)
    return TradeRouteService(client, live_enabled=True, reconciler_verified=True), client


def _route(dest: int = 700) -> PlannedRoute:
    return PlannedRoute(
        origin_village_id=20003,
        dest_village_id=dest,
        dest_x=1,
        dest_y=2,
        dest_name="target",
        cargo={Resource.CROP: 1000},
        cycle_hours=24,
        merchants=1,
        dispatch_minute=58,
    )


class TestTheCanaryRunCostsFiveRequests:
    """Read the village, create one route, then settle the way the page does.

    Four of these were settled long ago. The fifth took two captures. A
    session-wide recording showed a marketplace write followed by two GraphQL
    calls and (it was thought) a resource refresh, but recorded none of their
    bodies -- so the count was known and the content was not, and the branch
    deliberately sent ONE call rather than guess at three. An invented request is
    a positive anomaly and the cheapest possible thing to alert on; a missing one
    is an absence consistent with a dozen innocent causes.

    A full HAR of a live create (2026-09-15) has the bodies, and the guess that
    was avoided would have been wrong twice over::

        POST /api/v1/trade-routes                    07:25:50.679  (201)
          POST /api/v1/graphql  destinations 381 B   07:25:50.854
          POST /api/v1/graphql  route list   452 B   07:25:50.855

    The second call is not the read-back repeated -- it is the marketplace's own
    model, the one the create dialog loads when it opens. And there is no
    ``village/resources`` in the burst at all; a request from that same callback
    would share the millisecond, and nothing does.

    So five, and every one of them quotable.
    """

    def test_a_read_is_two_gets_in_the_human_order(self):
        service, client = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))

        assert client.calls == [
            ("GET", "/dorf2.php?newdid=20003"),
            ("GET", MARKETPLACE_URL),
        ], "the village view must come first, or the marketplace Referer is a lie"

    def test_a_create_is_one_post_and_it_waits_first(self):
        service, client = _service([])
        asyncio.run(service.create_route(_route()))

        assert client.calls == [("POST", "/api/v1/trade-routes")]
        assert client.waits, "a write with no pacing delay is a burst of one"

    def test_a_confirmation_is_two_graphql_calls_and_no_page_load(self):
        """Refetching the model the open page runs on is not a navigation, so it
        must not walk to the page or reload it.

        Two calls, because the page's success handler makes two -- and they are
        DIFFERENT queries, which is the whole reason this stayed at one for so
        long. Sending the route-list query twice would have put two identical
        bodies on the wire a millisecond apart, which no client does.
        """
        service, client = _service([], [_readback(20003, _route_row(1, 700))])
        asyncio.run(service.confirm_routes(20003, after_write=True))

        assert client.calls == [("POST", GRAPHQL), ("POST", GRAPHQL)]
        assert not any(m == "GET" for m, _ in client.calls), "no navigation"

    def test_the_two_calls_after_a_write_say_different_things(self):
        """The defect that kept this a no-op, pinned so it cannot come back."""
        service, client = _service([], [_readback(20003, _route_row(1, 700))])
        asyncio.run(service.confirm_routes(20003, after_write=True))

        sent = [body["query"] for path, body in client.bodies if path == GRAPHQL]
        assert len(sent) == 2
        assert sent[0] != sent[1], "two identical GraphQL bodies 1ms apart is a tell"
        # And in the capture's order: destinations first, route list second.
        assert sent[0].startswith("{ownPlayer{village{marketplace{merchantsInfo")
        assert sent[1].startswith("{ownPlayer{id currentVillageId")

    def test_a_confirmation_with_no_write_behind_it_is_one_request(self):
        """The stability re-reads are not read-backs.

        `settle_after_write` sends the query the page fires WHEN A WRITE
        SUCCEEDS -- the same one its create dialog loads on open. Firing that off
        a standalone re-read puts a create-dialog request on the wire with no
        create behind it, which is a request in a context the client never
        produces it in: the defect the method exists to fix, inverted.
        """
        service, client = _service([], [_readback(20003, _route_row(1, 700))])
        asyncio.run(service.confirm_routes(20003))

        assert client.calls == [("POST", GRAPHQL)]
        sent = [body["query"] for path, body in client.bodies if path == GRAPHQL]
        assert sent == [MARKETPLACE_READBACK_QUERY], "the read-back, not the dialog's query"

    def test_the_verification_survives_the_refresh_failing(self):
        """The refresh's answer is unused, so its failure must not be reported as
        "what was created is unknown" -- those are different answers."""
        service, client = _service(
            [],
            [
                NetworkError("the marketplace refresh fell over"),
                _readback(20003, _route_row(1, 700)),
            ],
        )

        confirmed = asyncio.run(service.confirm_routes(20003, after_write=True))

        assert [r.route_id for r in confirmed] == [1]
        assert client.calls == [("POST", GRAPHQL), ("POST", GRAPHQL)]

    def test_the_whole_canary_is_five_requests_in_this_exact_order(self):
        service, client = _service(
            [VILLAGE_VIEW, EMPTY_MARKETPLACE],
            [_readback(20003, _route_row(1, 700))],
        )
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))
        confirmed = asyncio.run(service.confirm_routes(20003, after_write=True))

        assert client.calls == [
            ("GET", "/dorf2.php?newdid=20003"),
            ("GET", MARKETPLACE_URL),
            ("POST", "/api/v1/trade-routes"),
            ("POST", GRAPHQL),
            ("POST", GRAPHQL),
        ]
        assert len(client.calls) == 5
        assert [r.route_id for r in confirmed] == [1]

    def test_verifying_costs_exactly_one_request_more_than_not_verifying(self):
        # The price of not guessing. TWO requests follow the write, but only one
        # of them is the verification -- the other is what the page does after a
        # create whether anyone is checking or not, so it is not charged to the
        # decision to check.
        service, client = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE], [_readback(20003)])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))
        before = len(client.calls)

        asyncio.run(service.confirm_routes(20003, after_write=True))
        after_write = len(client.calls) - before

        asyncio.run(service.confirm_routes(20003))
        settling_only = after_write - (len(client.calls) - before - after_write)

        assert after_write == 2
        assert settling_only == 1, "the verification itself is one request"


class TestTheReadBackIsTheQueryTheGameFires:
    """Byte-for-byte, because a query no client sends is worse than a page load.

    That was the standing objection to moving this read to GraphQL: a
    hand-written query differing by one field or one space is a request the game
    cannot produce, which is more distinctive than a page refresh, not less. It
    is answered by taking the query from the bundle rather than reconstructing
    it -- so this pins the body.
    """

    def _confirm(self):
        service, client = _service([], [_readback(20003)])
        asyncio.run(service.confirm_routes(20003))
        return next(body for path, body in client.bodies if path == GRAPHQL)

    def test_the_query_is_the_bundles_query(self):
        assert self._confirm()["query"] == (
            "{ownPlayer{id currentVillageId village{marketplace"
            "{tradeRoutes{...TradeRouteFields}}}}}"
            "fragment TradeRouteFields on TradeRoutesSet{objectId expanded"
            " from{id name tribeId}to{id mapId name travelTime player{id}}"
            "routes{...RouteFields}"
            "nextDelivery{departureAt merchants ships useTradeShips}}"
            "fragment RouteFields on TradeRoute{id enabled sendOnce"
            " carriedResources{lumber clay iron crop}departureAt arrivalAt repeat"
            " merchants ships useTradeShips}"
        )

    def test_the_derived_query_is_what_the_browser_actually_sent(self):
        """Both constants were DERIVED -- read out of the bundle and reassembled
        by reasoning about graphql-js `print` and `stripIgnoredCharacters`. The
        2026-09-15 HAR carries the real bodies, with their own content-lengths,
        and the derivation was exact.

        Serialised compactly, because that is what `JSON.stringify` emits and
        what the capture's `content-length` counts.
        """
        assert len(json.dumps({"query": MARKETPLACE_READBACK_QUERY}, separators=(",", ":"))) == 452
        assert (
            len(json.dumps({"query": MARKETPLACE_DESTINATIONS_QUERY}, separators=(",", ":"))) == 381
        )

    def test_the_post_write_refresh_is_the_marketplaces_own_model(self):
        """Byte for byte off the wire, 07:25:50.854.

        The page loads this when the create dialog OPENS and again when the
        create succeeds -- merchants, ship capacity and the destination list,
        which is exactly the state a completed send changes.
        """
        assert MARKETPLACE_DESTINATIONS_QUERY == (
            "{ownPlayer{village{marketplace{merchantsInfo{...MerchantsInfoFields}"
            "tradeShipCapacity tradeShipsInfo{...MerchantsInfoFields}"
            "destinations{...DestinationFields}}...HarbourFields}}}"
            "fragment MerchantsInfoFields on MerchantsInfo{total capacity}"
            "fragment DestinationFields on Destination{id name x y cropOnly player{id}}"
            "fragment HarbourFields on OwnVillage{isShore hasHarbour}"
        )

    def test_the_operation_carries_no_query_keyword(self):
        # graphql-js prints an anonymous operation with no variables and no
        # directives in short form, and the client prints before it sends. A
        # leading "query" would be a byte the browser never emits.
        assert self._confirm()["query"].startswith("{ownPlayer{")

    def test_it_asks_which_village_it_is_answering_about(self):
        # The query takes no village argument, so this field is the only thing
        # standing between us and attributing one village's routes to another.
        assert "currentVillageId" in self._confirm()["query"]

    def test_the_body_has_exactly_one_key(self):
        # The client passes no variables and JSON.stringify drops the undefined
        # value, so the real body is {"query": ...}. An extra key is a
        # fingerprint like any other.
        assert set(self._confirm()) == {"query"}

    def test_the_constant_is_what_is_sent(self):
        assert self._confirm()["query"] == MARKETPLACE_READBACK_QUERY


class TestEveryMarketplaceRequestStatesItsOwnReferer:
    """Not one of these may fall back to the account-wide "last page".

    That field is a single slot shared by every concurrent operation, and each of
    these requests waits out a pacing gap before its headers are built -- so a
    farm loop or queue poll landing in the window takes the Referer with it.
    """

    MARKETPLACE = f"https://example.invalid{MARKETPLACE_URL}"

    def test_the_marketplace_get_is_referred_from_the_village_view(self):
        # The navigation stays a page load precisely so this Referer is truthful:
        # dorf2 is the page a human clicks the marketplace from. Pinning it is
        # what makes the claim survive a concurrent GET.
        service, client = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))

        assert client.referers == [
            ("/dorf2.php?newdid=20003", None),
            (
                MARKETPLACE_URL,
                "https://example.invalid/dorf2.php?newdid=20003",
            ),
        ]

    def test_a_create_is_referred_from_the_page_that_has_the_form(self):
        # A POST to the trade-route endpoint referred from anywhere else is a
        # desync no browser produces: the form only exists on that tab.
        service, client = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.create_route(_route()))

        assert dict(client.referers)["/api/v1/trade-routes"] == self.MARKETPLACE

    def test_the_read_back_is_referred_from_the_marketplace_too(self):
        # An API request never advances page context, so the GraphQL read-back
        # cannot inherit a truthful Referer -- it has to be given one, and the
        # only page whose script fires this query is the trade-route tab.
        service, client = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE], [_readback(20003)])
        asyncio.run(service.list_existing_routes(20003))
        asyncio.run(service.confirm_routes(20003))

        assert dict(client.referers)[GRAPHQL] == self.MARKETPLACE

    def test_the_navigation_still_establishes_the_pin_for_later_writes(self):
        service, _ = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE])
        asyncio.run(service.list_existing_routes(20003))

        assert service._marketplace_referer[20003] == self.MARKETPLACE


class TestAnUnreadableAnswerIsNeverSilentlyEmpty:
    def test_a_confirmation_that_cannot_be_read_raises(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        # A soft block, a login redirect or an error payload all arrive as a body
        # with no marketplace in it. That is "unknown", never "no routes".
        service, _ = _service([], [{"errors": [{"message": "nope"}]}])
        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.confirm_routes(20003))

    def test_a_read_back_about_another_village_raises(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        # The query names no village, so a concurrent ?newdid= can move the
        # session between the write and the read. The answer says which village
        # it described; believing the wrong one would report routes that were
        # never created here.
        service, _ = _service([], [_readback(20009, _route_row(1, 700))])
        with pytest.raises(MarketplaceUnreadable, match="answered for village 20009"):
            asyncio.run(service.confirm_routes(20003))

    def test_a_read_that_cannot_be_parsed_raises_too(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        service, _ = _service(["<html/>", "<html><body>login please</body></html>"])
        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.list_existing_routes(20003))


class TestAFailedReadIsBilledToo:
    """A read that raised spent its request exactly as one that answered.

    This is the module's own argument for billing reads at all, pointed at the
    failures: they consumed a real throttler gap, and the daily ceiling is
    SHARED with the farm-list and oasis loops, so under-counting here silently
    licenses THOSE to overspend. A NetworkError refunds nothing.

    Exactly once is the property, per REQUEST: billing lives in the transport
    (``HttpClient._billed``), so a read that failed half-way through its two
    GETs is billed for the two that went out, and one that answered is not
    billed all over again by the service on top.
    """

    def test_a_village_view_that_fails_is_still_billed(self):
        service, client = _service([NetworkError("HTTP 500: the game said no")])

        with pytest.raises(NetworkError):
            asyncio.run(service.open_marketplace(20003))

        assert client.calls == [("GET", "/dorf2.php?newdid=20003")]
        assert len(client.logged_activity) == 1, "a failed read is not a free read"
        assert client.logged_activity[0] >= 0.0

    def test_a_marketplace_get_that_fails_after_the_village_view_bills_both(self):
        # Two requests went out, so two bills -- the one that answered and the
        # one that did not. Both spent a throttler gap.
        service, client = _service([VILLAGE_VIEW, NetworkError("HTTP 500: the game said no")])

        with pytest.raises(NetworkError):
            asyncio.run(service.open_marketplace(20003))

        assert len(client.calls) == 2
        assert len(client.logged_activity) == 2

    def test_a_read_back_that_fails_is_still_billed(self):
        service, client = _service([], [NetworkError("HTTP 500: the game said no")])

        with pytest.raises(NetworkError):
            asyncio.run(service.refresh_marketplace(20003))

        assert client.calls == [("POST", GRAPHQL)]
        assert len(client.logged_activity) == 1

    def test_an_unreadable_answer_is_billed_exactly_once(self):
        from travian_api.services.trade_route_service import MarketplaceUnreadable

        # The request succeeded and the BODY was the problem. Already billed
        # before this raise, so the guard is against billing it twice.
        service, client = _service([], [{"errors": [{"message": "nope"}]}])

        with pytest.raises(MarketplaceUnreadable):
            asyncio.run(service.refresh_marketplace(20003))

        assert len(client.logged_activity) == 1

    def test_a_read_that_answers_is_billed_once_per_request(self):
        # The regression anchor: open_marketplace is two GETs and two billings,
        # refresh_marketplace is one POST and one more.
        service, client = _service([VILLAGE_VIEW, EMPTY_MARKETPLACE], [_readback(20003)])
        asyncio.run(service.list_existing_routes(20003))
        assert len(client.logged_activity) == 2

        asyncio.run(service.refresh_marketplace(20003))
        assert len(client.logged_activity) == 3
