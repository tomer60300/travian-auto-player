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
        self._current_page: Optional[str] = None

    @property
    def current_page(self) -> Optional[str]:
        return self._current_page

    async def _visit(self, path: str, context: str = "") -> str:
        """Visit a page with delay and tracking."""
        await self._delay.wait(ActionType.PAGE_LOAD, context or f"visiting {path}")
        html = await self._http.get_html(path, skip_reauth=True)
        self._current_page = path
        return html

    async def warm_up(self, village_id: Optional[int] = None) -> None:
        """Post-login warm-up sequence. Loads pages a real player would visit.

        Simulates: login -> resource overview -> village center -> maybe stats -> back
        This prevents "login -> immediate API blast" detection patterns.
        """
        if not self.enabled:
            return

        logger.debug("Running post-login warm-up sequence")
        newdid = f"?newdid={village_id}" if village_id else ""

        # 1. Resource overview (dorf1) — already the landing page after login
        await self._visit(f"/dorf1.php{newdid}", "checking resource overview after login")

        # 2. Village center
        await self._visit(f"/dorf2.php{newdid}", "looking at village buildings")

        # 3. Optional: curiosity browsing (20% chance each)
        if random.random() < 0.2:
            await self._visit("/statistiken.php", "checking statistics")
        if random.random() < 0.1:
            await self._visit("/spieler.php", "checking own profile")

        # 4. Return to resource overview (common human pattern)
        await self._visit(f"/dorf1.php{newdid}", "returning to resource overview")
        logger.debug("Warm-up sequence complete")

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
            build_url = f"/build.php?newdid={village_id}&id={slot_id}"
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
            build_url = f"/build.php?newdid={village_id}&id={slot_id}"
        await self._visit(build_url, f"opening building slot {slot_id}")

    async def navigate_to_rally_point(self, village_id: Optional[int] = None) -> None:
        """Navigate to rally point as a human would.

        Chain: dorf2.php → build.php?gid=16&tt=2
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""
        newdid_amp = f"&newdid={village_id}" if village_id else ""

        # Visit dorf2 first
        if self._current_page != f"/dorf2.php{newdid}":
            await self._visit(f"/dorf2.php{newdid}", "viewing village")

        await self._delay.wait(ActionType.CLICK, "clicking rally point")

        # Actually fetch the rally point page so referer chain is truthful
        rally_url = f"/build.php?gid=16&tt=2{newdid_amp}"
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

        await self.navigate_to_rally_point(village_id)
        await self._delay.wait(ActionType.CLICK, "opening farm list tab")
        newdid_amp = f"&newdid={village_id}" if village_id else ""
        farm_url = f"/build.php?gid=16&tt=99{newdid_amp}"
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
        """Simulate idle browsing — random page visits that a human might do.

        Call this occasionally during long waits to maintain session
        and create background traffic noise.
        """
        if not self.enabled:
            return

        newdid = f"?newdid={village_id}" if village_id else ""

        # Pick a random "idle" action
        actions = [
            (f"/dorf1.php{newdid}", "checking resources"),
            (f"/dorf2.php{newdid}", "looking at village"),
            ("/statistiken.php", "checking statistics"),
            ("/spieler.php", "checking profile"),
        ]

        path, desc = random.choice(actions)
        await self._visit(path, f"idle browsing: {desc}")

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
