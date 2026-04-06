"""Stealth module — anti-bot detection countermeasures for Travian CLI.

Provides human-like behavior simulation to avoid bot detection:
- Realistic browser User-Agent rotation
- Browser-accurate HTTP headers with proper Referer chains
- Global request rate limiting
- Random human-like delays between actions (triangular + heavy-tailed)
- Page navigation simulation (browse before acting)
- Session lifetime management with breaks
- Behavioral noise injection between automation actions
- Activity scheduling with daily hour limits
"""

from .user_agents import get_random_ua, UserAgentRotator
from .headers import BrowserHeaders
from .throttler import RequestThrottler
from .human_delay import HumanDelay, ActionType
from .navigator import PageNavigator
from .session_manager import SessionManager
from .timing import HumanTiming
from .noise import NoiseInjector
from .scheduler import ActivityScheduler

__all__ = [
    "get_random_ua",
    "UserAgentRotator",
    "BrowserHeaders",
    "RequestThrottler",
    "HumanDelay",
    "ActionType",
    "PageNavigator",
    "SessionManager",
    "HumanTiming",
    "NoiseInjector",
    "ActivityScheduler",
]
