"""Trade-route execution against Travian's marketplace (Gold Club).

Turns a distribution plan's rows into recurring in-game trade routes. Mirrors
the FarmListService mutation pattern: xhr-shaped POSTs through the stealth
HttpClient, ``safe_to_retry=False`` on every write, a per-origin ``KeyedLock``,
and Gold Club errors mapped rather than raised.

The wire format is VERIFIED against a real client request captured from
Europe 2 on 2026-08-20 (gpack 597.6): ``POST /api/v1/trade-routes`` to create
(201, empty body) and ``PUT /api/v1/trade-routes`` to enable/disable in bulk
(200). ``tests/test_trade_route_payload.py`` pins both shapes to that capture.

The read-back after a write is likewise the game's own: ``POST /api/v1/graphql``
carrying :data:`MARKETPLACE_READBACK_QUERY`, which is read verbatim out of the
client bundle rather than reconstructed. See that constant, and
``docs/23-stealth-decisions.md`` for why this used to be a page load.

Live writes are gated on ``TradeRouteService.live_enabled``, because the payload
being correct is necessary but not sufficient: creating routes mutates a real
account. This class's OWN default is off -- every direct construction and every
test starts preview-only -- but the server does not use it: ``sessions.py``
passes ``settings.trade_route_live``, which has defaulted **ON** since
2026-08-27 at the operator's instruction, the opt-in having reverted to
preview-only on every restart. So a run through the app writes unless
``TRAVIAN_TRADE_ROUTE_LIVE=false`` says otherwise. The dry-run path never
touches the game and does not depend on any of this.

Two things the capture settled that the planner had been guessing at:

* **Dispatch phase is settable.** The body carries an explicit ``hour`` and
  ``minute``, so a route's send time is chosen at creation rather than being
  fixed to the moment of the click. Review R6 in
  ``docs/25-resource-distribution-planner.md`` assumed the opposite was
  possible; it is not the case, and the beat is therefore realisable as planned.
* **Merchant count is not sent.** The game derives it from the cargo, so the
  planner's merchant figures are for budgeting and warnings only, never wire
  data.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any

from ..clients.http_client import HttpClient
from ..concurrency import KeyedLock
from ..exceptions import NetworkError, TravianError
from ..parsers.html_parser import DEFAULT_MAP_SPAN
from ..services.distribution.allocation import Resource
from ..stealth.human_delay import ActionType
from .distribution.execution_trace import ExecutionTrace

logger = logging.getLogger(__name__)

# Marketplace building id (gid=17); its page lists a village's trade routes.
MARKETPLACE_GID = 17

# ── The read-back query, taken from the game's own bundle ──────────────────
#
# Europe 2, gpack 22.03, main.js. The list page builds it from three sources:
#
#   Zb = j("fragment RouteFields on TradeRoute{...}")
#   qb = j("fragment TradeRouteFields on TradeRoutesSet{...}", [Zb])
#   Kb = j("query{ownPlayer{id currentVillageId village{marketplace{tradeRoutes{...TradeRouteFields}}}}}", [qb])
#
# and a create's success handler calls `e.onSuccess()`, which on this page is
# `T = () => { N(Kb).then(...) }`. `N` is
#   (e, t) => Travian.Promises.graphQL({query: stripIgnoredCharacters(print(e)), variables: t})
# over `fetch("/api/v1/" + "graphql", {method: "POST", body: JSON.stringify(...)})`.
#
# So the string below is what leaves the browser: graphql-js `print` composes the
# operation followed by its fragments in the order the tag collected them
# (TradeRouteFields, then RouteFields), drops the `query` keyword because an
# anonymous operation with no variables or directives prints in short form, and
# `stripIgnoredCharacters` removes every separator that is not needed to keep two
# names apart. `variables` is undefined here, and JSON.stringify omits it -- so
# the body carries `query` and nothing else.
_ROUTE_FIELDS_FRAGMENT = (
    "fragment RouteFields on TradeRoute{id enabled sendOnce"
    " carriedResources{lumber clay iron crop}departureAt arrivalAt repeat"
    " merchants ships useTradeShips}"
)
_TRADE_ROUTE_FIELDS_FRAGMENT = (
    "fragment TradeRouteFields on TradeRoutesSet{objectId expanded"
    " from{id name tribeId}to{id mapId name travelTime player{id}}"
    "routes{...RouteFields}nextDelivery{departureAt merchants ships useTradeShips}}"
)
_MARKETPLACE_OPERATION = (
    "{ownPlayer{id currentVillageId village{marketplace{tradeRoutes{...TradeRouteFields}}}}}"
)
MARKETPLACE_READBACK_QUERY = (
    _MARKETPLACE_OPERATION + _TRADE_ROUTE_FIELDS_FRAGMENT + _ROUTE_FIELDS_FRAGMENT
)

# Gold Club rejection marker, matched case-insensitively (as FarmListService does).
_GOLDCLUB_MARKERS = ("goldclub", "gold club", "plus.error")


class TradeRoutePayloadUnverified(TravianError):
    """Raised when live creation is attempted without the live opt-in.

    The wire payload IS verified against a real capture; this is the operator's
    explicit switch for letting the app write to the game, not a
    "we do not know the format yet" guard.
    """


class TradeRouteReconcilerUnverified(TravianError):
    """Raised when a route would be CREATED without being able to read existing ones."""


class ToggleResponseUnreadable(TravianError):
    """A bulk enable/disable answered with a body we could not read.

    Raised instead of returning "nothing was rejected", for the reason
    :class:`MarketplaceUnreadable` exists on the read side: "I could not check"
    and "the game refused nothing" are different answers and must not collapse
    into one. The bulk toggle DOES answer with a body and the game's own client
    reads it, so an unrecognised body means something changed underneath us --
    a soft-block page, a gpack revision, an HTML error -- not that every route
    went through.

    The direction of the damage is what makes this worth an exception. On a
    DISABLE, which is the revert path, collapsing the two reported
    ``disabled: 24 route(s)`` while twenty-four rows kept shipping: a revert
    complete on paper with the account still draining a sender. A revert that
    plainly names the rows a human must remove is far better than one that
    claims to have done it.
    """


class MarketplaceUnreadable(TravianError):
    """A marketplace page carried no trade-route model we could read.

    Raised instead of returning an empty list, because the two are not the same
    answer: a village with no routes means "nothing to disable, create the
    plan", while a page we could not read means "we have no idea what is there".
    Treating the second as the first re-creates the whole plan every run and
    accumulates duplicates in-game. The executor already has the right
    behaviour for a failed read -- defer this origin and everything after it --
    so this rides that path rather than inventing a second one.
    """


# Whether the existing-route read has been confirmed against a real
# /build.php?gid=17&t=3 page. It has: tests/fixtures/marketplace_trade_routes.html
# is a real Europe 2 page (gpack 597.6), and the parser reads the JSON model the
# page hands React rather than scraping row markup.
#
# Kept as an explicit gate because it guards CREATION specifically, and the
# asymmetry is the point. A page we cannot read yields no routes, which is the
# safe answer for disabling -- nothing gets disabled -- and the DANGEROUS answer
# for creating, because "no routes" reads as "this village is empty" and the
# whole plan gets created again. Every run. Duplicates accumulate in-game and
# the repeated identical creates are exactly the daily rebuild-the-same-routes
# pattern the rest of this code goes to some trouble to avoid.
#
# read_trade_routes now distinguishes the two cases outright (None vs []), and
# list_existing_routes raises MarketplaceUnreadable rather than reporting an
# empty village. This flag stays as the switch a caller can flip if a future
# gpack moves the model and the distinction stops holding.
ROUTE_LIST_MARKUP_VERIFIED = True


@dataclass(frozen=True)
class PlannedRoute:
    """One route to create: origin village → destination coordinates."""

    origin_village_id: int
    # Reconciliation key -- see ExistingRoute.dest_village_id.
    dest_village_id: int
    dest_x: int
    dest_y: int
    dest_name: str
    # Always all four resources in game order; zeros allowed.
    cargo: dict[Resource, int]
    cycle_hours: int
    merchants: int
    # Scheduled send time, minutes past midnight (the planner's beat phase). A
    # Gold Club route has an explicit "Send at" time; dropping it would create
    # every route around the click instant, collapsing the beat that spaces
    # arrivals and orders relay hubs after their inbound crop.
    dispatch_minute: int = 0
    # The profile hours this route belongs to, when the run will prune its
    # fan-out -- (start_minute, end_minute), wrapping past midnight. None means
    # round the clock. Carried on the route rather than on the request because
    # a whole-day run executes SEVERAL profiles at once and each route is
    # trimmed, budgeted and reconciled against its own hours. The service layer
    # ignores it; the create payload has no field it could map to.
    window: tuple[int, int] | None = None
    # The profile's name, for the operator: "Day"/"Night" in previews and
    # traces. Never used for matching -- the window is the semantics, this is
    # the label.
    segment: str = ""


@dataclass
class RouteActionResult:
    """Outcome of one create/disable action (or its dry-run preview)."""

    origin_village_id: int
    dest_x: int
    dest_y: int
    # Every status this class actually produces. Kept in step with the code
    # because a stale list here is worse than none: it invites a caller to
    # branch on something that never arrives.
    #
    # `unverified` is not a weaker `failed`, it is a different answer: the
    # request returned SUCCESS and its body could not be read, so the write
    # probably landed and nothing here proves it. Only the marketplace does,
    # and the caller is expected to look -- exactly what the create path
    # already calls `created_unverified`.
    status: str  # created | disabled | enabled | updated | deleted
    #             | unverified | failed | skipped | stopped
    detail: str = ""


@dataclass
class ExistingRoute:
    """A trade route already present on a village's marketplace page."""

    route_id: int
    # The destination the page itself states. Reconciliation keys on THIS, not on
    # coordinates: the page carries no coordinates, so those are back-derived from
    # the map id through the world's span, and a wrong span (or a map id the span
    # cannot place) silently mismatches every route -- which reads as "the plan
    # wants none of these", disables them all, and creates the plan again on top.
    dest_village_id: int
    # Derived from the map id for display and reporting only. None when the map id
    # was missing or impossible for this span.
    dest_x: int | None = None
    dest_y: int | None = None
    visible: bool = True  # UI-visible; hidden entries are treated as honeypots
    # Whether the route is currently ENABLED. Travian keeps disabled routes in
    # the list (they can be re-enabled), so a visible row at a desired coordinate
    # is NOT necessarily satisfying the plan — only an enabled one is. Defaults
    # True so a parser that cannot read the flag errs toward "leave it alone".
    active: bool = True
    # What this row actually ships, as the page reports it. Needed to tell a
    # route that still matches the plan from one whose cargo has drifted: a
    # route is created once and the plan moves every time production does, so
    # without this the live routes and the sheet slowly describe different
    # accounts with nothing detecting it. None when the page did not say.
    cargo: dict[Resource, int] | None = None
    # When this ROW departs, as a unix timestamp. A "repeat every N hours" route
    # is 24/N separate rows sharing a destination and a cargo; their departures
    # are the only thing that tells them apart, and therefore the only way to say
    # which of them fall inside a profile's hours. None when the page did not say.
    departure_at: int | None = None


# How far a live route's cargo may drift from the plan before it is rewritten.
# Both bounds matter: the relative one stops a large route being rewritten for a
# rounding difference, and the absolute floor stops a small route thrashing on
# every run because 5% of 200 is 10. An update is a real request against a real
# account, so the cost of churn is paid in activity budget and in looking busy.
CARGO_DRIFT_RELATIVE = 0.05
CARGO_DRIFT_ABSOLUTE = 100


def _cargo_of(row: dict) -> dict[Resource, int] | None:
    """The parsed cargo as a Resource-keyed dict, or None if absent."""
    raw = row.get("cargo")
    if not isinstance(raw, dict):
        return None
    return {r: int(raw.get(r.value, 0) or 0) for r in Resource}


def cargo_has_drifted(live: dict[Resource, int] | None, planned: dict[Resource, int]) -> bool:
    """Is the live cargo far enough from the plan to be worth a request?

    Unknown live cargo returns False: "the page did not tell us" is not evidence
    of a difference, and rewriting on no evidence would rewrite everything.
    """
    if live is None:
        return False
    for resource in Resource:
        want = int(planned.get(resource, 0))
        have = int(live.get(resource, 0))
        tolerance = max(CARGO_DRIFT_ABSOLUTE, abs(want) * CARGO_DRIFT_RELATIVE)
        if abs(have - want) > tolerance:
            return True
    return False


class TradeRouteService:
    """Create/disable Travian trade routes from a distribution plan."""

    def __init__(
        self,
        http_client: HttpClient,
        *,
        live_enabled: bool = False,
        reconciler_verified: bool = ROUTE_LIST_MARKUP_VERIFIED,
        trace: ExecutionTrace | None = None,
    ) -> None:
        self.http_client = http_client
        # Records every write with its payload and the game's latency. Optional
        # so nothing here depends on being traced, but a live run should always
        # pass one: without it a route that appears in-game with the wrong cargo
        # cannot be traced back to either the planner or the serialiser.
        self.trace = trace
        # Defaults off HERE, which is this class's own safe default and what
        # every direct construction and every test relies on. It is NOT what a
        # server run gets: `sessions.py` passes `settings.trade_route_live`,
        # which has defaulted ON since 2026-08-27. The wire format is verified,
        # so this is not a "do we know the shape" guard -- it is the line
        # between previewing a plan and writing to a real account.
        self.live_enabled = live_enabled
        # An instance attribute rather than a module lookup so the gate can be
        # exercised in tests. The production default is
        # ROUTE_LIST_MARKUP_VERIFIED, which is True: the page's own React model
        # is read, and a fixture pins it.
        self.reconciler_verified = reconciler_verified
        # The marketplace URL last opened for each village, used to pin the
        # Referer on that village's writes. BrowserHeaders tracks only ONE "last
        # page visited" for the whole account, so a farm loop or queue poll
        # doing a single GET during a write's 3-20s pacing delay would otherwise
        # leave the write referred from a page that has no trade-route form.
        self._marketplace_referer: dict[int, str] = {}
        self._origin_lock = KeyedLock()
        # Serializes whole execute runs for this account so a double-click or a
        # second tab can't fire two concurrent reconciliations (which would
        # bypass the per-run request caps and burst writes).
        self.execute_lock = asyncio.Lock()

    def origin_lock(self, village_id: int) -> AbstractAsyncContextManager[None]:
        """Serialize the disable+create sequence for one origin village, so a
        concurrent execute (or other op) can't interleave route writes on it."""
        return self._origin_lock(village_id)

    # ── Read ──────────────────────────────────────────────────────────

    async def open_marketplace(self, village_id: int) -> str:
        """Open the marketplace (gid=17) for a village and return its HTML.

        Two GETs, matching how a human reaches the marketplace: the village
        view (dorf2) first, then the marketplace — so the marketplace request
        carries a truthful Referer (the village page), not whatever page the
        loop last touched (a bare gid=17 with a stale Referer is a tell). That
        Referer is PINNED rather than left to the stealth layer's account-wide
        "last page visited", which any concurrent request can move -- see the
        second GET below. The marketplace GET doubles as the read of existing
        routes, so "disable old routes if needed" needs no further request.

        ``newdid`` rides on BOTH GETs, matching the codebase convention
        (building_service, oasis_raider): keeping it on the data request pins the
        village context, so a concurrent same-session request switching the
        active village between the two GETs cannot make us read — and disable —
        the wrong village's routes.
        """
        newdid_q = f"?newdid={village_id}" if village_id else ""
        newdid_amp = f"&newdid={village_id}" if village_id else ""
        base = self.http_client.settings.base_url.rstrip("/")
        village_view = f"/dorf2.php{newdid_q}"
        await self.http_client.get_html(village_view)
        # `t=3` is the trade-route tab. Without it we never load the tab the
        # routes live on -- so the reconciler read a page that cannot contain
        # them, and a server-side "did this session render the trade-route tab
        # before POSTing to it?" check would fail outright.
        path = f"/build.php?gid={MARKETPLACE_GID}&t=3{newdid_amp}"
        # Pin the second GET to the village view we just loaded, rather than
        # letting BrowserHeaders supply it. The two are the same value only in a
        # quiet session: this GET waits out a throttler gap BEFORE its headers
        # are built, and the account-wide "last page" is one field shared with
        # every concurrent operation -- so a farm loop or queue poll landing in
        # that window sends this navigation out referred from /dorf1.php. The
        # same hazard `_marketplace_referer` closes on the writes, closed on the
        # read that establishes them.
        html = await self.http_client.get_html(path, referer=f"{base}{village_view}")
        self._marketplace_referer[village_id] = f"{base}{path}"
        return html

    async def refresh_marketplace(self, village_id: int) -> dict[str, Any]:
        """Re-read the route list the way the game's own client does. ONE request.

        Deliberately a separate method rather than a flag on open_marketplace:
        this is a different act, and a different REQUEST. open_marketplace
        *navigates* to the page from the village view; this refetches the model
        the open page runs on, and the game does that with GraphQL, not a page
        load: the create's success handler calls ``e.onSuccess()``, which on the
        list page is ``T = () => { N(Kb).then(...) }`` -- a
        ``POST /api/v1/graphql`` carrying :data:`MARKETPLACE_READBACK_QUERY`.
        There is no navigation here at all, so the dorf2 hop would be wrong as
        well as wasteful.

        The query takes no village argument -- it reads whichever village the
        session is on -- so it asks for ``currentVillageId`` and we check it. A
        pinned village would be better; a read that can say which village it
        actually described is the next best thing, and it is what stops a
        concurrent ``?newdid=`` from making us attribute one village's routes to
        another.

        Returns the GraphQL ``data`` payload, whose
        ``ownPlayer.village.marketplace.tradeRoutes`` path is identical to the
        page model's.
        """
        response = await self.http_client.post_json(
            "/api/v1/graphql",
            # `variables` is absent on purpose: the client's call passes none, and
            # JSON.stringify drops an undefined value, so the real body has this
            # one key. An extra key is a fingerprint like any other.
            {"query": MARKETPLACE_READBACK_QUERY},
            # An API request never advances page context, so this one must state
            # where it is issued from: the marketplace tab, which is the only
            # page whose script fires this query. Falling back to the
            # account-wide last page would send it referred from whatever a
            # concurrent loop touched during the write's 3-20s pacing delay.
            referer=self._marketplace_referer.get(village_id),
        )
        view = response.get("data") if isinstance(response, dict) else None
        if not isinstance(view, dict):
            raise MarketplaceUnreadable(
                f"village {village_id}: the marketplace query returned no data, so "
                f"what is on the marketplace is unknown"
            )
        own_player = view.get("ownPlayer")
        current = own_player.get("currentVillageId") if isinstance(own_player, dict) else None
        if village_id and current != village_id:
            # The session moved between the write and the read-back. The routes
            # in this payload are some OTHER village's; treating them as this
            # one's would report routes that were never created here and hide
            # ones that were.
            raise MarketplaceUnreadable(
                f"village {village_id}: the marketplace query answered for village "
                f"{current!r}, so this village's routes are unknown"
            )
        return view

    async def confirm_routes(
        self, village_id: int, *, map_span: int = DEFAULT_MAP_SPAN
    ) -> list[ExistingRoute]:
        """What is REALLY on the marketplace now, read back after writing to it.

        ``POST /api/v1/trade-routes`` answers with an empty body: no route id, no
        confirmation, nothing that distinguishes "created" from "accepted and
        silently did nothing". Treating a 200 as proof of creation is an
        assumption presented as a result, and the failure it hides is the
        expensive kind -- a run reporting routes it never made, whose next run
        reports them again, forever.

        So the only honest answer comes from looking. One request, and it is the
        same request the game's own UI makes after a create -- byte for byte the
        query its create handler fires (see :func:`refresh_marketplace`).

        Raises :class:`MarketplaceUnreadable` if the answer cannot be read,
        because "I could not check" and "nothing was created" are different
        answers and must not collapse into one.
        """
        view = await self.refresh_marketplace(village_id)
        from ..parsers.html_parser import read_trade_routes_from_view

        parsed = read_trade_routes_from_view(view, map_span)
        if parsed is None:
            raise MarketplaceUnreadable(
                f"village {village_id}: could not re-read the marketplace after "
                f"writing to it, so what was actually created is unknown"
            )
        return [
            ExistingRoute(
                route_id=r["route_id"],
                dest_village_id=r["dest_village_id"],
                dest_x=r["dest_x"],
                dest_y=r["dest_y"],
                visible=r["visible"],
                active=r.get("active", True),
                cargo=_cargo_of(r),
                departure_at=r.get("departure_at"),
            )
            for r in parsed
        ]

    async def list_existing_routes(
        self, village_id: int, *, map_span: int = DEFAULT_MAP_SPAN
    ) -> list[ExistingRoute]:
        """Existing trade routes on a village's marketplace, visibility preserved.

        Every entry is tagged with ``visible`` so a caller can ignore honeypots
        a human could not see. Always True in practice: the page's React model
        lists only real rows and has no hidden-entry concept, so the tag is
        vestigial rather than a live signal.

        The page's own React model is the source, so a route always carries the
        destination *village id*; ``dest_x``/``dest_y`` are derived from the map
        id through *map_span* for reporting only and may be None.

        Raises :class:`MarketplaceUnreadable` if the page carried no model at all,
        rather than reporting an empty village -- the difference decides whether
        the reconciler creates the whole plan again.
        """
        html = await self.open_marketplace(village_id)
        from ..parsers.html_parser import read_trade_routes

        parsed = read_trade_routes(html, map_span)
        if parsed is None:
            # A soft block page, a login redirect or a gpack that moved the
            # model all land here. Any of them would otherwise read as "this
            # village has no routes".
            raise MarketplaceUnreadable(
                f"village {village_id}: the marketplace page carried no trade-route "
                f"model, so what is already there is unknown; refusing to treat it "
                f"as an empty village"
            )

        return [
            ExistingRoute(
                route_id=r["route_id"],
                dest_village_id=r["dest_village_id"],
                dest_x=r["dest_x"],
                dest_y=r["dest_y"],
                visible=r["visible"],
                active=r.get("active", True),
                cargo=_cargo_of(r),
                departure_at=r.get("departure_at"),
            )
            for r in parsed
        ]

    # ── Write (gated: live opt-in; payload verified) ────────────────────────────

    def _build_create_payload(self, route: PlannedRoute) -> dict:
        """``POST /api/v1/trade-routes`` body. VERIFIED against a real request.

        Field-for-field from the capture, and deliberately nothing more: an
        extra field the client never sends is a fingerprint.

        ``hour``/``minute`` are the send time, which is why the planner's beat
        survives into the game.

        ``deliveries`` stays at 1 deliberately. The game allows 1, 2 or 3, and
        N means N consecutive round trips by the SAME merchant set, auto-resent
        the moment they arrive home -- so it holds `merchants` merchants for
        N x round_trip instead of holding N x merchants for one. That is a lever
        on peak merchant occupancy, which is exactly the budget the optimizer
        allocates, so raising it here without teaching the optimizer about it
        would spend merchants the plan believes are free.

        ``repeatEvery`` is the cycle in hours, and the game fans one request out
        into 24/N daily rows -- so this single call can become up to 24 routes. ``mode`` is "send" (the
        marketplace also has a fetch direction we never use), and
        ``useTradeShips`` is false because this server has no boats.
        """
        return {
            "action": "traderoute",
            "sourceVillageId": route.origin_village_id,
            "targetCoordinates": {"x": route.dest_x, "y": route.dest_y},
            "resources": {r.value: route.cargo.get(r, 0) for r in Resource},
            "mode": "send",
            "hour": route.dispatch_minute // 60,
            "minute": route.dispatch_minute % 60,
            "deliveries": 1,
            "repeatEvery": route.cycle_hours,
            "enabled": True,
            "useTradeShips": False,
        }

    def _build_toggle_payload(self, route_ids: list[int], *, active: bool) -> dict:
        """``PUT /api/v1/trade-routes`` body. VERIFIED against a real request.

        One call carries every route being switched, each as its own
        ``{enabled, id}`` entry -- the capture toggled 24 routes in a single
        request. Note the verb and path: it is a PUT to the same collection
        endpoint as create, not a POST to a separate toggle route.
        """
        return {
            "action": "traderoute",
            "routes": [{"enabled": active, "id": route_id} for route_id in route_ids],
        }

    def _trace_write(
        self,
        kind: str,
        origin: int,
        status: str,
        started: float,
        payload: Any,
        detail: str = "",
    ) -> None:
        """Record a write that reached the game. No-op when untraced."""
        if self.trace is None:
            return
        self.trace.wrote(
            kind=kind,
            origin=origin,
            status=status,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            payload=payload,
            detail=detail,
        )

    def _require_reconciler(self) -> None:
        """Refuse to create when we cannot read what already exists."""
        if not self.reconciler_verified:
            raise TradeRouteReconcilerUnverified(
                "Refusing to create trade routes: the marketplace route-list markup "
                "has not been confirmed, so an unreadable page is indistinguishable "
                "from a village with no routes. Creating on that basis re-creates "
                "the whole plan every run and accumulates duplicates in-game. "
                "Capture /build.php?gid=17&t=3 with a route present, confirm "
                "parse_trade_routes reads it, then set ROUTE_LIST_MARKUP_VERIFIED. "
                "Disabling and previewing are unaffected."
            )

    def _require_live(self) -> None:
        if not self.live_enabled:
            raise TradeRoutePayloadUnverified(
                "Live trade-route writes are disabled. The wire payload is verified "
                "against a captured client request, so this is an explicit opt-in and "
                "not a missing capability: set TRAVIAN_TRADE_ROUTE_LIVE=true to allow "
                "creating and toggling real routes. Preview (dry_run) needs no flag."
            )

    async def create_route(
        self, route: PlannedRoute, *, stop_check: Callable[[], str | None] | None = None
    ) -> RouteActionResult:
        """Create one trade route (LIVE). Gated on ``live_enabled``.

        Paced by a BETWEEN_ROUTES human delay, sent xhr-shaped and
        non-retryable (a committed create that loses its response must not be
        replayed into a duplicate route). Gold Club rejections are mapped to a
        skipped result, not raised.

        ``stop_check`` is consulted AFTER the pacing delay and immediately before
        the POST: if it returns a reason the request is not sent and a "stopped"
        result is returned, so a captcha resolved or budget exhausted DURING the
        human delay cannot let the write through (issues #62/#64).
        """
        self._require_live()
        self._require_reconciler()
        # An execute run is a burst of paced writes, and until now it spent them
        # entirely invisibly to the daily activity ceiling: check_activity_budget
        # was consulted but never fed, so mixing route execution with farm loops
        # under-counted real activity. Same pattern as farm_list_service and
        # scout_ws -- the service that owns the operation reports its seconds.
        started = time.monotonic()
        payload = self._build_create_payload(route)
        await self.http_client.human_delay.wait(ActionType.BETWEEN_ROUTES, "creating trade route")
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "stopped", reason
            )
        try:
            await self.http_client.post_json(
                "/api/v1/trade-routes",
                payload,
                request_type="fetch",
                safe_to_retry=False,
                referer=self._marketplace_referer.get(route.origin_village_id),
            )
        except NetworkError as exc:
            if any(m in str(exc).lower() for m in _GOLDCLUB_MARKERS):
                self._trace_write(
                    "create", route.origin_village_id, "skipped", started, payload, str(exc)
                )
                return RouteActionResult(
                    route.origin_village_id,
                    route.dest_x,
                    route.dest_y,
                    "skipped",
                    "Gold Club required for trade routes (plus.error_goldclub)",
                )
            self._trace_write(
                "create", route.origin_village_id, "failed", started, payload, str(exc)
            )
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "failed", str(exc)
            )
        self._trace_write("create", route.origin_village_id, "created", started, payload)
        return RouteActionResult(route.origin_village_id, route.dest_x, route.dest_y, "created")

    async def _toggle_routes(
        self,
        origin_village_id: int,
        routes: list[ExistingRoute],
        *,
        active: bool,
        stop_check: Callable[[], str | None] | None = None,
    ) -> RouteActionResult | None:
        """Enable/disable a village's existing routes (LIVE). Gated on ``live_enabled``.

        Returns None when there is nothing to toggle (so no request is sent).
        One coarse call for all of the origin's routes, not one per route.
        ``stop_check`` is consulted after the pacing delay, before the POST, so a
        captcha/budget stop during the delay cannot let the toggle through
        (issues #62/#64) — it returns a "stopped" result instead.
        """
        if not routes:
            return None
        self._require_live()
        verb = "enabling" if active else "disabling"
        kind = "enable" if active else "disable"
        started = time.monotonic()
        await self.http_client.human_delay.wait(ActionType.BETWEEN_ROUTES, f"{verb} trade routes")
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(origin_village_id, 0, 0, "stopped", reason)
        payload = self._build_toggle_payload([r.route_id for r in routes], active=active)
        try:
            response = await self.http_client.put_json(
                "/api/v1/trade-routes",
                payload,
                request_type="fetch",
                safe_to_retry=False,
                referer=self._marketplace_referer.get(origin_village_id),
            )
        except NetworkError as exc:
            self._trace_write(kind, origin_village_id, "failed", started, payload, str(exc))
            return RouteActionResult(origin_village_id, 0, 0, "failed", f"{verb} failed: {exc}")

        # Unlike the create, the bulk toggle DOES answer with a body, and the
        # game's own client reads it: it counts `response.routes` entries with an
        # `error` field and reports the failures. Ignoring it meant a request
        # where the game accepted some routes and rejected others reported a
        # clean success for all of them -- a per-route failure hidden behind an
        # overall 200.
        try:
            rejected = self._rejected_routes(response)
        except ToggleResponseUnreadable as exc:
            # Loudly unknown, not quietly fine -- and not FAILED either, which
            # is the over-statement this used to make. The request returned
            # success; what is missing is proof, and the shape the proof was
            # looked for in has never been observed on this account (`docs/15`
            # records the create's 200 body as empty, and records that
            # `routes[].error` came off the game's own `main.js` rather than a
            # reply anyone saw). Reading it as a refusal made every disable,
            # enable and cargo update a failure and sent /revert-plan to report
            # created routes as STILL RUNNING when the game had switched them
            # off. `unverified` is the same verdict the create path reaches on
            # the same evidence, and the caller settles it by reading the
            # marketplace back.
            #
            # The asymmetry the message carries stays: an unreadable ENABLE
            # leaves rows that may still be off, which a later run re-enables
            # harmlessly, while an unreadable DISABLE leaves rows that may
            # still be SHIPPING.
            consequence = (
                "they may still be inactive, and a later run can re-enable them"
                if active
                else "they may still be SHIPPING and have to be checked in-game"
            )
            detail = (
                f"{verb} of {len(routes)} route(s) cannot be confirmed: {exc}. The request "
                f"returned success, so some or all may have gone through -- {consequence}."
            )
            self._trace_write(kind, origin_village_id, "unreadable", started, payload, detail)
            return RouteActionResult(origin_village_id, 0, 0, "unverified", detail)
        if rejected:
            detail = f"{len(rejected)} of {len(routes)} route(s) rejected: {rejected}"
            self._trace_write(kind, origin_village_id, "partial", started, payload, detail)
            return RouteActionResult(origin_village_id, 0, 0, "failed", detail)

        status = "enabled" if active else "disabled"
        self._trace_write(kind, origin_village_id, status, started, payload)
        return RouteActionResult(origin_village_id, 0, 0, status, f"{len(routes)} route(s)")

    @staticmethod
    def _rejected_routes(response: Any) -> list[int]:
        """Route ids the game refused inside an otherwise-successful bulk toggle.

        Shape from the client's own handler: ``response.routes[].error`` marks
        the ones that failed. An empty list therefore means one thing only --
        the game named no failures in a body we could read.

        A body we could NOT read raises :class:`ToggleResponseUnreadable`. The
        old behaviour returned ``[]`` for it, defended as "an unparseable body
        must not invent failures"; the instinct is right and was pointed the
        wrong way. Not inventing failures is not the same as asserting success,
        and the caller reads ``[]`` as "all N went through". A route the game
        never toggled is then reported as toggled.
        """
        if not isinstance(response, dict):
            raise ToggleResponseUnreadable(
                f"the bulk toggle answered with {type(response).__name__}, not an object"
            )
        entries = response.get("routes")
        if not isinstance(entries, list):
            raise ToggleResponseUnreadable(
                "the bulk toggle's answer carried no 'routes' array, so which routes "
                "the game accepted cannot be read"
            )
        rejected: list[int] = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("error"):
                try:
                    rejected.append(int(entry["id"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return sorted(rejected)

    def _build_update_payload(
        self,
        route_ids: list[int],
        cargo: dict[Resource, int],
        *,
        dest_x: int,
        dest_y: int,
    ) -> dict:
        """``PUT /api/v1/trade-routes`` body for a CARGO change. From the client.

        The bulk-edit branch of the game's own bundle builds one partial object
        and stamps each id onto it::

            s = {}; u && (s.targetCoordinates = u)
                    t && (s.resources = {lumber, clay, iron, crop})
                    P !== null && (s.deliveries = P)
                    ...
            k.forEach(e => i.push({...s, id: e}))
            d = {routes: i}

        "Only the fields being changed" was a misreading of that code. The
        dialog never diffs against the original: it seeds itself with every field
        the SELECTED ROWS AGREE on, and then includes each one that is non-null::

            e = x.filter(r => r.carriedResources.lumber !== a.carriedResources.lumber || ...)
            0 === e.length && (s.resources = {...})            // agreed -> sent
            e = x.filter(r => r.repeat !== a.repeat);   0===e.length && (s.repeat = ...)
            e = x.filter(r => r.enabled !== a.enabled); 0===e.length && (s.enabled = ...)
            ...

        Every row this app updates belongs to ONE destination and came from ONE
        create, so they are homogeneous on destination, deliveries, enabled and
        useTradeShips by construction -- exactly the case where a real client
        sends all of them. Sending only ``resources`` was four fields short of
        what the game's own dialog produces in the same situation.

        Note what is still absent, and must stay absent: ``hour`` and ``minute``.
        The bulk form cannot move a departure time -- only the single-route
        ``PUT trade-routes/{id}`` can. That is a gift here rather than a
        limitation: a "route" the operator thinks of as one thing is 24/N rows at
        staggered times, and changing their cargo must not collapse them onto one
        clock.
        """
        fields = {
            "targetCoordinates": {"x": dest_x, "y": dest_y},
            "resources": {r.value: int(cargo.get(r, 0)) for r in Resource},
            # The values every row of a fanned-out route shares, because this app
            # is the thing that created them: see _build_create_payload.
            "deliveries": 1,
            "enabled": True,
            "useTradeShips": False,
        }
        return {
            "action": "traderoute",
            "routes": [{**fields, "id": route_id} for route_id in sorted(route_ids)],
        }

    async def update_cargo(
        self,
        origin_village_id: int,
        routes: list[ExistingRoute],
        cargo: dict[Resource, int],
        *,
        dest_x: int,
        dest_y: int,
        stop_check: Callable[[], str | None] | None = None,
    ) -> RouteActionResult | None:
        """Reset the cargo on existing routes to what the plan now wants (LIVE).

        Without this, a route was created once and never corrected: the plan
        drifts every time production or stocks move, and every already-created
        route keeps the cargo it was born with. Over days the live routes and the
        sheet describe different accounts.

        Deliberately cargo only. Moving a departure time needs the per-route
        endpoint and would collapse a fanned-out set onto one clock; changing the
        destination is not an update at all, it is a different route.
        """
        if not routes:
            return None
        self._require_live()
        started = time.monotonic()
        payload = self._build_update_payload(
            [r.route_id for r in routes], cargo, dest_x=dest_x, dest_y=dest_y
        )
        await self.http_client.human_delay.wait(
            ActionType.BETWEEN_ROUTES, "updating trade route cargo"
        )
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(origin_village_id, 0, 0, "stopped", reason)
        try:
            response = await self.http_client.put_json(
                "/api/v1/trade-routes",
                payload,
                request_type="fetch",
                safe_to_retry=False,
                referer=self._marketplace_referer.get(origin_village_id),
            )
        except NetworkError as exc:
            self._trace_write("update", origin_village_id, "failed", started, payload, str(exc))
            return RouteActionResult(
                origin_village_id, 0, 0, "failed", f"cargo update failed: {exc}"
            )

        try:
            rejected = self._rejected_routes(response)
        except ToggleResponseUnreadable as exc:
            # A cargo correction that did not happen, reported as applied, is
            # the same defect wearing different clothes: the rows keep carrying
            # the figures the plan was supposed to replace.
            #
            # But `failed` was the OTHER over-statement, and the one that bites:
            # this is the same endpoint, the same request shape and the same
            # missing evidence `_toggle_routes` calls `unverified`, and `docs/15`
            # records the empty 200 as the normal body here -- so `failed` was
            # the expected outcome of every cargo correction this app makes. The
            # run reported "0 corrected" and "the live route is still shipping
            # the old amounts" over rows it had just rewritten, and the operator
            # could not tell that from the game genuinely saying no. The caller
            # settles it by reading the marketplace back, as it does for a
            # disable.
            detail = (
                f"cargo update of {len(routes)} route(s) cannot be confirmed: {exc}. The "
                f"request returned success, so some or all may already carry the NEW "
                f"cargo -- the page has to say which."
            )
            self._trace_write("update", origin_village_id, "unreadable", started, payload, detail)
            return RouteActionResult(origin_village_id, 0, 0, "unverified", detail)
        if rejected:
            detail = f"{len(rejected)} of {len(routes)} route(s) rejected: {rejected}"
            self._trace_write("update", origin_village_id, "partial", started, payload, detail)
            return RouteActionResult(origin_village_id, 0, 0, "failed", detail)

        self._trace_write("update", origin_village_id, "updated", started, payload)
        return RouteActionResult(origin_village_id, 0, 0, "updated", f"{len(routes)} route(s)")

    def _build_delete_payload(self, route_ids: list[int]) -> dict:
        """``DELETE /api/v1/trade-routes`` body. Taken from the client's own code.

        The game's marketplace bundle does this, verbatim::

            Travian.api("trade-routes", {data: {action: "traderoute", routes: k}},
                        "DELETE")

        where ``k = e.routes ?? []`` is a flat array of route IDS.

        Note the shape difference from the toggle, which is easy to get wrong and
        impossible to detect from a 200: the bulk PUT sends
        ``routes: [{enabled: bool, id: int}, ...]`` -- objects -- while DELETE
        sends ``routes: [int, ...]`` -- bare ids. Same key, different element
        type, on the same endpoint.
        """
        return {"action": "traderoute", "routes": sorted(route_ids)}

    async def delete_routes(
        self,
        origin_village_id: int,
        routes: list[ExistingRoute],
        *,
        stop_check: Callable[[], str | None] | None = None,
    ) -> RouteActionResult | None:
        """Remove routes from a village's marketplace for good (LIVE).

        The one destructive operation here. Disabling leaves a row that can be
        switched back on; this does not, so it is deliberately never called by
        the reconciler -- a plan that no longer wants a route disables it. This
        exists so a run can be undone, which is the only reason to remove a route
        the operator did not create by hand.

        One request for all of them, matching the UI: it deletes the whole
        selection at once.

        RESPONSE SHAPE, UNVERIFIED. `_rejected_routes` reads
        ``{"routes": [{"id": .., "error": ..}]}``, which is the shape the game's
        own bulk-TOGGLE handler uses in `main.js` (`docs/15`). Nobody has
        observed a DELETE reply on this account at all, so applying that shape
        here is an assumption, not a reading. It is a safe one -- anything this
        parser cannot read becomes `unverified` and is settled by re-reading the
        marketplace -- but it is an assumption, and the first live prune settles
        it: check the `window_pruned` trace event's `status`.
        """
        if not routes:
            return None
        self._require_live()
        started = time.monotonic()
        payload = self._build_delete_payload([r.route_id for r in routes])
        await self.http_client.human_delay.wait(ActionType.BETWEEN_ROUTES, "deleting trade routes")
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(origin_village_id, 0, 0, "stopped", reason)
        try:
            response = await self.http_client.delete_json(
                "/api/v1/trade-routes",
                data=payload,
                request_type="fetch",
                safe_to_retry=False,
                referer=self._marketplace_referer.get(origin_village_id),
            )
        except NetworkError as exc:
            self._trace_write("delete", origin_village_id, "failed", started, payload, str(exc))
            return RouteActionResult(origin_village_id, 0, 0, "failed", f"delete failed: {exc}")

        # Read like the toggles read theirs. The return used to be discarded, so
        # every 2xx was `deleted` -- a body naming per-route errors, an HTML
        # soft-block, or a non-object included. Both callers verify by re-reading
        # the marketplace, but the execute path only re-read on `deleted`, which
        # this always was: one call site from being the only guard on the one
        # irreversible operation here.
        try:
            rejected = self._rejected_routes(response)
        except ToggleResponseUnreadable as exc:
            detail = (
                f"delete of {len(routes)} route(s) cannot be confirmed: {exc}. The request "
                f"returned success, so some or all may already be gone -- the page has to "
                f"say which."
            )
            self._trace_write("delete", origin_village_id, "unreadable", started, payload, detail)
            return RouteActionResult(origin_village_id, 0, 0, "unverified", detail)
        if rejected:
            detail = f"{len(rejected)} of {len(routes)} route(s) rejected: {rejected}"
            self._trace_write("delete", origin_village_id, "partial", started, payload, detail)
            return RouteActionResult(origin_village_id, 0, 0, "failed", detail)

        self._trace_write("delete", origin_village_id, "deleted", started, payload)
        return RouteActionResult(origin_village_id, 0, 0, "deleted", f"{len(routes)} route(s)")

    async def disable_routes(
        self,
        origin_village_id: int,
        routes: list[ExistingRoute],
        *,
        stop_check: Callable[[], str | None] | None = None,
    ) -> RouteActionResult | None:
        """Disable a village's stale routes the plan no longer wants."""
        return await self._toggle_routes(
            origin_village_id, routes, active=False, stop_check=stop_check
        )

    async def enable_routes(
        self,
        origin_village_id: int,
        routes: list[ExistingRoute],
        *,
        stop_check: Callable[[], str | None] | None = None,
    ) -> RouteActionResult | None:
        """Re-enable a village's disabled routes that the plan still wants, rather
        than creating a duplicate. Travian keeps disabled routes in the list, so a
        desired-but-disabled route is restored by re-enabling it."""
        return await self._toggle_routes(
            origin_village_id, routes, active=True, stop_check=stop_check
        )
