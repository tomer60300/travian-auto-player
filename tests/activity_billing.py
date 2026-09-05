"""One helper: make a fake client bill the way the real transport bills.

``HttpClient._billed`` charges the rolling activity ceiling once per issued
request, from a ``finally``, so a request that raised is billed exactly like
one that answered. Every fake client in the suite stands in for that transport,
so a fake whose request methods bill nothing lets a service-layer double-bill
pass unnoticed -- which is precisely what happened: the trade-route service kept
its own ``_log_activity`` after billing moved into the client, and the ~17 tests
that pinned "a write is billed" could not see the overlap because their fakes
replaced the request methods outright.

Wrap a fake's request methods with :func:`billing` and the fake bills like the
transport. Inject failures as data the fake serves, never by replacing a wrapped
method, or the bill goes missing again.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_F = TypeVar("_F", bound=Callable[..., Awaitable[Any]])


def billing(ledger: list[float]) -> Callable[[_F], _F]:
    """Charge one entry in *ledger* per call, on every exit path."""

    def decorate(method: _F) -> _F:
        @functools.wraps(method)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.monotonic()
            try:
                return await method(*args, **kwargs)
            finally:
                ledger.append(time.monotonic() - started)

        return wrapper  # type: ignore[return-value]

    return decorate
