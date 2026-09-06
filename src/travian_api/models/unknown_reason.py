"""Named reasons a figure could not be established, and the guard that keeps
one out of arithmetic.

A figure the game did not give us is not a zero. Three audits this week found
the same defect: an absence written as ``0`` (or ``{}``) and then computed with
-- a cranny that hides the whole stock scored as 67% of it, a trapper nobody
looked at scored as ``min(n, 0)`` dead, a failed troop read taken as "no scout
limit". The fix each time was to refuse. This module is the next step: refusing
is right, but a bare refusal does not say WHICH read failed, so a trace cannot
tell an empty page from a page with no attacker block.

So a field that carries no figure carries a ``UnknownReason`` instead. The codes
are negative because every figure they stand in for is a count, a level or a
capacity -- all non-negative -- so ``value < 0`` is an unambiguous "this is not
a quantity", and ``require_known`` turns any attempt to compute with one into a
named error rather than a plausible wrong answer.

Relation to ``raidable_confidence`` (``models/raid_analyzer.py``)
----------------------------------------------------------------
These two vocabularies look similar and are NOT the same thing. Do not merge
them.

``raidable_confidence`` is a CONFIDENCE LADDER over one field that DOES have a
value: ``"scouted"`` and ``"raided"`` are successes, ``"depleted"`` is a
measured zero, and only ``"none"``/``"unreadable"`` are failures. Its question
is "how much is this number worth?".

``UnknownReason`` names only failures, and it stands IN PLACE OF the number.
Its question is "why is there no number?". A field holding an ``UnknownReason``
has no value at all; a field with weak ``raidable_confidence`` has one worth
little. Adding "unreadable" to this enum, or "NO_BUILDING_ROW" to that ladder,
would collapse that distinction and put a reason code where a quantity is
expected.
"""

from __future__ import annotations

from enum import IntEnum


class UnknownReason(IntEnum):
    """Why a figure could not be established. Never a quantity."""

    NEVER_RAIDED = -1
    UNPARSEABLE_PAGE = -2
    EMPTY_RESPONSE = -3
    NO_ATTACKER_BLOCK = -4
    NO_BUILDING_ROW = -5
    STALE_BEYOND_HORIZON = -6


class UnknownFigureError(ValueError):
    """A reason code reached a site that wanted a quantity.

    Raised by :func:`require_known`. The message names the field and the reason
    so the trace says which read failed, rather than leaving a ``-2`` to be
    interpreted.
    """

    def __init__(self, field: str, value: int) -> None:
        self.field = field
        self.value = value
        self.reason = reason_of(value)
        named = self.reason.name if self.reason is not None else "an unrecognised negative"
        super().__init__(
            f"{field} carries no figure: {named} ({value}). It is an unknown-reason "
            "code, not a quantity, and must not be computed with."
        )


def is_unknown(value: int) -> bool:
    """True when *value* stands in for a figure instead of being one.

    Every figure these codes replace -- a wall level, a trap capacity, a combat
    strength -- is non-negative, so any negative is an unknown.
    """
    return value < 0


def reason_of(value: int) -> UnknownReason | None:
    """The :class:`UnknownReason` *value* names, or None if it is a figure."""
    if not is_unknown(value):
        return None
    try:
        return UnknownReason(value)
    except ValueError:
        return None


def reason_name(value: int) -> str | None:
    """The reason's NAME for a log or the wire, or None if *value* is a figure.

    A raw ``-2`` on the wire renders as a defence value; ``"UNPARSEABLE_PAGE"``
    cannot be mistaken for one.
    """
    reason = reason_of(value)
    if reason is not None:
        return reason.name
    return "UNKNOWN" if is_unknown(value) else None


def require_known(value: int, field: str) -> int:
    """Return *value*, or raise :class:`UnknownFigureError` if it is a reason code.

    Call this at every arithmetic site that consumes a figure which may be
    unknown. Callers are expected to check :func:`is_unknown` first and refuse
    with a reason the operator can act on; this guard is the backstop that turns
    a leak into a named error instead of ``min(n, -5)``.
    """
    if is_unknown(value):
        raise UnknownFigureError(field, value)
    return value
