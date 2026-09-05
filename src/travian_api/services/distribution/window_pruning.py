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

Pure functions over rows the page already reported. No requests and no clock:
deciding to delete something is not a place for a value read from whichever
machine this happens to run on.
"""

from __future__ import annotations

from typing import Protocol, Sequence

SECONDS_PER_DAY = 86_400
# No MINUTES_PER_DAY here. It was declared as `1_440` beside `schedule.py`'s
# `24 * 60` -- two spellings of one quantity, in two modules, with nothing
# tying them together -- and nothing in this one ever read it. `schedule.py`
# owns it, and this module deliberately depends on no clock at all.


class _Row(Protocol):
    """The part of an ``ExistingRoute`` this decision reads."""

    route_id: int
    departure_at: int | None


def minute_of_day(departure_at: int | None) -> int | None:
    """Minutes past midnight for a row's departure, or None if it has none.

    None is a real answer and not a zero. A row whose departure the page did not
    state has an unknown position in the day, and treating unknown as midnight
    would put it inside a night window and outside a day one -- deleting or
    sparing it for a reason that was never established.
    """
    if departure_at is None:
        return None
    return (int(departure_at) % SECONDS_PER_DAY) // 60


def in_window(minute: int, window: tuple[int, int]) -> bool:
    """Is *minute* inside *window*, which may wrap past midnight?

    Start-inclusive, end-exclusive, matching how the beat already reads a
    window -- so the rows kept here are exactly the firings the plan counted.
    """
    start, end = window
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


def rows_outside_window(rows: Sequence[_Row], window: tuple[int, int] | None) -> list[_Row]:
    """The rows to delete so a route only fires inside *window*.

    Returns them rather than deleting them: the caller owns the write, the
    verification and the pacing, and a pure answer can be asserted against.

    A row with no stated departure is never proposed for deletion. Erring toward
    keeping leaves a route shipping outside its hours, which the plan already
    reports as a finding; erring the other way destroys a row on a guess.

    Raises:
        ValueError: if every row would be removed. A window that matches nothing
            would delete the entire route the run just created, which is a bug in
            the caller -- most likely a window and a dispatch time that disagree --
            not an instruction to carry out.
    """
    if window is None:
        return []
    doomed = [
        row
        for row in rows
        if (minute := minute_of_day(row.departure_at)) is not None and not in_window(minute, window)
    ]
    if rows and len(doomed) == len(rows):
        raise ValueError(
            f"pruning to window {window} would remove every row of this route "
            f"({len(rows)} of {len(rows)}); refusing, because that deletes the route "
            f"the run just created rather than confining it"
        )
    return doomed
