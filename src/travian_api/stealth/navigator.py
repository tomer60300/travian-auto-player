"""Page navigation simulation.

Real players don't jump directly to build.php?id=15&action=build.
They navigate: overview → village view → click building → upgrade.

This module simulates natural navigation patterns so the request
sequence looks like a human browsing the game.
"""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..clients.http_client import HttpClient

from .human_delay import HumanDelay
from .headers import BrowserHeaders

logger = logging.getLogger(__name__)


# Navigation patterns: what pages a human would visit before each action
_NAV_PATTERNS = {
    # Upgrading a resource field (slot 1-18)
    "upgrade_resource": ["/dorf1.php"],
    
    # Upgrading a village building (slot 19-40)
    "upgrade_building": ["/dorf2.php"],
    
    # Sending troops (rally point)
    "send_troops": ["/dorf2.php"],
    
    # Checking reports
    "view_reports": ["/dorf1.php"],
    
    # Viewing map
    "view_map": ["/dorf2.php"],
}


class PageNavigator:
    """Simulates realistic page navigation before performing actions.
    
    Tracks which pages have been recently visited to avoid redundant
    navigation (a human doesn't reload dorf1 if they're already there).
    
    Usage:
        navigator = PageNavigator(http_client, delay, headers)
        await navigator.navigate_to_building(slot_id=15, village_id=41699)
        # Now safe to call the actual upgrade
    """
    
    def __init__(
        self,
        http_client: "HttpClient" = None,
        delay: HumanDelay = None,
        headers: BrowserHeaders = None,
        enabled: bool = True,
    ):
        self._http = http_client
        self._delay = delay or HumanDelay()
        self._headers = headers
        self.enabled = enabled
        self._current_page: Optional[str] = None
        self._current_village: Optional[int] = None
        self._visited_pages: set = set()
    
    async def navigate_to_resource_view(self, village_id: Optional[int] = None) -> None:
        """Navigate to the resource field view (dorf1.php).
        
        Skips if we're already on dorf1 for this village.
        """
        if not self.enabled:
            return
        
        target = self._build_url("/dorf1.php", village_id)
        
        if self._current_page == target:
            logger.debug("Already on dorf1, skipping navigation")
            return
        
        await self._delay.page_load()
        await self._visit(target)
    
    async def navigate_to_village_view(self, village_id: Optional[int] = None) -> None:
        """Navigate to the village center view (dorf2.php).
        
        Skips if we're already on dorf2 for this village.
        """
        if not self.enabled:
            return
        
        target = self._build_url("/dorf2.php", village_id)
        
        if self._current_page == target:
            logger.debug("Already on dorf2, skipping navigation")
            return
        
        await self._delay.page_load()
        await self._visit(target)
    
    async def navigate_to_building(self, slot_id: int, village_id: Optional[int] = None) -> None:
        """Navigate to a specific building page (as a human would).
        
        For resource fields (slot 1-18): dorf1 → build.php?id=X
        For village buildings (slot 19-40): dorf2 → build.php?id=X
        """
        if not self.enabled:
            return
        
        # First visit the appropriate overview page
        if slot_id <= 18:
            await self.navigate_to_resource_view(village_id)
        else:
            await self.navigate_to_village_view(village_id)
        
        # Then "click" on the building
        await self._delay.quick_click()
        build_url = self._build_url(f"/build.php?id={slot_id}", village_id)
        await self._visit(build_url)
    
    async def navigate_to_rally_point(self, village_id: Optional[int] = None) -> None:
        """Navigate to the rally point (for sending troops)."""
        if not self.enabled:
            return
        
        await self.navigate_to_village_view(village_id)
        await self._delay.quick_click()
        
        url = self._build_url("/build.php?gid=16&tt=2", village_id)
        await self._visit(url)
    
    async def idle_browse(self, http_client: "HttpClient" = None, village_id: Optional[int] = None) -> None:
        """Perform an idle page visit during long waits.
        
        Called by SessionManager. Uses provided http_client or falls back to self._http.
        """
        client = http_client or self._http
        if not client or not self.enabled:
            return
        
        old_http = self._http
        self._http = client
        try:
            await self.browse_randomly(village_id)
        finally:
            self._http = old_http
    
    async def browse_randomly(self, village_id: Optional[int] = None) -> None:
        """Visit a random page to create natural browsing noise.
        
        Call this occasionally during long-running operations to
        simulate a human checking different parts of the game.
        """
        if not self.enabled:
            return
        
        import random
        pages = [
            "/dorf1.php",       # Resource overview
            "/dorf2.php",       # Village center
            "/statistiken.php", # Statistics
            "/spieler.php",     # Player profile
        ]
        
        page = random.choice(pages)
        url = self._build_url(page, village_id)
        
        await self._delay.think()
        await self._visit(url)
        await self._delay.page_load()
    
    def set_http_client(self, http_client: "HttpClient") -> None:
        """Set the HTTP client (for deferred initialization)."""
        self._http = http_client
    
    def set_headers(self, headers: BrowserHeaders) -> None:
        """Set the browser headers (for deferred initialization)."""
        self._headers = headers
    
    def reset(self) -> None:
        """Reset navigation state (e.g., after re-login)."""
        self._current_page = None
        self._current_village = None
        self._visited_pages.clear()
    
    def clear_visited(self) -> None:
        """Alias for reset — clears all visited page tracking."""
        self.reset()
    
    def _build_url(self, path: str, village_id: Optional[int] = None) -> str:
        """Build URL with optional village context."""
        if village_id:
            sep = "&" if "?" in path else "?"
            return f"{path}{sep}newdid={village_id}"
        return path
    
    async def _visit(self, url: str) -> None:
        """Actually fetch a page (silently, just for the request pattern)."""
        try:
            logger.debug(f"Navigator: visiting {url}")
            self._headers.update_last_page(url)
            # Use get_html but we don't need the response — it's just for the pattern
            await self._http.get_html(url)
            self._current_page = url.split("?")[0]  # Track base path
        except Exception as e:
            logger.debug(f"Navigator: visit failed (non-critical): {e}")
