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

import time
from collections.abc import Awaitable, Callable

from travian_api.operation_manager import OperationContext
from travian_api.stealth.timing import HumanTiming

# Upper bound on a single inter-cycle wait, as a multiple of the configured
# interval. HumanTiming.delay's own clamp is 15x — on an hour-long interval a
# 15h stall that reads as a hang. 4x keeps the periodogram-smearing heavy tail
# and a realistic ~0.84x average throughput while bounding any single wait to
# something an operator won't mistake for a stuck loop (4h on an hourly sweep,
# minutes on a typical farm interval).
_MAX_WAIT_INTERVAL_MULTIPLE = 4.0


def recurring_wait(ctx: OperationContext, interval: float) -> float:
    """Inter-cycle sleep for a recurring loop: heavy-tailed, tempo-scaled.

    ``HumanTiming.delay`` at variance_factor=1.0 keeps the expected value ≈
    ``interval`` (~0.97x, measured) so average throughput is preserved while the
    bursty shape smears the fixed-period peak; ``tempo_scale`` couples the
    super-cadence to the shared session rhythm. The result is capped at
    ``_MAX_WAIT_INTERVAL_MULTIPLE`` × interval so a large interval (e.g. an
    hourly oasis sweep) can't draw a multi-hour stall. Deterministic when
    stealth is off so dev/test runs stay predictable.
    """
    hc = ctx.session.http_client
    if not getattr(hc, "stealth_enabled", False):
        return float(interval)
    drawn = hc.tempo_scale(HumanTiming.delay(interval, variance_factor=1.0))
    return max(1.0, min(drawn, interval * _MAX_WAIT_INTERVAL_MULTIPLE))


async def night_rest_pause(
    ctx: OperationContext,
    announce: Callable[[float], Awaitable[None]] | None = None,
    deadline: float | None = None,
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

    ``deadline`` (epoch seconds) caps the sleep so a bounded run never lingers
    asleep past when it should have ended: the pause is trimmed to the deadline
    and the caller's end-of-run check then stops it, instead of sleeping hours
    past its finish.
    """
    rest = ctx.session.http_client.rest_pause_seconds()
    if rest <= 0:
        return False
    if deadline is not None:
        rest = min(rest, deadline - time.time())
        if rest <= 0:
            return False  # deadline already reached; let the loop end
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
