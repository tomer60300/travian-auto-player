"""Page navigation simulation.

A real player navigates through pages before performing actions:
  login → dorf1 (overview) → click field → upgrade

A bot goes directly to:
  build.php?id=5&action=build&checksum=xxx

This module simulates realistic navigation chains so the server sees
a natural page-view sequence before every action.
"""

import asyncio
import logging
import random
from typing import Optional, TYPE_CHECKING

from .human_delay import HumanDelay, ActionType

if TYPE_CHECKING:
    from ..clients.http_client import HttpClient

logger = logging.getLogger(__name__)


class PageNavigator:
    """Simulates realistic page navigation before actions.
    
    Tracks which pages have been visited recently and ensures
    the correct "parent" pages are loaded before performing actions.
    
    Navigation rules:
    - Resource field upgrade (slot 1-18): must visit /dorf1.php first
    - Building upgrade (slot 19-40): must visit /dorf2.php first  
    - Building detail: must visit /build.php?id=X before action URL
    - Rally point operations: must visit /build.php?gid=16&tt=2 first
    - Map operations: should visit /karte.php first
    
    Each visit generates a real GET request (the server sees it),
    with human-like delays between pages.
    """
    
    def __init__(self, delay: HumanDelay, enabled: bool = True):
        self._delay = delay
        self.enabled = enabled
        self._visited: dict[str, float] = {}  # path -> timestamp of last visit
        self._visit_ttl = 120.0  # seconds before a page "visit" expires
    
    def _is_recently_visited(self, path: str) -> bool:
        """Check if a page was visited recently (within TTL)."""
        import time
        visit_time = self._visited.get(path, 0)
        return (time.monotonic() - visit_time) < self._visit_ttl
    
    def _mark_visited(self, path: str) -> None:
        """Record a page visit."""
        import time
        self._visited[path] = time.monotonic()
    
    async def ensure_dorf1(self, http_client: "HttpClient", village_id: Optional[int] = None) -> None:
        """Ensure dorf1.php (resource overview) has been visited recently."""
        if not self.enabled:
            return
        
        path = "/dorf1.php"
        newdid = f"?newdid={village_id}" if village_id else ""
        full_path = f"{path}{newdid}"
        
        if not self._is_recently_visited(full_path):
            await self._delay.wait(ActionType.PAGE_LOAD, "navigating to resource overview")
            await http_client.get_html(full_path, skip_reauth=True)
            self._mark_visited(full_path)
            logger.debug(f"Navigator: visited {full_path}")
    
    async def ensure_dorf2(self, http_client: "HttpClient", village_id: Optional[int] = None) -> None:
        """Ensure dorf2.php (village overview) has been visited recently."""
        if not self.enabled:
            return
        
        path = "/dorf2.php"
        newdid = f"?newdid={village_id}" if village_id else ""
        full_path = f"{path}{newdid}"
        
        if not self._is_recently_visited(full_path):
            await self._delay.wait(ActionType.PAGE_LOAD, "navigating to village overview")
            await http_client.get_html(full_path, skip_reauth=True)
            self._mark_visited(full_path)
            logger.debug(f"Navigator: visited {full_path}")
    
    async def before_upgrade(self, http_client: "HttpClient", slot_id: int, 
                              village_id: Optional[int] = None) -> None:
        """Navigate to the correct pages before upgrading a building.
        
        Simulates: overview page → building detail page → (then caller does upgrade)
        """
        if not self.enabled:
            return
        
        # Step 1: Visit the correct overview page
        if slot_id <= 18:
            await self.ensure_dorf1(http_client, village_id)
        else:
            await self.ensure_dorf2(http_client, village_id)
        
        # Step 2: Visit the building detail page (like clicking on the building)
        build_path = f"/build.php?id={slot_id}"
        if village_id:
            build_path = f"/build.php?newdid={village_id}&id={slot_id}"
        
        if not self._is_recently_visited(build_path):
            await self._delay.wait(ActionType.CLICK, f"clicking building slot {slot_id}")
            # Note: we don't need to parse the response here — the building_service
            # will fetch it again. This is just to create the page-view in server logs.
            await http_client.get_html(build_path, skip_reauth=True)
            self._mark_visited(build_path)
            logger.debug(f"Navigator: visited {build_path}")
    
    async def before_rally_point(self, http_client: "HttpClient", 
                                  village_id: Optional[int] = None) -> None:
        """Navigate to rally point before sending troops.
        
        Simulates: dorf2 → rally point (send troops tab)
        """
        if not self.enabled:
            return
        
        await self.ensure_dorf2(http_client, village_id)
        
        rp_path = "/build.php?gid=16&tt=2"
        if village_id:
            rp_path = f"/build.php?newdid={village_id}&gid=16&tt=2"
        
        # Rally point visits expire faster (player might send multiple attacks)
        if not self._is_recently_visited(rp_path):
            await self._delay.wait(ActionType.CLICK, "opening rally point")
            # Don't actually fetch — the military service will fetch it
            self._mark_visited(rp_path)
    
    async def before_map_action(self, http_client: "HttpClient") -> None:
        """Visit the map page before scouting/scanning."""
        if not self.enabled:
            return
        
        map_path = "/karte.php"
        if not self._is_recently_visited(map_path):
            await self._delay.wait(ActionType.PAGE_LOAD, "opening map")
            await http_client.get_html(map_path, skip_reauth=True)
            self._mark_visited(map_path)
    
    async def idle_browse(self, http_client: "HttpClient", 
                           village_id: Optional[int] = None) -> None:
        """Simulate idle browsing — random page visit to create noise.
        
        Call this periodically during long waits to make the session
        look like a player checking things.
        """
        if not self.enabled:
            return
        
        pages = [
            "/dorf1.php",
            "/dorf2.php", 
            "/statistiken.php",     # statistics
            "/berichte.php",        # reports
            "/nachrichten.php",     # messages
            "/spieler.php",         # player profile
        ]
        
        page = random.choice(pages)
        newdid = f"?newdid={village_id}" if village_id and "dorf" in page else ""
        
        await self._delay.wait(ActionType.READING, f"casually browsing {page}")
        try:
            await http_client.get_html(f"{page}{newdid}", skip_reauth=True)
            self._mark_visited(f"{page}{newdid}")
            logger.debug(f"Navigator: idle browse {page}")
        except Exception:
            pass  # idle browsing failures are fine
    
    def clear_visited(self) -> None:
        """Clear all visit history (e.g., on session restart)."""
        self._visited.clear()
