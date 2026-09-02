"""Planner findings: one fact, what it costs, and what to do about it.

A run on a 25-village account produced 153 warnings -- bare strings in one flat
list -- and the operator stopped reading, which wasted the two lines that
mattered along with the 151 that did not. The measured shape of that list is why
this module exists:

* 51 lines were the same systemic fact ("this village keeps everything it makes
  and its warehouse is full") repeated once per village with an identical number.
* Another 51 were the *same stores* described a second time by the continuous
  fill-time check, so half the list was a duplicate of the other half.
* The single most expensive line -- a 1.9M/day crop loss, forty times the next
  biggest -- sat in the middle, visually identical to a 22,224/day one.
* 45 lines ended with the identical clause "geometry or the merchant budget may
  forbid better". The distinguishing content was a route name and one number.
* Nothing anywhere gave the total, which is the number that decides whether the
  operator cares at all.

So a finding is not a string. It carries what it is (:class:`Category`), who it
is about (``village``, ``resource``), what it costs (``loss_per_day``) and a
short distinguishing ``detail``; :func:`summarise` folds the flat list into
ranked groups with counts, totals and one shared action apiece. The prose stays
-- ``message`` is the same line the operator used to read, and the endpoint still
returns every one of them -- but nothing downstream has to parse English to
group, rank or total.

Severity belongs to the *category*, never to the individual finding. Whether a
missed latency target is worth interrupting someone over cannot depend on which
route missed it, and letting each call site decide its own urgency is exactly how
a flat list of 153 undifferentiated lines happens in the first place.

Pure functions over already-computed state. Nothing here spends a game request.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import cycle: allocation.py emits findings
    from .allocation import Resource


class Severity(StrEnum):
    CRITICAL = "critical"
    """Resources or troops are being destroyed, or the plan cannot be executed."""

    WARNING = "warning"
    """The plan runs, but it misses a target the operator set."""

    NOTE = "note"
    """A neutral observation. Nothing to do; here so the picture is complete."""


class Category(StrEnum):
    """What kind of finding this is. One member per warning the planner raises.

    Deliberately not a free-form label: the category decides the severity, the
    aggregate wording and the action, so adding a warning means deciding all
    three rather than appending another sentence to a list nobody reads.
    """

    STARVATION = "starvation"
    OVERFLOW_STRUCTURAL = "overflow_structural"
    OVERFLOW_BURST = "overflow_burst"
    OVER_ALLOCATED = "over_allocated"
    TRIBUTE_UNFUNDED = "tribute_unfunded"
    UNALLOCATED = "unallocated"
    STOCK_FLOOR_UNSUSTAINABLE = "stock_floor_unsustainable"
    STOCK_FUNDED = "stock_funded"
    MERCHANTS_BUSY = "merchants_busy"
    MERCHANTS_CROWDED = "merchants_crowded"
    RELAY_LATENCY = "relay_latency"
    LATENCY = "latency"
    ARRIVAL_GAP = "arrival_gap"
    CYCLE_TOO_SHORT = "cycle_too_short"
    CYCLE_VS_WINDOW = "cycle_vs_window"
    WINDOW_NOT_ENFORCEABLE = "window_not_enforceable"
    WINDOW_PRUNED = "window_pruned"
    RESERVED_WINDOW = "reserved_window"
    MANUAL_TRANSFER = "manual_transfer"
    UNREADABLE_RATE = "unreadable_rate"
    SEARCH_TRUNCATED = "search_truncated"
    STORE_FILLING = "store_filling"
    TRIBUTE_COLD_START = "tribute_cold_start"
    TRIBUTE_SPLIT = "tribute_split"
    SUSTAIN_NOOP = "sustain_noop"
    RELAY = "relay"


@dataclass(frozen=True)
class _Spec:
    """Everything the aggregate view needs to know about one category."""

    order: float
    """Editorial rank inside a severity. Fixed, so two runs read the same way.

    Float rather than int so a finding can be slotted between two existing ones
    without renumbering everything below it -- a renumbering diff hides what
    actually changed. The sort key that reads this is fully deterministic, so
    fractional ranks order exactly as whole ones do.
    """

    severity: Severity
    subject: str
    """What the count counts: villages, routes, stores, tributes, allocations."""

    headline: str
    """Aggregate wording. Placeholders: {count} {subject} {loss} {resource}."""

    action: str
    """What to DO, said once for the whole group. Placeholder: {resource}."""


# Order is the reading order. Starvation leads because it destroys troops rather
# than surplus, and an army cannot be re-grown from a warehouse.
_SPECS: Mapping[Category, _Spec] = {
    Category.STARVATION: _Spec(
        order=0,
        severity=Severity.CRITICAL,
        subject="store",
        headline="{resource} runs dry in {count} {subject}",
        action=(
            "Troops starve when a granary empties, and an army cannot be re-grown from "
            "a warehouse. Ship {resource} in, or cut the drain, before the countdowns "
            "below expire."
        ),
    ),
    Category.OVERFLOW_STRUCTURAL: _Spec(
        order=1,
        severity=Severity.CRITICAL,
        subject="village",
        headline="{loss}/day of {resource} lost at the store cap in {count} {subject}",
        action=(
            "The average inflow itself does not fit, so no amount of re-scheduling helps: "
            "give the {resource} somewhere to go (set a remainder village, or an absolute "
            "target below production), raise the store, or spend it in-village."
        ),
    ),
    Category.OVERFLOW_BURST: _Spec(
        order=2,
        severity=Severity.CRITICAL,
        subject="village",
        headline="{loss}/day of {resource} lost to oversized batches in {count} {subject}",
        action=(
            "The average rate fits; one delivery does not. Shorten those routes' cycles so "
            "each batch is smaller, or raise the store to hold a whole batch."
        ),
    ),
    Category.OVER_ALLOCATED: _Spec(
        order=3,
        severity=Severity.CRITICAL,
        subject="resource",
        headline="{resource} allocations claim more than the account produces",
        action=(
            "The remainder village would have to ship {resource} it does not have. Lower a "
            "target, or move the remainder to a village with real surplus."
        ),
    ),
    Category.TRIBUTE_UNFUNDED: _Spec(
        order=4,
        severity=Severity.CRITICAL,
        subject="tribute",
        headline="{count} {subject} owed crop that no village can supply",
        action=(
            "Free crop somewhere -- lower another village's target, or drop the obligation. "
            "As planned it simply will not be paid."
        ),
    ),
    Category.UNALLOCATED: _Spec(
        order=10,
        severity=Severity.WARNING,
        subject="resource",
        headline="{resource} has unallocated production and no remainder village",
        action=(
            "Slack piles up wherever it happens to be produced, and is lost once those "
            "stores fill. Name a remainder village for {resource} so it lands somewhere "
            "you chose."
        ),
    ),
    Category.STOCK_FLOOR_UNSUSTAINABLE: _Spec(
        order=19,
        severity=Severity.WARNING,
        subject="village",
        headline="{count} {subject} draw their stock floor down faster than it refills",
        action=(
            "NPC trades crop for materials one for one, so a floor refills no faster than "
            "the village's crop surplus. Drawn harder than that the floor sinks, and the "
            "routes it funds quietly start under-delivering. Ship less from it, raise its "
            "crop, or lower the claims it is covering."
        ),
    ),
    Category.STOCK_FUNDED: _Spec(
        order=21,
        severity=Severity.WARNING,
        subject="village",
        headline="{count} {subject} ship more than they produce, funded from stock",
        action=(
            "These routes only deliver while the warehouse stays at its floor. That is an "
            "operator promise, not a fact of the account -- keep NPC trading, or the cargo "
            "arrives short with nothing in the plan to say why."
        ),
    ),
    Category.MERCHANTS_BUSY: _Spec(
        order=11,
        severity=Severity.WARNING,
        subject="village",
        headline="{count} {subject} commit more merchants than are free right now",
        action=(
            "Existing routes or shipments hold the rest. Disable the stale routes first "
            "(the live run does that for you), or run again once the merchants are home."
        ),
    ),
    Category.MERCHANTS_CROWDED: _Spec(
        order=11.5,
        severity=Severity.WARNING,
        subject="village",
        headline="{count} {subject} run out of merchant headroom while a nearer village idles",
        action=(
            "The plan already tries to spread load, so these are the ones it could not "
            "move: shifting the work would have cost more merchants than the headroom is "
            "priced at, or nothing else has the surplus. A village at its ceiling has "
            "nothing left for a manual send or next run's drift, and one Trade Office "
            "there is a large share of the plan. Raise its Trade Office, give the idle "
            "village named below some surplus to ship, or accept it."
        ),
    ),
    Category.RELAY_LATENCY: _Spec(
        order=12,
        severity=Severity.WARNING,
        subject="relay",
        headline="{count} {subject} miss the latency target end-to-end",
        action=(
            "Even where every leg is inside the target, the delivery need not be: the "
            "cargo also waits at the hub for its next dispatch. Shorten the forwarding "
            "leg's cycle, ship those villages direct (set max_relay_hops to 0), or "
            "accept it."
        ),
    ),
    Category.LATENCY: _Spec(
        order=13,
        severity=Severity.WARNING,
        subject="route",
        headline="{count} {subject} miss the latency target",
        action=(
            "Geometry or the merchant budget may forbid better -- this is a soft target, "
            "not a failure. Add merchants at the origin, raise the latency target, or "
            "accept it."
        ),
    ),
    Category.ARRIVAL_GAP: _Spec(
        order=14,
        severity=Severity.WARNING,
        subject="route",
        headline="{count} {subject} land too close to another arrival",
        action=(
            "A busy destination cannot space what it is given. Route fewer villages into "
            "the same receiver, or lower the arrival-gap target."
        ),
    ),
    Category.CYCLE_TOO_SHORT: _Spec(
        order=15,
        severity=Severity.WARNING,
        subject="route",
        headline="{count} {subject} fire faster than the arrival-gap target",
        action=(
            "No dispatch offset can space a route's own repeats. Lengthen the cycle, or "
            "lower the arrival-gap target."
        ),
    ),
    Category.WINDOW_PRUNED: _Spec(
        order=15,
        # A NOTE, because the thing that made this critical is being dealt with.
        # The rows that would fire outside the profile are deleted after the route
        # is created, so the window really is enforced -- but the plan DEPENDS on
        # that happening, and a plan whose correctness rests on a later step
        # should say so rather than look untroubled.
        severity=Severity.NOTE,
        subject="route",
        headline="{count} {subject} rely on the out-of-window rows being pruned",
        action=(
            "Travian fans a repeat interval across the whole day, so the rows departing "
            "outside these hours are deleted after each route is created. That is what "
            "makes the window real; if the prune fails the run says so, and those routes "
            "then ship round the clock."
        ),
    ),
    Category.WINDOW_NOT_ENFORCEABLE: _Spec(
        order=15,
        severity=Severity.CRITICAL,
        subject="route",
        headline="{count} {subject} keep firing outside the profile's hours",
        action=(
            "A Gold Club route carries only 'repeat every N hours' -- Travian fans that "
            "across the whole day and there is no setting to confine it to a profile's "
            "hours. The cargo was sized for the firings inside the window, so the "
            "village receives every firing outside it as well. Use a 24h cycle, widen "
            "the profile to the whole day, or switch the route set off at the window's "
            "end and on again at its start."
        ),
    ),
    Category.CYCLE_VS_WINDOW: _Spec(
        order=16,
        severity=Severity.WARNING,
        subject="route",
        headline="{count} {subject} cannot fit their cycle into the profile's hours",
        action=(
            "They send once a day instead of every cycle, so they deliver a fraction of "
            "what was planned. Shorten the cycle, or widen the profile's window."
        ),
    ),
    Category.RESERVED_WINDOW: _Spec(
        order=17,
        severity=Severity.WARNING,
        subject="route",
        headline="{count} {subject} land inside the reserved window",
        action="Nothing else fits. Widen the window's slack, or accept the arrivals in it.",
    ),
    Category.MANUAL_TRANSFER: _Spec(
        order=18,
        severity=Severity.WARNING,
        subject="tribute",
        headline="{count} {subject} must be shipped by hand",
        action=(
            "Travian only routes to your own, Wonder, or alliance-artifact villages, so no "
            "merchants are reserved for these. Ship them by hand, or mark them "
            "route-eligible if they really are one of those."
        ),
    ),
    Category.UNREADABLE_RATE: _Spec(
        order=19,
        # CRITICAL, not advisory. The operator gave this village an explicit rate
        # and the plan silently did not honour it -- the village plans as if
        # untargeted, so part of what was asked for is simply absent from the
        # sheet. The documented feasibility contract promises every receiver's
        # demand can be supplied, and a green light over an ignored instruction
        # contradicts it. Distinct from MANUAL_TRANSFER, which stays a warning:
        # that one is a real Travian restriction the plan states correctly and
        # hands to the operator, not an instruction it dropped.
        severity=Severity.CRITICAL,
        subject="allocation",
        headline="{count} {subject} were ignored: no production rate could be read",
        action=(
            "Those villages plan as if untargeted. Re-fetch the snapshot -- the statistics "
            "page needs Travian Plus to be readable."
        ),
    ),
    Category.SEARCH_TRUNCATED: _Spec(
        order=20,
        severity=Severity.WARNING,
        subject="search",
        headline="the route search stopped before it converged",
        action=(
            "A truncated search overstates how many villages are over budget, so the "
            "merchant figures may be pessimistic. Raise max_improve_passes and re-plan."
        ),
    ),
    Category.STORE_FILLING: _Spec(
        order=30,
        severity=Severity.NOTE,
        subject="store",
        headline="{count} {subject} of {resource} fill up within the day",
        action=(
            "Nothing is lost yet, and a village with surplus filling its store is normal. "
            "Worth knowing only if you were about to leave it unattended."
        ),
    ),
    Category.TRIBUTE_COLD_START: _Spec(
        order=31,
        severity=Severity.NOTE,
        subject="tribute",
        headline="{count} {subject} need covering by hand until the first send lands",
        action=(
            "A Gold Club route fires at its scheduled send time, so the first delivery is "
            "up to one cycle plus travel away. Create the route shortly before its send "
            "time and the wait is roughly travel time."
        ),
    ),
    Category.TRIBUTE_SPLIT: _Spec(
        order=32,
        severity=Severity.NOTE,
        subject="tribute",
        headline="{count} {subject} are supplied by more than one village",
        action=(
            "Several routes to keep track of. Raising one supplier's share would let a "
            "single route cover the obligation."
        ),
    ),
    Category.SUSTAIN_NOOP: _Spec(
        order=33,
        severity=Severity.NOTE,
        subject="target",
        headline="{count} sustain {subject} have nothing to sustain",
        action=(
            "Sustain covers a negative production deficit; these villages are not losing "
            "{resource}, so the mode does nothing. Harmless, but not what was intended."
        ),
    ),
    Category.RELAY: _Spec(
        order=34,
        severity=Severity.NOTE,
        subject="relay",
        headline="{count} {subject} carry crop through a hub instead of direct",
        action=(
            "Two rows on the sheet, one delivery: the hub forwards what it collects. The "
            "second leg is only as full as the first one made it, so creating one without "
            "the other ships nothing useful."
        ),
    ),
}

_SEVERITY_ORDER: Mapping[Severity, int] = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.NOTE: 2,
}

# A single finding worth this share of the account's whole daily loss is named in
# the headline. Below it, naming one village would imply the rest are noise.
DOMINANT_SHARE = 0.4


@dataclass(frozen=True)
class Finding:
    """One thing the planner noticed, in a form that can be grouped and ranked."""

    category: Category
    message: str
    """The full sentence a person reads. Unchanged from the flat warning list."""

    detail: str = ""
    """The distinguishing part only -- "03 - 22,224/day", "18 -> 02: 5.9h".

    What the shared prose in ``message`` strips down to once the group has said
    the common reason once. Empty for a finding that is its own whole story.
    """

    village: str = ""
    """As the operator names it. Never an id: see ``allocation.village_label``."""

    resource: Resource | None = None
    loss_per_day: float = 0.0
    """Resources destroyed per day, and 0 when the finding costs none.

    The only magnitude here, deliberately. Hours of latency and counts of
    merchants are not commensurable with resources per day, so totalling them
    into one "severity score" would produce a number that means nothing.
    """

    @property
    def severity(self) -> Severity:
        return _SPECS[self.category].severity

    @property
    def action(self) -> str:
        return _SPECS[self.category].action.format(resource=self.resource or "the resource")


@dataclass(frozen=True)
class FindingGroup:
    """Every finding of one category about one resource, as a single item."""

    category: Category
    severity: Severity
    resource: Resource | None
    headline: str
    action: str
    count: int
    loss_per_day: float
    findings: tuple[Finding, ...]

    @property
    def key(self) -> str:
        """Stable identity, for a UI that remembers which groups were expanded."""
        return f"{self.category.value}:{self.resource or ''}"


@dataclass(frozen=True)
class ResourceLoss:
    resource: Resource
    per_day: float


@dataclass(frozen=True)
class Diagnostics:
    """The whole finding list, ranked and totalled. What the operator reads."""

    headline: str
    """One sentence. If they read nothing else, they read this."""

    total_loss_per_day: float
    loss_by_resource: tuple[ResourceLoss, ...]
    counts: Mapping[str, int] = field(default_factory=dict)
    """Findings per severity value. Group counts are len(groups)."""

    groups: tuple[FindingGroup, ...] = ()


def _plural(subject: str, count: int) -> str:
    return subject if count == 1 else f"{subject}s"


def _group_headline(
    category: Category, resource: Resource | None, findings: Sequence[Finding]
) -> str:
    """One line for the group. A group of one is its own message.

    A lone finding already reads as a complete sentence naming its village and
    its number, so aggregating it into "1 village ..." would lose information to
    say less.
    """
    spec = _SPECS[category]
    if len(findings) == 1:
        return findings[0].message
    headline = spec.headline.format(
        count=len(findings),
        subject=_plural(spec.subject, len(findings)),
        loss=f"{sum(f.loss_per_day for f in findings):,.0f}",
        resource=resource or "resources",
    )
    if findings[0].detail:
        headline += f" (worst: {findings[0].detail})"
    return headline


def _account_headline(
    total_loss: float,
    groups: Sequence[FindingGroup],
    counts: Mapping[str, int],
    routes_planned: int | None = None,
) -> str:
    """The five-second read: what is being lost, and where most of it is.

    Says "this ACCOUNT loses", not "this plan destroys". The distinction is not
    pedantry -- the earlier wording was measurably false. Every overflow figure
    here comes from replaying production against capacity, so a plan with no
    routes at all still totalled a loss and announced that the plan had caused
    it: a one-village account, where no route is even possible, reported
    480,000/day destroyed "by this plan".

    The losses are real; the causation was invented. What cannot be separated
    from the findings alone is how much of it the plan could have prevented --
    that needs the route set, which lives in the caller. ``routes_planned`` is
    the one piece worth stating outright: when the plan ships nothing, the loss
    is entirely the account's own production with nowhere to go, and saying so
    stops an operator hunting for a planning mistake that is not there.
    """
    if total_loss > 0:
        worst = max(
            (f for group in groups for f in group.findings),
            key=lambda f: f.loss_per_day,
        )
        sentence = f"This account loses {total_loss:,.0f} resources a day at its store caps"
        if worst.loss_per_day >= total_loss * DOMINANT_SHARE and worst.village:
            sentence += f" — {worst.loss_per_day:,.0f} of it {worst.resource} in {worst.village}"
        sentence += "."
        if routes_planned == 0:
            sentence += (
                " This plan ships nothing, so none of that is the plan's doing:"
                " it is production with nowhere to go."
            )
        return sentence
    critical = counts.get(Severity.CRITICAL.value, 0)
    if critical:
        return (
            f"Nothing is being wasted, but {critical} "
            f"{'thing' if critical == 1 else 'things'} must be fixed before this plan is "
            f"safe to run."
        )
    warnings = counts.get(Severity.WARNING.value, 0)
    if warnings:
        return (
            f"Nothing is being wasted; {warnings} "
            f"{'target is' if warnings == 1 else 'targets are'} missed."
        )
    return "No problems found."


def summarise(findings: Sequence[Finding], *, routes_planned: int | None = None) -> Diagnostics:
    """Fold a flat finding list into ranked groups, counts and totals.

    Groups are keyed by (category, resource) and ordered by severity, then by
    the category's editorial rank, then by the resources they cost per day.
    Within a group the findings are ordered by cost, worst first, and a stable
    sort keeps the producer's own ordering for ties -- which is what puts the
    soonest starvation countdown at the top of its group.
    """
    grouped: dict[tuple[Category, Resource | None], list[Finding]] = {}
    for finding in findings:
        grouped.setdefault((finding.category, finding.resource), []).append(finding)

    groups: list[FindingGroup] = []
    for (category, resource), members in grouped.items():
        ranked = tuple(sorted(members, key=lambda f: -f.loss_per_day))
        spec = _SPECS[category]
        groups.append(
            FindingGroup(
                category=category,
                severity=spec.severity,
                resource=resource,
                headline=_group_headline(category, resource, ranked),
                action=spec.action.format(resource=resource or "the resource"),
                count=len(ranked),
                loss_per_day=sum(f.loss_per_day for f in ranked),
                findings=ranked,
            )
        )
    groups.sort(
        key=lambda g: (
            _SEVERITY_ORDER[g.severity],
            _SPECS[g.category].order,
            -g.loss_per_day,
            str(g.resource or ""),
        )
    )

    per_resource: dict[Resource, float] = {}
    for finding in findings:
        if finding.loss_per_day and finding.resource is not None:
            per_resource[finding.resource] = (
                per_resource.get(finding.resource, 0.0) + finding.loss_per_day
            )
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity.value] = counts.get(finding.severity.value, 0) + 1

    total_loss = sum(per_resource.values())
    return Diagnostics(
        headline=_account_headline(total_loss, groups, counts, routes_planned),
        total_loss_per_day=total_loss,
        loss_by_resource=tuple(
            ResourceLoss(resource=resource, per_day=per_day)
            for resource, per_day in sorted(per_resource.items(), key=lambda kv: -kv[1])
        ),
        counts=counts,
        groups=tuple(groups),
    )
