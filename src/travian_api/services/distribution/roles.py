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

from enum import StrEnum


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
