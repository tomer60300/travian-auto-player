"""Page navigation simulation.

Real players don't jump directly to build.php?id=5&action=build.
They navigate: overview → village view → click building → read → click upgrade.

This module simulates realistic navigation chains so the server sees
proper page-load sequences with correct Referer headers.
"""

import asyncio
import logging
import random
from typing import TYPE_CHECKING, List, Optional, Tuple

from .human_delay import ActionType, HumanDelay

if TYPE_CHECKING:
    from ..clients.http_client import HttpClient
    from .headers import BrowserHeaders

logger = logging.getLogger(__name__)


# Navigation chains: what pages a human visits before each action
_NAV_CHAINS = {
    # Before upgrading a resource field (slot 1-18)
    "upgrade_resource": [
        "/dorf1.php",           # view resource overview
    ],
    # Before upgrading a building (slot 19-40)
    "upgrade_building": [
        "/dorf2.php",           # view village overview
    ],
    # Before sending troops
    "send_troops": [
        "/build.php?gid=16&tt=1",  # rally point overview
    ],
    # Before viewing reports
    "view_reports": [
        "/berichte.php",        # reports page
    ],
    # Before checking map
    "view_map": [
        "/karte.php",           # map view
    ],
    # Generic — just load the village overview
    "generic": [
        "/dorf1.php",
    ],
}

# Idle browsing pages — visited randomly to create noise
_IDLE_PAGES = [
    "/dorf1.php",
    "/dorf2.php",
    "/statistiken.php",       # statistics
    "/spieler.php",           # player profile
    "/allianz.php",           # alliance page
    "/berichte.php",          # reports
    "/nachrichten.php",       # messages
]


class PageNavigator:
    """Simulates realistic page navigation before actions.
    
    Before performing an action (upgrade, send troops, etc.), this
    navigator loads the prerequisite pages a human would visit,
    with realistic delays between each.
    
    Also provides idle browsing to simulate an active session.
    """
    
    def __init__(
        self,
        human_delay: HumanDelay,
        enabled: bool = True,
    ):
        self._delay = human_delay
        self.enabled = enabled
        self._pages_visited: int = 0
        self._http_client = None  # set by http_client after init
        self._browser_headers = None  # set by http_client after init
    
    def bind(self, http_client: "HttpClient", browser_headers: "BrowserHeaders") -> None:
        """Bind to HTTP client and browser headers (called during setup)."""
        self._http_client = http_client
        self._browser_headers = browser_headers
    
    async def navigate_before(self, action: str, village_id: Optional[int] = None) -> None:
        """Load prerequisite pages before an action.
        
        Args:
            action: Action type key (e.g., "upgrade_resource", "send_troops")
            village_id: Village ID to include in URLs
        """
        if not self.enabled or not self._http_client:
            return
        
        chain = _NAV_CHAINS.get(action, _NAV_CHAINS["generic"])
        
        for page_path in chain:
            # Add village context if needed
            if village_id:
                sep = "&" if "?" in page_path else "?"
                page_path = f"{page_path}{sep}newdid={village_id}"
            
            try:
                await self._delay.wait(ActionType.NAVIGATION, f"navigating to {page_path}")
                # Use the raw client to load the page (updates cookies/session)
                await self._http_client.get_html(page_path, skip_reauth=True)
                
                # Update referer tracking
                if self._browser_headers:
                    self._browser_headers.update_last_page(page_path)
                
                self._pages_visited += 1
                
            except Exception as e:
                logger.debug(f"Navigation to {page_path} failed (non-critical): {e}")
    
    async def idle_browse(self, pages: int = 1) -> None:
        """Visit random pages to simulate idle browsing.
        
        Call this periodically during long waits to keep the session
        looking active and natural.
        
        Args:
            pages: Number of random pages to visit (1-3 recommended)
        """
        if not self.enabled or not self._http_client:
            return
        
        for _ in range(min(pages, 3)):
            page = random.choice(_IDLE_PAGES)
            
            try:
                await self._delay.wait(ActionType.PAGE_READ, f"idle browsing {page}")
                await self._http_client.get_html(page, skip_reauth=True)
                
                if self._browser_headers:
                    self._browser_headers.update_last_page(page)
                
                self._pages_visited += 1
                
            except Exception as e:
                logger.debug(f"Idle browse to {page} failed (non-critical): {e}")
    
    @property
    def pages_visited(self) -> int:
        """Total pages visited this session (navigation + idle)."""
        return self._pages_visited
