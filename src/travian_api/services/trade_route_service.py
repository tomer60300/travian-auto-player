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
        # Live creation stays OFF until the operator turns it on deliberately.
        # The wire format is verified, so this is not a "do we know the shape"
        # guard -- it is the line between previewing a plan and writing to a
        # real account.
        self.live_enabled = live_enabled
        # An instance attribute rather than a module lookup so the gate can be
        # exercised: a caller that genuinely can read route state says so here,
        # and the production default stays False until the markup is captured.
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
        path = f"/build.php?gid={MARKETPLACE_GID}&t=3{newdid_amp}"
        html = await self.http_client.get_html(path)
        base = self.http_client.settings.base_url.rstrip("/")
        self._marketplace_referer[village_id] = f"{base}{path}"
        return html

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

    def _log_activity(self, started: float) -> None:
        """Feed the seconds this write consumed into the daily activity ceiling.

        Accounting must never break a write that already went out, hence the
        broad catch -- the ceiling exists to keep the account looking human, not
        to become a new way for a committed request to fail.
        """
        try:
            self.http_client.activity_scheduler.log_activity(time.monotonic() - started)
        except Exception:  # noqa: BLE001 - accounting must not break traffic
            logger.debug("activity accounting failed for a trade-route write", exc_info=True)

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
        self._log_activity(started)
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
            await self.http_client.put_json(
                "/api/v1/trade-routes",
                payload,
                request_type="fetch",
                safe_to_retry=False,
                referer=self._marketplace_referer.get(origin_village_id),
            )
        except NetworkError as exc:
            self._trace_write(kind, origin_village_id, "failed", started, payload, str(exc))
            return RouteActionResult(origin_village_id, 0, 0, "failed", f"{verb} failed: {exc}")
        self._log_activity(started)
        status = "enabled" if active else "disabled"
        self._trace_write(kind, origin_village_id, status, started, payload)
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
