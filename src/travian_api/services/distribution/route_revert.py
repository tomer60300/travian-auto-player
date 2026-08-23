"""Work out how to put a village's trade routes back the way they were.

The problem this solves: ``POST /api/v1/trade-routes`` returns an empty body, so
the game never tells us the id of a route it just created. A run therefore cannot
say "I made route 627318" -- it can only say "I asked for a route to village N".
The only way to identify what a run actually added is to diff a fresh read of the
marketplace against exactly what was there before it started, which is why the
execution trace records the full pre-write inventory per origin.

The asymmetry that shapes everything here: **disabling is something this app can
do, and deleting is not.** The UI deletes a route (select rows, "Edit selected",
the trash icon) but that request has never been captured, so there is no verified
call to make. A revert therefore comes in two halves:

* what the app can do itself -- disable what it created, restore the enabled or
  disabled state of everything it touched;
* what only a human can do -- delete the created rows, in the UI.

Reporting those separately is the point. A revert that claims to have undone a
run while leaving live routes behind would be far worse than one that says
plainly which two rows a person still has to remove.

Everything here is a pure function over two inventories. No requests, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class RouteState:
    """One route row as an inventory records it."""

    route_id: int
    dest: int
    active: bool


@dataclass
class RevertPlan:
    """What it would take to undo one run against one origin village.

    ``created`` and ``vanished`` are deliberately separate from the state
    changes: a route that appeared is a different kind of problem from a route
    whose enabled flag moved, and only the first needs a human.
    """

    origin: int
    # Rows that exist now and did not before: what the run added.
    created: list[RouteState] = field(default_factory=list)
    # Rows that existed before and are gone now. The app never deletes, so this
    # means something outside this run removed them -- worth surfacing rather
    # than quietly ignoring, because it makes the rest of the diff unreliable.
    vanished: list[RouteState] = field(default_factory=list)
    # Pre-existing rows whose enabled flag the run moved: (route_id, was_active).
    to_restore: list[tuple[int, bool]] = field(default_factory=list)

    @property
    def disable_ids(self) -> list[int]:
        """Created rows that are live now and must be switched off first.

        Done before anything else and separately from deletion, because it is
        the part that actually stops resources moving. A created route left
        enabled while waiting for someone to delete it keeps shipping.
        """
        return sorted(r.route_id for r in self.created if r.active)

    @property
    def manual_delete_ids(self) -> list[int]:
        """Created rows a human has to delete in the UI. See the module docstring."""
        return sorted(r.route_id for r in self.created)

    @property
    def is_clean(self) -> bool:
        """Nothing to undo: the run left this village exactly as it found it."""
        return not (self.created or self.to_restore or self.vanished)


def _as_states(rows: Iterable[Mapping[str, Any]]) -> dict[int, RouteState]:
    """Inventory rows -> {route_id: RouteState}, skipping anything unusable.

    A row without a usable id cannot be reverted and must not be silently
    counted as revertible, so it is dropped here rather than half-handled later.
    """
    states: dict[int, RouteState] = {}
    for row in rows:
        try:
            route_id = int(row["route_id"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            dest = int(row["dest"])
        except (KeyError, TypeError, ValueError):
            dest = 0
        states[route_id] = RouteState(
            route_id=route_id, dest=dest, active=bool(row.get("active", True))
        )
    return states


def plan_revert(
    origin: int,
    before: Iterable[Mapping[str, Any]],
    after: Iterable[Mapping[str, Any]],
) -> RevertPlan:
    """How to return *origin* to its ``before`` state, given what it looks like now.

    ``before`` is the ``inventory`` an execution trace recorded for this origin;
    ``after`` is a fresh read. Route ids are the join: they are assigned by the
    game and stable, which is what makes this diff trustworthy where matching on
    cargo or departure time would not be (two rows of a fanned-out route can be
    identical in everything but id and time).
    """
    old = _as_states(before)
    new = _as_states(after)

    plan = RevertPlan(origin=origin)
    for route_id, state in sorted(new.items()):
        if route_id not in old:
            plan.created.append(state)
        elif old[route_id].active != state.active:
            plan.to_restore.append((route_id, old[route_id].active))
    for route_id, state in sorted(old.items()):
        if route_id not in new:
            plan.vanished.append(state)
    return plan


def describe(plan: RevertPlan) -> list[str]:
    """The plan as lines for an operator, ordered the way it must be carried out.

    Written to be read by someone who is about to click things in a game and
    needs to know what is still live while they do it.
    """
    if plan.is_clean:
        return [f"village {plan.origin}: unchanged, nothing to revert"]

    lines: list[str] = []
    if plan.disable_ids:
        lines.append(
            f"village {plan.origin}: FIRST disable {len(plan.disable_ids)} created "
            f"route(s) still running: {plan.disable_ids} "
            f"(the app can do this; until then they keep shipping)"
        )
    inert = [r.route_id for r in plan.created if not r.active]
    if inert:
        lines.append(
            f"village {plan.origin}: {len(inert)} created route(s) already disabled: {inert}"
        )
    if plan.manual_delete_ids:
        lines.append(
            f"village {plan.origin}: then DELETE {len(plan.manual_delete_ids)} route(s) "
            f"by hand: {plan.manual_delete_ids} — select the row(s), press "
            f"'Edit selected', then the trash icon. The app has no verified "
            f"delete request, so it will not pretend to do this."
        )
    for route_id, was_active in plan.to_restore:
        lines.append(
            f"village {plan.origin}: restore route {route_id} to "
            f"{'enabled' if was_active else 'disabled'} (the run changed it)"
        )
    if plan.vanished:
        lines.append(
            f"village {plan.origin}: WARNING — {len(plan.vanished)} route(s) that "
            f"existed before are gone: {[r.route_id for r in plan.vanished]}. This "
            f"app never deletes, so something else changed this village and the "
            f"rest of this comparison may not reflect what the run did."
        )
    return lines
