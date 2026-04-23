"""Per-user captcha/bot-detection gate.

When Travian signals bot detection (recaptcha, captcha form, ban page),
the guard blocks ALL outbound HTTP requests for that user until the human
resolves the captcha manually and calls ``resolve()``.

The blocking is implemented via ``asyncio.Event``:
    - **set**   → requests flow normally
    - **clear** → every coroutine calling ``wait_if_blocked()`` suspends
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class CaptchaGuard:
    """Async gate that freezes all HTTP traffic when bot detection fires."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._event.set()  # starts open (allowed)
        self._active: bool = False
        self._triggered_at: float | None = None
        self._trigger_pattern: str | None = None
        self._trigger_url: str = ""
        self._trigger_status_code: int = 0
        self._trigger_snippet: str = ""
        self._on_trigger: Optional[Callable] = None  # async callback

    # ── Public API ──────────────────────────────────────────────────

    @property
    def is_blocked(self) -> bool:
        return self._active

    @property
    def status(self) -> dict:
        return {
            "active": self._active,
            "triggered_at": self._triggered_at,
            "pattern": self._trigger_pattern,
            "url": self._trigger_url,
            "status_code": self._trigger_status_code,
            "response_snippet": self._trigger_snippet,
        }

    def set_trigger_callback(self, callback: Callable) -> None:
        """Wire an async callback invoked when captcha is detected.

        The callback receives ``(pattern: str)`` and should broadcast
        the alert to the user's WebSocket connections.
        """
        self._on_trigger = callback

    async def trigger(
        self,
        pattern: str,
        *,
        url: str = "",
        status_code: int = 0,
        response_snippet: str = "",
    ) -> None:
        """Activate the gate — block all subsequent requests.

        Idempotent: calling trigger() while already blocked just updates
        the pattern and timestamp.
        """
        self._active = True
        self._triggered_at = time.time()
        self._trigger_pattern = pattern
        self._trigger_url = url
        self._trigger_status_code = status_code
        self._trigger_snippet = response_snippet
        self._event.clear()

        logger.critical(
            "CAPTCHA GUARD ACTIVATED — pattern=%r | url=%s | status=%d — "
            "all requests blocked until resolved",
            pattern,
            url,
            status_code,
        )

        if self._on_trigger is not None:
            try:
                await self._on_trigger(
                    pattern,
                    url=url,
                    status_code=status_code,
                    response_snippet=response_snippet,
                )
            except Exception:
                logger.exception("Error in captcha trigger callback")

    async def wait_if_blocked(self) -> None:
        """Block the caller if the guard is active, resume when resolved.

        When the guard is open this returns immediately (fast path).
        """
        if self._event.is_set():
            return
        logger.info("Request blocked by captcha guard — waiting for resolution…")
        await self._event.wait()

    def resolve(self) -> None:
        """Lift the block — all waiting coroutines resume."""
        if not self._active:
            return
        self._active = False
        self._event.set()
        logger.info(
            "CAPTCHA GUARD RESOLVED — requests unblocked (was triggered by %r)",
            self._trigger_pattern,
        )
        self._trigger_pattern = None
        self._triggered_at = None
        self._trigger_url = ""
        self._trigger_status_code = 0
        self._trigger_snippet = ""
