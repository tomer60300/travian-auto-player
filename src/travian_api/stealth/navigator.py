"""Page navigation simulation.

A real player doesn't jump directly to build.php?id=15&action=build.
They navigate: overview → dorf1/dorf2 → click building → click upgrade.

This module simulates that navigation chain, making intermediate page
requests with realistic timing to create a believable browsing pattern.
"""

import logging
import random
from typing import TYPE_CHECKING, Optional

from .human_delay import ActionType, HumanDelay

if TYPE_CHECKING:
    from ..clients.http_client import HttpClient

logger = logging.getLogger(__name__)

# ── Where the village selector goes ────────────────────────────────────
#
# LAST. Always last, on every URL that carries one.
#
# All 23 village-scoped loads in the 2026-09-15 capture agree, and they agree
# across four different page types::
#
#     /karte.php?zoom=1&newdid=61837
#     /build.php?id=30&gid=17&t=2&newdid=64215
#     /dorf1.php?id=14&gid=1&as=ccI9Tbn0kPZtXJmk&newdid=30540
#
# which is what you would expect from how the game produces them: the markup
# renders the link for the page, and the village selector appends itself to
# whatever that link already was. Leading with `newdid` is a shape its own
# markup cannot emit, and four call sites here and in the services were doing
# exactly that -- `/build.php?newdid=123&id=30` -- while four others put it
# last. The parameters are order-independent to the SERVER, so this was free to
# get wrong and free to fix; it is not free to leave wrong, because "same
# parameters, unusual order" is the cheapest possible clustering feature and
# one of the few that survives every layer of timing noise beneath it.
#
# ── Warm-up navigation model ───────────────────────────────────────────
#
# The pages, their URLs and their weights all come from a recorded human
# session: 1,631 requests, 2026-09-15, Europe 2, one player raiding, reading
# reports and setting up trade routes. Every top-level page it touched is here
# and nothing else is, which is the point -- see below.
#
# Two of these URLs used to be wrong, and wrong in the way that matters most.
# We browsed ``/statistiken.php`` and ``/spieler.php``: the GERMAN route names,
# on an English server, from a gpack whose own links are ``/statistics`` and
# ``/report``. The capture contains one statistics visit and it goes to
# ``/statistics``; ``/statistiken.php`` and ``/spieler.php`` appear nowhere in
# 1,631 requests. Whether the old routes still answer is beside the point --
# a session that reaches for a URL the client cannot generate has said
# something about itself that no amount of timing jitter takes back, and it is
# precisely the signal a deprecated-path trap is built to collect. So the table
# is evidence, not recollection: a page goes in when the capture shows a player
# loading it, at the URL the capture shows.
#
# The profile page left the set entirely. It is surely reachable somehow, but
# the player never opened it, so there is no observed URL to use -- and
# guessing is the mistake this table exists to stop.
PAGE_PATHS = {
    "dorf1": "/dorf1.php",
    "dorf2": "/dorf2.php",
    "karte": "/karte.php",
    "report": "/report",
    "statistics": "/statistics",
    # The troop overview. Bare ``gid``, no slot: unlike the marketplace, all
    # four of the capture's visits address it this way.
    "troops": "/build.php?gid=19",
}
_PAGE_BY_PATH = {path: page for page, path in PAGE_PATHS.items()}
# Top-level pages a real player browses right after login. All are coherent
# navigation targets from one another, so any transition keeps the Referer
# chain truthful (no impossible jumps).
_WARMUP_PAGES = tuple(PAGE_PATHS)
# Pages that take a ?newdid= village selector (the global pages don't).
_WARMUP_PAGE_TAKES_NEWDID = frozenset({"dorf1", "dorf2", "karte"})
_WARMUP_PAGE_DESC = {
    "dorf1": "checking resource overview",
    "dorf2": "looking at village buildings",
    "karte": "glancing at the map",
    "report": "reading through reports",
    "statistics": "checking statistics",
    "troops": "checking troop numbers",
}
# Pre-persona destination affinity: how commonly a page is visited at all.
# Ordered as the capture's own document counts order them -- dorf1 30, karte
# 20, report 13, dorf2 4, statistics 1 -- rather than by what felt plausible,
# which had dorf2 at 0.9 and second place. It is second-to-last: a player opens
# the village view to go SOMEWHERE (its four loads there all precede a
# building), and almost never to look at it.
#
# This stays realistic rather than flat on purpose. A *human population* also
# returns to the overview more than it opens statistics, so the skew is not a
# bot discriminator. Per-account spread (the wide page-bias below) is what
# defeats clustering, not flattening this.
_WARMUP_PAGE_AFFINITY = {
    "dorf1": 1.0,
    "karte": 0.7,
    "report": 0.45,
    "dorf2": 0.3,
    "troops": 0.25,
    "statistics": 0.1,
}
# Base stop weight, scaled per-account by a stop bias.
_WARMUP_STOP_BASE = 0.9
# Absolute hard cap on extra pages after the initial dorf1 landing (a
# per-account length cap is drawn within this).
_WARMUP_MAX_STEPS = 7


class PageNavigator:
    """Simulates realistic page navigation patterns.

    Before performing an action, navigates through the pages a human
    would visit. The intermediate page loads aren't wasted — they help
    maintain session state and cookies while creating natural traffic.

    Navigation chains:
    - Upgrade resource field: dorf1.php → build.php?id=X → upgrade
    - Upgrade building: dorf2.php → build.php?id=X → upgrade
    - Send troops: dorf2.php → build.php?gid=16&tt=2 → confirm
    - Check reports: dorf1.php → berichte.php
    - View map: dorf1.php → karte.php
    """

    def __init__(
        self,
        http_client: "HttpClient",
        human_delay: HumanDelay,
        enabled: bool = True,
    ):
        self._http = http_client
        self._delay = human_delay
        self.enabled = enabled
        # Warm-up navigation is a per-persona first-order Markov chain. Default
        # from the global RNG; bound to the persona via seed_routes() so each
        # account has a stable browsing "personality" rather than one shared
        # deterministic (or shared-distribution) route.
        self._init_route_prefs(random)

    def _init_route_prefs(self, rng: "random.Random") -> None:
        """Build the per-persona warm-up transition matrix from ``rng``.

        ``_route_transitions[from_page]`` is a probability distribution over
        the next page (or ``None`` = stop). To defeat *cross-account* clustering
        (transition-count chi-square / likelihood-ratio, route-length KS,
        edit-distance), each account draws several persona-stable behavioral
        motifs, giving the population a broad mixture rather than one shared
        curve:

        - a wide per-page *bias* (a coherent browsing "personality": this
          account may favor the map, another may favor profiles);
        - a small *self-loop* tendency, so reloading the current page is
          possible — a hard-zero diagonal is itself a regularity humans lack;
        - a *stop bias* and a per-account *max length*, so route lengths form a
          broad family, not one shared length distribution.

        The per-call realization still varies (sampled from the global RNG in
        ``warm_up``); only these motifs are persona-stable.
        """
        # Coherent per-account page personality (wide prior) + reload tendency,
        # stop bias, and browse-length cap. The page bias is kept as an
        # attribute so mid-session idle browsing (idle_browse) draws from the
        # same personality instead of a fleet-uniform random.choice.
        self._route_page_bias = {p: rng.uniform(0.2, 2.5) for p in _WARMUP_PAGES}
        self_loop_bias = rng.uniform(0.02, 0.30)  # floored: a reload is always possible
        stop_bias = rng.uniform(0.4, 2.2)
        self._route_max_steps = rng.randint(4, _WARMUP_MAX_STEPS)

        self._route_transitions: dict[str, dict[Optional[str], float]] = {}
        for frm in _WARMUP_PAGES:
            weights: dict[Optional[str], float] = {}
            for to in _WARMUP_PAGES:
                weight = (
                    _WARMUP_PAGE_AFFINITY[to] * self._route_page_bias[to] * rng.uniform(0.6, 1.4)
                )
                if to == frm:
                    weight *= self_loop_bias  # rare reload, not a structural zero
                weights[to] = weight
            weights[None] = _WARMUP_STOP_BASE * stop_bias  # persona browse length
            total = sum(weights.values())
            self._route_transitions[frm] = {k: v / total for k, v in weights.items()}

    def seed_routes(self, identity: str) -> None:
        """Bind the warm-up transition matrix to a stable persona identity."""
        self._init_route_prefs(random.Random(identity))

    def _next_route_step(self, current: str) -> Optional[str]:
        """Sample the next warm-up page (or None=stop) from the global RNG."""
        r = random.random()
        cumulative = 0.0
        target: Optional[str] = None
        for target, prob in self._route_transitions[current].items():
            cumulative += prob
            if r < cumulative:
                return target
        return target  # numerical-tail fallback: last item (often stop)

    @staticmethod
    def _warmup_page_path(page: str, newdid: str) -> str:
        """The URL for a warm-up page, from the table rather than from its name.

        It used to be ``f"/{page}.php"``, which is why two of these pages were
        addressed at URLs the client does not use: the rule quietly asserted
        that every page is ``<name>.php``, and this gpack's statistics and
        report pages are extensionless.
        """
        path = PAGE_PATHS[page]
        return path + newdid if page in _WARMUP_PAGE_TAKES_NEWDID else path

    @staticmethod
    def _page_key(path: Optional[str]) -> Optional[str]:
        """Map a visited path back to a warm-up page key, or None if it isn't one."""
        if not path:
            return None
        # Exact first: the troop overview IS its query string, and /build.php
        # carrying anything else is a different page entirely -- a marketplace,
        # a rally point, a building. Stripping the query would call all of them
        # the troop overview.
        if path in _PAGE_BY_PATH:
            return _PAGE_BY_PATH[path]
        return _PAGE_BY_PATH.get(path.split("?", 1)[0])

    def _next_idle_page(self, current: str) -> str:
        """First-order Markov transition to the next idle page (excludes stop).

        Reuses the persona transition matrix so idle browsing has the same
        transition structure as warm_up — otherwise idle transitions look
        memoryless while warm-up transitions don't, an inconsistency a
        first-order-Markov likelihood-ratio test could exploit.
        """
        row = self._route_transitions[current]
        pages = [p for p in row if p is not None]
        weights = [row[p] for p in pages]
        return random.choices(pages, weights=weights, k=1)[0]

    @property
    def _current_page(self) -> Optional[str]:
        """Where this SESSION actually is, derived from the Referer's own field.

        This used to be a field with a single writer (`_visit`), while the field
        the Referer is built from is written by EVERY page load -- and dozens of
        call sites go straight to `get_html` without passing through here. So the
        navigator could believe it was still on the farm-list tab while the wire
        had moved to /dorf1.php. That was harmless while every chain was walked
        unconditionally, but once the walk could be SKIPPED on a match, a stale
        belief meant skipping the navigation and then sending a farm-list POST
        refered from a page that has no farm-list form -- which is the exact
        "impossible from a real browser" tell the Referer work exists to avoid.

        Deriving it from the same field makes that divergence impossible rather
        than unlikely: if the wire moved, this moved.
        """
        return self._http.browser_headers.last_page_path

    @property
    def current_page(self) -> Optional[str]:
        return self._current_page

    async def _visit(self, path: str, context: str = "") -> str:
        """Visit a page with delay and tracking."""
        await self._delay.wait(ActionType.PAGE_LOAD, context or f"visiting {path}")
        # No separate bookkeeping: get_html updates the Referer's page field, and
        # `_current_page` reads that. One source of truth.
        return await self._http.get_html(path, skip_reauth=True)

    async def warm_up(self, village_id: Optional[int] = None) -> Optional[str]:
        """Post-login warm-up sequence. Loads pages a real player would visit.

        Simulates: login -> resource overview -> village center -> maybe stats -> back
        This prevents "login -> immediate API blast" detection patterns.

        Returns the landing page's HTML so the caller does not have to fetch a
        page we have already loaded. Loading /dorf1.php twice in a row, back to
        back, is not something a browser does.
        """
        if not self.enabled:
            return None

        logger.debug("Running post-login warm-up sequence")
        newdid = f"?newdid={village_id}" if village_id else ""

        # Always land on dorf1 first (the post-login landing page) so we never
        # "login -> immediate API blast". From there, walk a per-persona
        # first-order Markov chain over top-level pages: each step's next page
        # (or stop) is drawn from this account's stable transition matrix, so
        # both the visited set AND the transition structure are persona-
        # specific and vary per call — no single n-gram / Markov signature is
        # shared across accounts. All pages are coherent navigation targets, so
        # the Referer chain stays truthful, and the walk is bounded.
        landing = await self._visit(f"/dorf1.php{newdid}", "checking resource overview after login")

        current = "dorf1"
        for _ in range(self._route_max_steps):
            nxt = self._next_route_step(current)
            if nxt is None:
                break  # this account's browse ended here
            await self._visit(self._warmup_page_path(nxt, newdid), _WARMUP_PAGE_DESC[nxt])
            current = nxt
        logger.debug("Warm-up sequence complete")
        return landing

    async def navigate_to_resource_field(
        self, slot_id: int, village_id: Optional[int] = None
    ) -> None:
        """Navigate to a resource field (slot 1-18) as a human would.

        Chain: dorf1.php → build.php?id=X
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""

        # Visit dorf1 first (resource field overview)
        if self._current_page != f"/dorf1.php{newdid}":
            await self._visit(f"/dorf1.php{newdid}", "viewing resource fields")

        # Small click delay before opening the field
        await self._delay.wait(ActionType.CLICK, f"clicking field slot {slot_id}")

        # Actually fetch the building page (creates realistic referer chain)
        build_url = f"/build.php?id={slot_id}"
        if village_id:
            build_url = f"/build.php?id={slot_id}&newdid={village_id}"
        await self._visit(build_url, f"opening resource field slot {slot_id}")

    async def navigate_to_building(self, slot_id: int, village_id: Optional[int] = None) -> None:
        """Navigate to a village building (slot 19-40) as a human would.

        Chain: dorf2.php → build.php?id=X
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""

        # Visit dorf2 first (building overview)
        if self._current_page != f"/dorf2.php{newdid}":
            await self._visit(f"/dorf2.php{newdid}", "viewing village buildings")

        # Small click delay
        await self._delay.wait(ActionType.CLICK, f"clicking building slot {slot_id}")

        # Actually fetch the building page (creates realistic referer chain)
        build_url = f"/build.php?id={slot_id}"
        if village_id:
            build_url = f"/build.php?id={slot_id}&newdid={village_id}"
        await self._visit(build_url, f"opening building slot {slot_id}")

    async def navigate_to_rally_point(self, village_id: Optional[int] = None) -> None:
        """Navigate to rally point as a human would.

        Chain: dorf2.php → build.php?gid=16&tt=2
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""
        newdid_amp = f"&newdid={village_id}" if village_id else ""

        # Already here: nothing to navigate. Checked before the dorf2 hop, or
        # "go to the rally point" would walk away from the rally point and back.
        rally_url = f"/build.php?gid=16&tt=2{newdid_amp}"
        if self._current_page == rally_url:
            return

        # Visit dorf2 first
        if self._current_page != f"/dorf2.php{newdid}":
            await self._visit(f"/dorf2.php{newdid}", "viewing village")

        await self._delay.wait(ActionType.CLICK, "clicking rally point")

        # Actually fetch the rally point page so referer chain is truthful
        await self._visit(rally_url, "opening rally point")

    async def navigate_to_map(self, village_id: Optional[int] = None) -> None:
        """Navigate to the world map page (/karte.php).

        Map tile XHRs (map/position, tile-details) are fired by frontend JS
        from the map page. If we POST those without first opening
        karte.php, the Referer chain points at whatever page we last
        visited — a clear desync from how a real browser produces those
        requests.
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""

        if self._current_page != f"/karte.php{newdid}":
            await self._visit(f"/karte.php{newdid}", "opening world map")

    async def navigate_to_farm_list(self, village_id: Optional[int] = None) -> None:
        """Navigate to the farm-list edit page on the rally point.

        Chain: dorf2.php → rally point → farm-list tab. Used before any
        bulk farm-list mutation API (slot add/remove, send) so the
        Referer/Origin header chain matches what the browser would send
        when a player is editing the list through the UI.
        """
        if not self.enabled:
            return

        newdid_amp = f"&newdid={village_id}" if village_id else ""
        farm_url = f"/build.php?gid=16&tt=99{newdid_amp}"

        # Already looking at this village's farm-list tab: a player sending
        # their second list does not reload dorf2, the rally point and the tab
        # to get back to the page they are already on. Re-walking it made a
        # farm cycle issue the same three GETs once per list -- both wasted
        # budget and a repetition no session produces. The caller groups lists
        # by owner village precisely so this context holds.
        if self._current_page == farm_url:
            return

        await self.navigate_to_rally_point(village_id)
        await self._delay.wait(ActionType.CLICK, "opening farm list tab")
        await self._visit(farm_url, "opening farm-list tab")

    async def pre_construct_flow(self, slot_id: int, village_id: Optional[int] = None) -> None:
        """Full navigation flow before constructing a NEW building.

        Mirrors the upgrade pre-flow but for empty slots: dorf2 → click
        empty slot (opens construction picker) → review options. The
        actual construct POST happens after this returns.
        """
        if not self.enabled:
            return

        await self.navigate_to_building(slot_id, village_id)
        await self._delay.wait(ActionType.DECISION, "reviewing buildings to construct")

    async def navigate_to_reports(self) -> None:
        """Navigate to reports page."""
        if not self.enabled:
            return

        # Optionally visit dorf1 first (50% chance)
        if random.random() < 0.5 and self._current_page != "/dorf1.php":
            await self._visit("/dorf1.php", "checking overview")

        await self._delay.wait(ActionType.CLICK, "clicking reports")

    async def idle_browse(self, village_id: Optional[int] = None) -> None:
        """Simulate idle browsing — a persona-weighted page visit during waits.

        The page is drawn from this account's stable page personality (the same
        per-account affinity ``warm_up`` uses), not a flat ``random.choice``: a
        uniform pick shared across the fleet is clusterable by a visit-frequency
        chi-square, whereas a persona-weighted draw gives each account a
        distinct, internally consistent idle pattern. The realistic base
        affinity (overviews > profile/stats) keeps the aggregate human-like.
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""
        # If we're on a known top-level page, take a first-order Markov step
        # (same chain as warm_up). Otherwise fall back to the persona-weighted
        # marginal so the page is still account-distinct, not fleet-uniform.
        current = self._page_key(self._current_page)
        if current is not None:
            page = self._next_idle_page(current)
        else:
            pages = list(_WARMUP_PAGES)
            weights = [_WARMUP_PAGE_AFFINITY[p] * self._route_page_bias[p] for p in pages]
            page = random.choices(pages, weights=weights, k=1)[0]
        await self._visit(
            self._warmup_page_path(page, newdid), f"idle browsing: {_WARMUP_PAGE_DESC[page]}"
        )

    async def pre_upgrade_flow(self, slot_id: int, village_id: Optional[int] = None) -> None:
        """Full navigation flow before upgrading a building/field.

        Simulates: overview → slot page → (read costs) → upgrade click
        """
        if not self.enabled:
            return

        if slot_id <= 18:
            await self.navigate_to_resource_field(slot_id, village_id)
        else:
            await self.navigate_to_building(slot_id, village_id)

        # "Read" the building page (human checks costs/time)
        await self._delay.wait(ActionType.DECISION, "reviewing upgrade costs")

    async def pre_send_troops_flow(self, village_id: Optional[int] = None) -> None:
        """Full navigation flow before sending troops.

        Simulates: dorf2 → rally point → fill form → send
        """
        if not self.enabled:
            return

        await self.navigate_to_rally_point(village_id)
        await self._delay.wait(ActionType.FORM_FILL, "filling troop form")
