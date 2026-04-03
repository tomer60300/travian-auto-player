"""Realistic browser User-Agent rotation.

Maintains a pool of current, real-world User-Agent strings from
Chrome, Firefox, and Edge on Windows/Mac. Picks one per session
and sticks with it (switching UA mid-session is a detection signal).
"""

import random
from typing import Optional

# Real User-Agent strings — updated for early 2026 browser versions.
# Mix of Chrome (dominant), Firefox, and Edge on Windows 10/11 and macOS.
# Chrome version ~134, Firefox ~137, Edge ~134 as of March/April 2026.
_USER_AGENTS = [
    # Chrome 132-135 on Windows 10/11 (current stable range early 2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
    
    # Chrome on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    
    # Firefox 135-137 on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:136.0) Gecko/20100101 Firefox/136.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
    
    # Firefox on macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:137.0) Gecko/20100101 Firefox/137.0",
    
    # Edge on Windows (Chromium-based, tracks Chrome version)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 Edg/133.0.0.0",
]


def get_random_ua() -> str:
    """Get a random User-Agent string."""
    return random.choice(_USER_AGENTS)


class UserAgentRotator:
    """Manages User-Agent selection per session.
    
    Picks one UA at init and keeps it for the entire session.
    Call rotate() to pick a new one (e.g., on reconnect/new session).
    """
    
    def __init__(self, ua: Optional[str] = None):
        self._ua = ua or get_random_ua()
        self._is_firefox = "Firefox" in self._ua
        self._is_edge = "Edg/" in self._ua
    
    @property
    def ua(self) -> str:
        return self._ua
    
    @property
    def is_firefox(self) -> bool:
        return self._is_firefox
    
    @property
    def is_edge(self) -> bool:
        return self._is_edge
    
    @property
    def is_chrome(self) -> bool:
        return not self._is_firefox and not self._is_edge
    
    def rotate(self) -> str:
        """Pick a new UA for a new session."""
        self._ua = get_random_ua()
        self._is_firefox = "Firefox" in self._ua
        self._is_edge = "Edg/" in self._ua
        return self._ua
