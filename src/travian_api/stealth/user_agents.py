"""Realistic browser User-Agent rotation.

Maintains a pool of current Chrome-on-Windows User-Agent strings
(the only family curl_cffi can faithfully impersonate). Picks one
per session and sticks with it (switching UA mid-session is a
detection signal).
"""

from __future__ import annotations

import random
from typing import Optional

from .persona import Persona, build_persona, _CHROME_WINDOWS_UAS

# Public alias — kept for backward compatibility
_USER_AGENTS = list(_CHROME_WINDOWS_UAS)


def get_random_ua() -> str:
    """Get a random User-Agent string."""
    return random.choice(_USER_AGENTS)


class UserAgentRotator:
    """Manages User-Agent selection per session.

    Picks one UA at init and keeps it for the entire session.
    Call rotate() to pick a new one (e.g., on reconnect/new session).
    """

    def __init__(
        self,
        ua: Optional[str] = None,
        *,
        persona: Persona | None = None,
        server_url: str = "",
    ):
        if persona is not None:
            self._persona = persona
        else:
            self._persona = build_persona(ua=ua, server_url=server_url)

        self._ua = self._persona.user_agent
        self._is_firefox = False  # pool is Chrome-only now
        self._is_edge = False

    @property
    def ua(self) -> str:
        return self._ua

    @property
    def persona(self) -> Persona:
        return self._persona

    @property
    def is_firefox(self) -> bool:
        return self._is_firefox

    @property
    def is_edge(self) -> bool:
        return self._is_edge

    @property
    def is_chrome(self) -> bool:
        return not self._is_firefox and not self._is_edge

    def get_persona(self) -> Persona:
        """Return the current session Persona."""
        return self._persona

    def rotate(self, *, server_url: str = "") -> str:
        """Pick a new UA (and persona) for a new session."""
        self._persona = build_persona(server_url=server_url)
        self._ua = self._persona.user_agent
        self._is_firefox = False
        self._is_edge = False
        return self._ua
