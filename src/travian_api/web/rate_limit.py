"""Simple in-memory rate limiter for API routes.

Prevents rapid-fire requests that could trigger Travian's anti-bot detection.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

from fastapi import HTTPException, Request, status


class RateLimiter:
    """Per-user sliding-window rate limiter.

    Usage as a FastAPI dependency::

        _limiter = RateLimiter(max_calls=5, window_seconds=10)

        @router.post("/upgrade")
        async def upgrade(request: Request, _=Depends(_limiter)):
            ...
    """

    def __init__(self, max_calls: int = 5, window_seconds: int = 10) -> None:
        self.max_calls = max_calls
        self.window = window_seconds
        self._calls: dict[str, list[float]] = defaultdict(list)

    def _key(self, request: Request) -> str:
        """Extract a rate-limit key from the request (user_id from auth, or IP)."""
        # Try to use user_id from the JWT payload attached by dependencies
        user_id = getattr(request.state, "user_id", None)
        if user_id:
            return f"user:{user_id}"
        # Fall back to client IP
        client = request.client
        return f"ip:{client.host}" if client else "ip:unknown"

    async def __call__(self, request: Request) -> None:
        key = self._key(request)
        now = time.monotonic()
        # Prune old entries
        window_start = now - self.window
        self._calls[key] = [t for t in self._calls[key] if t > window_start]
        if len(self._calls[key]) >= self.max_calls:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Max {self.max_calls} requests per {self.window}s.",
            )
        self._calls[key].append(now)


# Pre-configured limiters for different route groups
action_limiter = RateLimiter(max_calls=5, window_seconds=10)
