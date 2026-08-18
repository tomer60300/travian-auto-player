"""Shared stealth helpers for recurring WebSocket operation loops.

Both the farm-cycle loop and the oasis-raider loop are long-running metronomes.
Two tells they share, factored here so there is one implementation:

* a fixed inter-cycle interval is a razor-sharp periodogram peak — replaced by
  a heavy-tailed, tempo-scaled wait whose expected value is still the interval;
* running straight through the night is the strongest machine-vs-human signal —
  so a loop goes quiet during the account's rest window and resumes in the
  morning, gracefully (an info message), never the fatal budget path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from travian_api.operation_manager import OperationContext
from travian_api.stealth.timing import HumanTiming


def recurring_wait(ctx: OperationContext, interval: float) -> float:
    """Inter-cycle sleep for a recurring loop: heavy-tailed, tempo-scaled.

    ``HumanTiming.delay`` at variance_factor=1.0 keeps the expected value ≈
    ``interval`` (~0.97x, measured) so average throughput is preserved while the
    bursty shape smears the fixed-period peak; ``tempo_scale`` couples the
    super-cadence to the shared session rhythm. Deterministic when stealth is
    off so dev/test runs stay predictable.
    """
    hc = ctx.session.http_client
    if not getattr(hc, "stealth_enabled", False):
        return float(interval)
    return max(1.0, hc.tempo_scale(HumanTiming.delay(interval, variance_factor=1.0)))


async def night_rest_pause(
    ctx: OperationContext,
    announce: Callable[[float], Awaitable[None]] | None = None,
) -> bool:
    """Pause until morning if the account is in its night-rest window.

    Returns True only if a stop signal arrived during the pause (so the caller
    breaks its loop); False otherwise (rested-and-resumed, or not the rest
    window). A graceful pause, NOT the fatal budget-exhausted path. WS liveness
    across the long sleep is covered by the operation manager's global
    keepalive. Polls the stop event in chunks so a captcha-stop is honored.

    ``announce`` lets each loop surface the pause in ITS own frontend's message
    vocabulary (the farm and oasis UIs handle different frame types) and mirror
    it to the log stream; it receives the pause length in hours. Without it a
    plain ``info`` frame is pushed — a loop whose UI has no ``info`` case must
    pass its own announce or the multi-hour pause is invisible.
    """
    rest = ctx.session.http_client.rest_pause_seconds()
    if rest <= 0:
        return False
    hours = rest / 3600.0
    if announce is not None:
        await announce(hours)
    else:
        ctx.push(
            {
                "type": "info",
                "message": f"Night rest — pausing ~{hours:.1f}h, resuming in the morning.",
            }
        )
    remaining = rest
    chunk = 15.0
    while remaining > 0:
        step = min(chunk, remaining)
        if await ctx.wait_or_stop(step):
            return True
        if ctx.should_stop():  # captcha-stop
            return True
        remaining -= step
    return False
