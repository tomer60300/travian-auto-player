"""Stealth module — anti-bot detection countermeasures for Travian CLI.

Provides human-like behavior simulation to avoid bot detection:
- Realistic browser User-Agent rotation
- Browser-accurate HTTP headers with proper Referer chains
- Global request rate limiting
- Random human-like delays between actions
- Page navigation simulation (browse before acting)
- Session lifetime management with breaks
"""

from .user_agents import get_random_ua, UserAgentRotator
from .headers import BrowserHeaders
from .throttler import RequestThrottler
from .human_delay import HumanDelay
from .navigator import PageNavigator

__all__ = [
    "get_random_ua",
    "UserAgentRotator",
    "BrowserHeaders",
    "RequestThrottler",
    "HumanDelay",
    "PageNavigator",
]
