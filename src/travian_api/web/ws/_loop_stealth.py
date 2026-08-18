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

# Upper bound on the VARIABLE part of an inter-cycle wait, as a multiple of the
# configured interval. HumanTiming.delay's own clamp is 15x — on an hour-long
# interval a 15h stall that reads as a hang. 4x keeps the periodogram-smearing
# heavy tail while bounding any single wait to something an operator won't
# mistake for a stuck loop (the wait is `floor + <=4x*interval`).
_MAX_WAIT_INTERVAL_MULTIPLE = 4.0


def recurring_wait(ctx: OperationContext, interval: float, *, floor: float = 0.0) -> float:
    """Inter-cycle sleep for a recurring loop: heavy-tailed, tempo-scaled.

    ``floor`` is the loop's ABSOLUTE minimum wait (the farm's 60s stealth floor).
    The heavy-tailed variable part is drawn with ``HumanTiming.delay`` and ADDED
    to the floor rather than clamped against it. This fixes two ways the previous
    sampler broke the floor's intent:

    * ``HumanTiming.delay``'s own lower bound is ``0.1 * mean`` — a 6s wait at a
      60s interval — so ``delay(interval)`` alone routinely dipped below the
      60s floor. Adding the variable part to the floor makes every wait strictly
      greater than the floor, with continuous density just above it (no
      detectable point mass that a hard ``max(floor, …)`` clamp would create).
    * the ``_MAX_WAIT_INTERVAL_MULTIPLE`` tail cut pulled ``delay(interval)``'s
      mean to ~0.84x, i.e. ~19% MORE requests than configured. ``floor + …``
      lifts the mean back to at least the configured interval across the farm's
      operating range, so cadence can only run slower than asked, never faster.

    ``tempo_scale`` couples the super-cadence to the shared session rhythm.
    Deterministic when stealth is off so dev/test runs stay predictable.
    """
    hc = ctx.session.http_client
    if not getattr(hc, "stealth_enabled", False):
        return float(interval)
    interval = float(interval)
    floor = max(0.0, float(floor))
    drawn = hc.tempo_scale(HumanTiming.delay(interval, variance_factor=1.0))
    return floor + min(drawn, interval * _MAX_WAIT_INTERVAL_MULTIPLE)


async def interruptible_sleep(ctx: OperationContext, seconds: float, chunk: float = 2.0) -> bool:
    """Sleep, returning True on stop. Chunked so BOTH stop signals are honored.

    ``ctx.wait_or_stop`` watches only the explicit stop event; the timestamp-
    based captcha-stop needs a separate ``should_stop()`` poll. Breaking the
    wait into small chunks polls both often, so a captcha resolved mid-sleep
    (or an explicit stop) is picked up within ``chunk`` seconds rather than
    after a multi-hour wait. Shared by the farm and oasis loops and by
    night_rest_pause so all three sleep the same way.
    """
    remaining = seconds
    while remaining > 0:
        step = min(chunk, remaining)
        if await ctx.wait_or_stop(step):
            return True
        if ctx.should_stop():  # captcha-stop
            return True
        remaining -= step
    return False


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
    # Re-clamp after the (possibly slow, awaited) announce so the sleep still
    # ends by the deadline — keeps deadline and an I/O-doing announce composable
    # even though no current caller passes both.
    if deadline is not None:
        rest = min(rest, deadline - time.time())
        if rest <= 0:
            return False
    return await interruptible_sleep(ctx, rest, chunk=15.0)
