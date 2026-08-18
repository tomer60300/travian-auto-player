"""Trade-route execution against Travian's marketplace (Gold Club).

Turns a distribution plan's rows into recurring in-game trade routes. Mirrors
the FarmListService mutation pattern: xhr-shaped POSTs through the stealth
HttpClient, ``safe_to_retry=False`` on every write, a per-origin ``KeyedLock``,
and Gold Club errors mapped rather than raised.

IMPORTANT — the exact ``/api/v1/trade-routes`` request payload has never been
captured from a live client. The wire shape below (``_build_create_payload`` /
``_build_disable_payload``) is a best-effort derived from the documented
endpoint and the farm-list analogy, and is therefore GATED: live creation
raises unless the operator explicitly enables it after capturing/confirming the
real payload (see ``TradeRouteService.live_enabled``). The dry-run path never
touches the game and does not depend on any of this.
"""

from __future__ import annotations

import logging
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


class TradeRouteService:
    """Create/disable Travian trade routes from a distribution plan."""

    def __init__(self, http_client: HttpClient, *, live_enabled: bool = False) -> None:
        self.http_client = http_client
        # Live creation stays OFF until the real payload is captured and this is
        # flipped on deliberately. Guessing the wire shape and firing it at a
        # production account is exactly what this guard prevents.
        self.live_enabled = live_enabled
        self._origin_lock = KeyedLock()

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
        """
        newdid_q = f"?newdid={village_id}" if village_id else ""
        newdid_amp = f"&newdid={village_id}" if village_id else ""
        await self.http_client.get_html(f"/dorf2.php{newdid_q}")
        return await self.http_client.get_html(f"/build.php?gid={MARKETPLACE_GID}{newdid_amp}")

    async def list_existing_routes(self, village_id: int) -> list[ExistingRoute]:
        """Existing trade routes on a village's marketplace, visibility preserved.

        Both visible and hidden entries are returned, each tagged with
        ``visible``. Callers must never *act on* an invisible route (disabling a
        route a human can't see is a pure bot signal — it is a honeypot), but a
        hidden route still occupies its destination, so its coordinates must be
        honored when deduplicating so we don't stack a visible duplicate on top
        of a honeypot. Parsing is best-effort until a real marketplace page is
        captured; an unparseable page yields an empty list rather than a guess.
        """
        html = await self.open_marketplace(village_id)
        from ..parsers.html_parser import parse_trade_routes

        return [
            ExistingRoute(
                route_id=r["route_id"], dest_x=r["dest_x"], dest_y=r["dest_y"], visible=r["visible"]
            )
            for r in parse_trade_routes(html)
        ]

    # ── Write (gated: payload UNVERIFIED) ─────────────────────────────

    def _build_create_payload(self, route: PlannedRoute) -> dict:
        """Best-effort ``POST /api/v1/trade-routes`` body. UNVERIFIED.

        Derived from the documented endpoint + the farm-list slot shape. The
        real field names/nesting must be confirmed from a captured request
        before this is trusted; that is why live creation is gated. Kept in one
        place so finalizing the feature is a single-function edit — send exactly
        what the game sends, no extra fields (an extra field is a fingerprint).
        """
        return {
            "sourceVillageId": route.origin_village_id,
            "x": route.dest_x,
            "y": route.dest_y,
            "resources": {r.value: route.cargo.get(r, 0) for r in Resource},
            "interval": route.cycle_hours,
            "merchants": route.merchants,
            "active": True,
        }

    def _build_disable_payload(self, route_ids: list[int]) -> dict:
        """Best-effort disable/toggle body. UNVERIFIED (see _build_create_payload)."""
        return {"ids": route_ids, "active": False}

    def _require_live(self) -> None:
        if not self.live_enabled:
            raise TradeRoutePayloadUnverified(
                "Live trade-route writes are disabled: the /api/v1/trade-routes "
                "request payload has not been captured and verified yet. Capture a "
                "real create/disable request, confirm _build_*_payload matches it, "
                "then enable live execution."
            )

    async def create_route(self, route: PlannedRoute) -> RouteActionResult:
        """Create one trade route (LIVE). Gated on ``live_enabled``.

        Paced by a BETWEEN_ROUTES human delay, sent xhr-shaped and
        non-retryable (a committed create that loses its response must not be
        replayed into a duplicate route). Gold Club rejections are mapped to a
        skipped result, not raised.
        """
        self._require_live()
        await self.http_client.human_delay.wait(ActionType.BETWEEN_ROUTES, "creating trade route")
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

    async def disable_routes(
        self, origin_village_id: int, routes: list[ExistingRoute]
    ) -> RouteActionResult | None:
        """Disable a village's existing routes (LIVE). Gated on ``live_enabled``.

        Returns None when there is nothing to disable (so no request is sent).
        One coarse call for all of the origin's routes, not one per route.
        """
        if not routes:
            return None
        self._require_live()
        await self.http_client.human_delay.wait(
            ActionType.BETWEEN_ROUTES, "disabling old trade routes"
        )
        try:
            await self.http_client.post_json(
                "/api/v1/trade-routes/toggle-group",
                self._build_disable_payload([r.route_id for r in routes]),
                request_type="xhr",
                safe_to_retry=False,
            )
        except NetworkError as exc:
            return RouteActionResult(origin_village_id, 0, 0, "failed", f"disable failed: {exc}")
        return RouteActionResult(origin_village_id, 0, 0, "disabled", f"{len(routes)} route(s)")
