"""Trade-route execution against Travian's marketplace (Gold Club).

Turns a distribution plan's rows into recurring in-game trade routes. Mirrors
the FarmListService mutation pattern: xhr-shaped POSTs through the stealth
HttpClient, ``safe_to_retry=False`` on every write, a per-origin ``KeyedLock``,
and Gold Club errors mapped rather than raised.

The wire format is VERIFIED against a real client request captured from
Europe 2 on 2026-08-20 (gpack 597.6): ``POST /api/v1/trade-routes`` to create
(201, empty body) and ``PUT /api/v1/trade-routes`` to enable/disable in bulk
(200). ``tests/test_trade_route_payload.py`` pins both shapes to that capture.

Live writes remain OFF by default behind ``TradeRouteService.live_enabled``
(``TRAVIAN_TRADE_ROUTE_LIVE``), because the payload being correct is necessary
but not sufficient: creating routes mutates a real account. The dry-run path
never touches the game and does not depend on any of this.

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
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from ..clients.http_client import HttpClient
from ..concurrency import KeyedLock
from ..exceptions import NetworkError, TravianError
from ..services.distribution.allocation import Resource
from ..stealth.human_delay import ActionType

logger = logging.getLogger(__name__)

# Marketplace building id (gid=17); its page lists a village's trade routes.
MARKETPLACE_GID = 17

# Gold Club rejection marker, matched case-insensitively (as FarmListService does).
_GOLDCLUB_MARKERS = ("goldclub", "gold club", "plus.error")


class TradeRoutePayloadUnverified(TravianError):
    """Raised when live creation is attempted before the wire payload is confirmed."""


class TradeRouteReconcilerUnverified(TravianError):
    """Raised when a route would be CREATED without being able to read existing ones."""


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


# Whether parse_trade_routes has been confirmed against real gid=17&t=3 markup.
#
# This gates CREATION specifically, and the asymmetry is the point. The parser
# returns [] for markup it does not recognise, which is the safe answer for
# disabling -- an empty list disables nothing. It is the DANGEROUS answer for
# creating, because the reconciler reads [] as "this village has no routes" and
# creates the whole plan again. Every run. Duplicates accumulate in-game and the
# repeated identical creates are exactly the daily rebuild-the-same-routes
# pattern the code elsewhere says it avoids.
#
# The 2026-08-20 capture recorded the POST and PUT bodies but not the page HTML,
# so the row markup (`data-route-id`, `data-x`, `data-y`) is still a guess.
# To lift this: save the HTML of /build.php?gid=17&t=3 with at least one route
# present, confirm parse_trade_routes finds it, add that page as a fixture, and
# set this True.
ROUTE_LIST_MARKUP_VERIFIED = True


@dataclass(frozen=True)
class PlannedRoute:
    """One route to create: origin village → destination coordinates."""

    origin_village_id: int
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


@dataclass
class RouteActionResult:
    """Outcome of one create/disable action (or its dry-run preview)."""

    origin_village_id: int
    dest_x: int
    dest_y: int
    status: str  # "would_create" | "created" | "would_disable" | "disabled" | "failed" | "skipped"
    detail: str = ""


@dataclass
class ExistingRoute:
    """A trade route already present on a village's marketplace page."""

    route_id: int
    dest_x: int
    dest_y: int
    visible: bool = True  # UI-visible; hidden entries are treated as honeypots
    # Whether the route is currently ENABLED. Travian keeps disabled routes in
    # the list (they can be re-enabled), so a visible row at a desired coordinate
    # is NOT necessarily satisfying the plan — only an enabled one is. Defaults
    # True so a parser that cannot read the flag errs toward "leave it alone".
    active: bool = True


class TradeRouteService:
    """Create/disable Travian trade routes from a distribution plan."""

    def __init__(
        self,
        http_client: HttpClient,
        *,
        live_enabled: bool = False,
        reconciler_verified: bool = ROUTE_LIST_MARKUP_VERIFIED,
    ) -> None:
        self.http_client = http_client
        # Live creation stays OFF until the real payload is captured and this is
        # flipped on deliberately. Guessing the wire shape and firing it at a
        # production account is exactly what this guard prevents.
        self.live_enabled = live_enabled
        # An instance attribute rather than a module lookup so the gate can be
        # exercised: a caller that genuinely can read route state says so here,
        # and the production default stays False until the markup is captured.
        self.reconciler_verified = reconciler_verified
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
        loop last touched (a bare gid=17 with a stale Referer is a tell). The
        stealth layer advances the Referer chain across the two page GETs
        automatically. The marketplace GET doubles as the read of existing
        routes, so "disable old routes if needed" needs no further request.

        ``newdid`` rides on BOTH GETs, matching the codebase convention
        (building_service, oasis_raider): keeping it on the data request pins the
        village context, so a concurrent same-session request switching the
        active village between the two GETs cannot make us read — and disable —
        the wrong village's routes.
        """
        newdid_q = f"?newdid={village_id}" if village_id else ""
        newdid_amp = f"&newdid={village_id}" if village_id else ""
        await self.http_client.get_html(f"/dorf2.php{newdid_q}")
        # `t=3` is the trade-route tab. Without it we never load the tab the
        # routes live on -- so the reconciler read a page that cannot contain
        # them, and a server-side "did this session render the trade-route tab
        # before POSTing to it?" check would fail outright.
        return await self.http_client.get_html(f"/build.php?gid={MARKETPLACE_GID}&t=3{newdid_amp}")

    async def list_existing_routes(self, village_id: int) -> list[ExistingRoute]:
        """Existing trade routes on a village's marketplace, visibility preserved.

        Both visible and hidden entries are returned, each tagged with
        ``visible``, so the caller can tell them apart. Hidden entries are
        honeypots — a human can't see them — and the reconciler ignores them
        entirely: it never disables one (acting on an invisible route is a pure
        bot signal) and never lets one influence a create decision (conditioning
        behavior on invisible data is the same tell in reverse). Parsing is
        best-effort until a real marketplace page is captured; an unparseable
        page yields an empty list rather than a guess. That is the safe answer
        for DISABLING and the dangerous one for creating, which is why creation
        is gated separately on ROUTE_LIST_MARKUP_VERIFIED.
        """
        html = await self.open_marketplace(village_id)
        from ..parsers.html_parser import parse_trade_routes, trade_route_page_recognised

        if not trade_route_page_recognised(html):
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
                dest_x=r["dest_x"],
                dest_y=r["dest_y"],
                visible=r["visible"],
                active=r.get("active", True),
            )
            for r in parse_trade_routes(html)
        ]

    # ── Write (gated: live opt-in; payload verified) ────────────────────────────

    def _build_create_payload(self, route: PlannedRoute) -> dict:
        """``POST /api/v1/trade-routes`` body. VERIFIED against a real request.

        Field-for-field from the capture, and deliberately nothing more: an
        extra field the client never sends is a fingerprint.

        ``hour``/``minute`` are the send time, which is why the planner's beat
        survives into the game. ``deliveries`` was 1 in the capture and is left
        at 1 -- a route sends one load per cycle. ``mode`` is "send" (the
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
        await self.http_client.human_delay.wait(ActionType.BETWEEN_ROUTES, "creating trade route")
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "stopped", reason
            )
        try:
            await self.http_client.post_json(
                "/api/v1/trade-routes",
                self._build_create_payload(route),
                request_type="xhr",
                safe_to_retry=False,
            )
        except NetworkError as exc:
            if any(m in str(exc).lower() for m in _GOLDCLUB_MARKERS):
                return RouteActionResult(
                    route.origin_village_id,
                    route.dest_x,
                    route.dest_y,
                    "skipped",
                    "Gold Club required for trade routes (plus.error_goldclub)",
                )
            return RouteActionResult(
                route.origin_village_id, route.dest_x, route.dest_y, "failed", str(exc)
            )
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
        await self.http_client.human_delay.wait(ActionType.BETWEEN_ROUTES, f"{verb} trade routes")
        if stop_check is not None and (reason := stop_check()):
            return RouteActionResult(origin_village_id, 0, 0, "stopped", reason)
        try:
            await self.http_client.put_json(
                "/api/v1/trade-routes",
                self._build_toggle_payload([r.route_id for r in routes], active=active),
                request_type="xhr",
                safe_to_retry=False,
            )
        except NetworkError as exc:
            return RouteActionResult(origin_village_id, 0, 0, "failed", f"{verb} failed: {exc}")
        status = "enabled" if active else "disabled"
        return RouteActionResult(origin_village_id, 0, 0, status, f"{len(routes)} route(s)")

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
