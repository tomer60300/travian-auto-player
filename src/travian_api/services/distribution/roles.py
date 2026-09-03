"""What a village is FOR, and the permissions that follow from it.

The profile is written in this vocabulary and the planner was not: section 5.9
("role villages may not relay"), section 6.7 ("every role village at 60%"),
sections 9.1-9.2 ("01 and 03 are negative by design"), section 2.1 (one profile
applied to four DEF villages). Every one of those is a rule about a *kind* of
village, and with no way to say which kind a village is, each had to be
re-derived from whatever the snapshot happened to carry -- which is how the
relay guard came to infer "safe to forward through" from the sign of a crop
rate. That inference is right when nothing has been declared and wrong the
moment something has: 01 reads -5,880/h *by design*, so its sign says nothing
about whether the operator wants crop passing through it.

Roles are therefore backend state, not a label the page paints. Two of the
decisions they carry are made in here -- who may relay
(:func:`default_may_relay`) and how loud a designed crop deficit is -- and
neither is reachable from the frontend.

Assignment is exhaustive: every village has exactly one role, ``feeder`` being
what the profile calls "all other villages". The foreign tribute is deliberately
NOT a role: it is a :class:`~travian_api.web.routes.distribution.ForeignTarget`,
because it has no production, no merchants and no stores, and giving it a role
would invite treating it as a village that has them.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from .allocation import Resource, village_label
from .findings import Category, Finding


class Role(StrEnum):
    """The five kinds of village in profile section 1.

    A ``StrEnum`` so a role survives JSON, a setup file and a log line as the
    word the operator uses, and so an unknown name is a validation error at the
    edge rather than a silently mis-typed key deeper in.
    """

    CAPITAL = "capital"
    """Capital, storage and NPC hub. Section 1: village 02."""

    TROOPS_OFF = "troops_off"
    """Troops-only offensive village, permanently crop-negative by design (03)."""

    FULL_OFF = "full_off"
    """The Hammer: full offence, permanently crop-negative by design (01)."""

    DEF = "def"
    """Defensive village. Section 1 names four (11, 13, 17, 19), which is the
    whole reason templates exist: one profile, four villages."""

    FEEDER = "feeder"
    """Everything else. A village whose job is to grow resources and move them
    on, so it holds nothing and can be asked to carry someone else's cargo."""


def default_may_relay(role: Role) -> bool:
    """May a village of this role be made to forward someone else's cargo?

    Only a feeder, and the rule is the profile's rather than an optimisation:
    section 5.9 says role villages may not relay. The reason is not solvency --
    a DEF village with 1,200/h of spare crop is perfectly able to fund a
    pass-through leg -- it is that a role village has a job which a leg in
    transit interferes with, and its stores are sized for that job.

    The capital is ``False``, which resolves profile inconsistency #9 rather
    than papering over it. Section 5 makes 02 the hub every feeder ships to,
    and also says the onward distribution runs through a tier of relays drawn
    from 02's own neighbour set. Those two only fit together if the capital
    *hands off*: it receives and it holds, and something else carries the
    onward leg. Read the other way the capital would win every relay search it
    entered -- it is the most central village on the account by construction --
    and the tier section 5 asks for would never be built.

    A default, not a law: :class:`RoleTemplate.may_relay` overrides it per role,
    for the account whose defensive village sits on the only road to a corner of
    the map. What the override may not do is go unstated, which is why there is
    no way to reach this decision from the page alone.
    """
    return role is Role.FEEDER


def keeps_a_morning_floor(role: Role) -> bool:
    """Must a village of this role be at the morning fill floor at 07:00?

    Section 6: "every role village (DEF + OFF; capital excluded) must be at 60%
    capacity on both warehouse and granary." So this is a NARROWER set than the
    "role village" of :func:`default_may_relay`, which counts the capital in --
    and the difference is deliberate rather than an inconsistency to tidy away.
    The relay rule is about a village having a job that a leg in transit
    interferes with, which the capital does; the morning floor is about waking
    up able to train and build, which is what DEF and the two OFF roles are
    FOR.

    The capital is excluded because it is the storage and NPC hub: its stores
    are drawn down on purpose, and a floor under them would report the hub doing
    its job as a defect. A feeder is excluded because it holds nothing by design
    -- it grows resources and moves them on, so 60% of its warehouse is cargo
    sitting where it is not wanted.
    """
    return role in (Role.DEF, Role.TROOPS_OFF, Role.FULL_OFF)


CROP_DRIFT_THRESHOLD = 0.20
"""Section 9, verbatim:

    Consumption profiles are flat constants. Drift is expected between manual
    updates. Flag any village whose actual net crop deviates >20% from its
    assumed profile.

A named constant because the figure is the operator's tolerance for his own
bookkeeping and not an arithmetic detail -- and because "20%" appears in the
spec, so a literal buried in a comparison could drift from it silently, which
is exactly the failure the check itself exists to catch.

Strictly greater than: the spec says ">20%", so a village exactly 20% off is
inside the tolerance and stays silent.
"""


def crop_drift_findings(
    assumed: Mapping[int, float],
    actual: Mapping[int, float | None],
    roles: Mapping[int, Role],
    names: Mapping[int, str] | None = None,
) -> list[Finding]:
    """Villages whose net crop has moved away from the figure their role assumes.

    A staleness detector on hand-maintained constants, NOT a health check on the
    account. Section 9 says in the same breath that 01 and 03 are permanently
    crop-negative by design and that starvation there is not an error state, so
    a village sitting exactly where its profile says it sits must stay silent
    however deep the deficit is. What this reports is that the profile and the
    game no longer agree, which matters because every crop cargo the plan sizes
    was sized against the profile.

    ``assumed`` is the role template's ``assumed_crop_per_hour`` per village --
    the operator's recorded reading. ``actual`` is the snapshot's
    ``crop_per_hour``, which is ALREADY net of troop upkeep (the standing
    ruling: a declared crop consumption would subtract the same troops twice),
    so the two figures are the same quantity and directly comparable. A village
    absent from ``assumed`` has no assumption and is not checked: no figure is
    not a figure of zero, and reading it as zero would flag every village on
    every account that has never typed one. A village whose ``actual`` is
    ``None`` had no crop balance to read, which the plan already reports as
    UNREADABLE_RATE -- a missing reading is not a stale figure.

    **The arithmetic, which is the whole of the difficulty.** Deviation is

        |actual - assumed| / |assumed|

    -- the magnitude of the DIFFERENCE over the magnitude of the assumption.
    Two tempting alternatives are both wrong on this account:

    * *Comparing magnitudes* (``||actual| - |assumed||``) is blind to a sign
      flip. An assumed -5,880/h against an actual +5,880/h is 0% drift by that
      reading, when it is in fact the largest thing that can happen to the
      figure: the village has stopped eating its own fields, so the strict crop
      delivery the whole plan is built around is no longer needed at all. A sign
      flip is therefore measured here like any other gap (200% in that example)
      AND named in the message, because 200% alone understates it.
    * *Signed relative deviation* (``(actual - assumed) / assumed``) inverts
      with a negative denominator, so a village that got worse (-5,880 to
      -9,000) reports +53% and one that got better reports -49% -- the opposite
      of every other rate on the sheet, where more crop reads as more. Dividing
      by ``|assumed|`` keeps the sign of the numerator meaning what it says, and
      the message states the direction in words rather than leaving it to be
      inferred from a sign.

    An assumed figure of **zero** is never divided by. It is still a claim --
    "this village breaks even" -- so the comparison is written as a
    multiplication, ``|gap| <= threshold x |assumed|``, which needs no special
    case in the predicate: against an assumption of zero any nonzero reading is
    drift, and only the wording changes, since there is no percentage of zero to
    print.

    ``crop_negative_by_design`` deliberately does NOT silence this. That flag
    downgrades the granary countdown from a critical to a note, which is a
    statement about the STORE; the profile behind the deficit can still go
    stale, and on the two villages carrying that flag it is the likeliest figure
    on the account to have moved.
    """
    out: list[Finding] = []
    for village_id in sorted(assumed):
        reads = actual.get(village_id)
        if reads is None:
            continue
        claim = assumed[village_id]
        gap = reads - claim
        if abs(gap) <= CROP_DRIFT_THRESHOLD * abs(claim):
            continue
        label = village_label(village_id, names)
        role = roles.get(village_id)
        whose = f"its {role.value} profile" if role is not None else "its role profile"
        direction = "lower" if gap < 0 else "higher"
        if claim:
            far = f"{abs(gap) / abs(claim):.0%} off"
            measured = (
                f"{far}, {abs(gap):,.0f}/h {direction} than the profile" if abs(gap) >= 1 else far
            )
        else:
            # No percentage exists against an assumed break-even, so the gap IS
            # the measurement. Said in words rather than printed as an infinity.
            far = "no percentage against an assumed 0/h"
            measured = f"the whole {abs(gap):,.0f}/h is the drift, and 0/h has no percentage"
        message = (
            f"{label} nets {reads:,.0f}/h of crop but {whose} assumes {claim:,.0f}/h -- {measured}."
        )
        if claim and reads and (claim > 0) != (reads > 0):
            eats, grows = "eats more crop than it grows", "grows more crop than it eats"
            was, now = (eats, grows) if claim < 0 else (grows, eats)
            message += (
                f" The sign has flipped: the profile assumes this village {was}, and it now {now}."
            )
        message += (
            " Section 9's profiles are flat constants kept by hand, so this is a figure to "
            "re-read rather than a fault in the account -- but every crop cargo sized from "
            "it is sized from the old number."
        )
        out.append(
            Finding(
                category=Category.CROP_PROFILE_DRIFT,
                message=message,
                detail=f"{label} — assumes {claim:,.0f}/h, reads {reads:,.0f}/h ({far})",
                village=label,
                resource=Resource.CROP,
            )
        )
    return out
