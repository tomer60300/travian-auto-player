"""Page navigation simulation.

Real players don't jump directly to build.php?id=5&action=build.
They browse: dorf1 → click the field → see the upgrade page → click upgrade.

This module simulates that navigation chain by pre-loading pages a real
player would visit before performing an action. This creates realistic
server-side access logs and makes the action look organic.

Navigation chains:
- Upgrade resource field: dorf1.php → build.php?id=X (view) → upgrade
- Upgrade building: dorf2.php → build.php?id=X (view) → upgrade
- Send troops: dorf2.php → build.php?gid=16&tt=2 (rally point) → form
- Check reports: dorf1.php → berichte.php
- Map browse: karte.php
"""

import logging
import random
from typing import List, Optional, TYPE_CHECKING

from .human_delay import HumanDelay, DelayProfile

if TYPE_CHECKING:
    from ..clients.http_client import HttpClient

logger = logging.getLogger(__name__)


# Common navigation paths that a real player visits periodically.
# Used for "idle browsing" simulation.
_IDLE_PAGES = [
    "/dorf1.php",
    "/dorf2.php",
    "/berichte.php",        # reports
    "/statistiken.php",     # statistics
    "/spieler.php",         # player profile
    "/karte.php",           # map
    "/nachrichten.php",     # messages
    "/allianz.php",         # alliance
]


class PageNavigator:
    """Simulates realistic page navigation before actions.
    
    Wraps the HTTP client to add pre-navigation page loads,
    creating a realistic browsing trail on the server.
    
    Usage:
        navigator = PageNavigator(http_client, delay)
        await navigator.before_resource_upgrade(slot_id)  # loads dorf1 → build page
        # ... then do the actual upgrade
    """
    
    def __init__(
        self,
        http_client: "HttpClient",
        delay: HumanDelay,
        enabled: bool = True,
    ):
        self._http = http_client
        self._delay = delay
        self.enabled = enabled
        self._last_dorf_visit: float = 0  # track when we last visited dorf pages
    
    async def before_resource_upgrade(self, slot_id: int, village_id: Optional[int] = None) -> None:
        """Navigate as if clicking a resource field to upgrade.
        
        Chain: dorf1.php → build.php?id=X
        """
        if not self.enabled:
            return
        
        newdid = f"?newdid={village_id}" if village_id else ""
        
        # Visit dorf1 (resource overview)
        logger.debug(f"Nav: visiting dorf1 before upgrading slot {slot_id}")
        await self._http.get_html(f"/dorf1.php{newdid}")
        await self._delay.wait(DelayProfile.NAV_STEP, "browsing resource fields")
        
        # Visit the specific build page (player clicks on the field)
        url = f"/build.php?id={slot_id}"
        if village_id:
            url = f"/build.php?newdid={village_id}&id={slot_id}"
        await self._http.get_html(url)
        await self._delay.wait(DelayProfile.PAGE_READ, "reading upgrade details")
    
    async def before_building_upgrade(self, slot_id: int, village_id: Optional[int] = None) -> None:
        """Navigate as if clicking a building to upgrade.
        
        Chain: dorf2.php → build.php?id=X
        """
        if not self.enabled:
            return
        
        newdid = f"?newdid={village_id}" if village_id else ""
        
        # Visit dorf2 (village center)
        logger.debug(f"Nav: visiting dorf2 before upgrading slot {slot_id}")
        await self._http.get_html(f"/dorf2.php{newdid}")
        await self._delay.wait(DelayProfile.NAV_STEP, "browsing village center")
        
        # Visit the specific build page
        url = f"/build.php?id={slot_id}"
        if village_id:
            url = f"/build.php?newdid={village_id}&id={slot_id}"
        await self._http.get_html(url)
        await self._delay.wait(DelayProfile.PAGE_READ, "reading building details")
    
    async def before_troop_send(self, village_id: Optional[int] = None) -> None:
        """Navigate as if going to the rally point to send troops.
        
        Chain: dorf2.php → build.php?gid=16&tt=2
        """
        if not self.enabled:
            return
        
        newdid = f"?newdid={village_id}" if village_id else ""
        
        logger.debug("Nav: visiting dorf2 before troop send")
        await self._http.get_html(f"/dorf2.php{newdid}")
        await self._delay.wait(DelayProfile.NAV_STEP, "browsing to rally point")
    
    async def before_reports(self, village_id: Optional[int] = None) -> None:
        """Navigate as if checking reports.
        
        Chain: dorf1.php (or wherever we are) → berichte.php
        """
        if not self.enabled:
            return
        
        newdid = f"?newdid={village_id}" if village_id else ""
        
        logger.debug("Nav: visiting dorf1 before reports")
        await self._http.get_html(f"/dorf1.php{newdid}")
        await self._delay.wait(DelayProfile.NAV_STEP, "navigating to reports")
    
    async def idle_browse(self) -> None:
        """Simulate idle browsing — visit 1-3 random pages.
        
        Call this periodically during long waits to make the session
        look like a real player casually checking the game.
        """
        if not self.enabled:
            return
        
        num_pages = random.randint(1, 3)
        pages = random.sample(_IDLE_PAGES, min(num_pages, len(_IDLE_PAGES)))
        
        for page in pages:
            logger.debug(f"Idle browse: {page}")
            try:
                await self._http.get_html(page)
            except Exception:
                pass  # don't crash on idle browse failures
            await self._delay.wait(DelayProfile.IDLE_BROWSE, "idle browsing")
    
    async def visit_dorf(self, is_resource: bool, village_id: Optional[int] = None) -> None:
        """Visit dorf1 or dorf2 depending on context."""
        if not self.enabled:
            return
        
        newdid = f"?newdid={village_id}" if village_id else ""
        dorf = "dorf1" if is_resource else "dorf2"
        await self._http.get_html(f"/{dorf}.php{newdid}")
        await self._delay.wait(DelayProfile.PAGE_READ, f"viewing {dorf}")
