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

from .headers import BrowserHeaders
from .human_delay import ActionType, HumanDelay
from .navigator import PageNavigator
from .noise import NoiseInjector
from .persona import Persona, build_persona, load_persona, save_persona
from .scheduler import ActivityScheduler
from .session_manager import SessionManager
from .throttler import RequestThrottler
from .timing import HumanTiming
from .user_agents import UserAgentRotator, get_random_ua

__all__ = [
    "get_random_ua",
    "UserAgentRotator",
    "BrowserHeaders",
    "Persona",
    "build_persona",
    "load_persona",
    "save_persona",
    "RequestThrottler",
    "HumanDelay",
    "ActionType",
    "PageNavigator",
    "SessionManager",
    "HumanTiming",
    "NoiseInjector",
    "ActivityScheduler",
]
