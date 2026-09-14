"""Which rows of a fanned-out route fall outside a profile's hours.

Travian offers no way to confine a trade route to part of the day, and it does
not need to: ``repeat every N hours`` is implemented as 24/N separate rows, each
with its own id, its own departure, and each individually deletable. Measured on
a real account -- a 1-hour route produced 24 rows, and deleting one left 23.

So a windowed profile is enforced by subtraction rather than by a setting. The
route is created, and the rows departing outside the window are removed. What
survives fires only inside the profile's hours, which is what the planner already
sized the cargo for: the window stops being a fiction the beat believed and the
game ignored, and the row footprint falls to the fraction of the day covered.

``departure_at % 86400`` needs no timezone. It is the same minutes-past-midnight
that the create payload's ``hour``/``minute`` and ``dispatch_window`` already
use -- confirmed against the game, which returned 1410 for a route asked to leave
at 23:30.

Pure functions over what the page already reported. No requests and no clock:
deciding to delete something is not a place for a value read from whichever
machine this happens to run on.

**Which rows to remove is decided in `web/routes/distribution.py`, not here.**
This module once carried a `rows_outside_window` that nothing ever called: the
executor answers a strictly harder question -- which live rows correspond to a
planned FIRING, matching cargo and counting duplicates at the same minute -- and
its `_planned_minutes` has already applied the window before that match runs. So
the window rule was never a separate step, and keeping a second, simpler copy of
it only invited the two to drift. What is left here are the two primitives that
answer really are shared.
"""

from __future__ import annotations

SECONDS_PER_DAY = 86_400
# No MINUTES_PER_DAY here. It was declared as `1_440` beside `schedule.py`'s
# `24 * 60` -- two spellings of one quantity, in two modules, with nothing
# tying them together -- and nothing in this one ever read it. `schedule.py`
# owns it, and this module deliberately depends on no clock at all.


def minute_of_day(departure_at: int | None, *, server_utc_offset_minutes: int = 0) -> int | None:
    """Minutes past midnight for a row's departure, on the GAME's clock.

    None is a real answer and not a zero. A row whose departure the page did not
    state has an unknown position in the day, and treating unknown as midnight
    would put it inside a night window and outside a day one -- deleting or
    sparing it for a reason that was never established.

    ``server_utc_offset_minutes`` is what the game server's clock reads ahead of
    UTC. It defaults to zero, which is the reading this function had before it
    took the argument at all, so a caller that has not been taught to pass one
    behaves exactly as it did.
    """
    if departure_at is None:
        return None
    shifted = int(departure_at) + server_utc_offset_minutes * 60
    return (shifted % SECONDS_PER_DAY) // 60


def in_window(minute: int, window: tuple[int, int]) -> bool:
    """Is *minute* inside *window*, which may wrap past midnight?

    Start-inclusive, end-exclusive, matching how the beat already reads a
    window -- so the rows kept here are exactly the firings the plan counted.
    """
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end
