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
    STARVATION_BY_DESIGN = "starvation_by_design"
    OVERFLOW_STRUCTURAL = "overflow_structural"
    OVERFLOW_BURST = "overflow_burst"
    OVERFLOW_PROJECTED = "overflow_projected"
    OVER_ALLOCATED = "over_allocated"
    TRIBUTE_UNFUNDED = "tribute_unfunded"
    UNALLOCATED = "unallocated"
    # Section 7's NPC balancing, in the three things it can say. CAPACITY_SHORT
    # replaced STOCK_FLOOR_UNSUSTAINABLE, which asked the same question of the
    # wrong model: it compared the draw against the village's crop surplus after
    # the fact, where the allowance IS the feedstock surplus and the comparison
    # belongs in the solve. CRITICAL rather than WARNING because the spec's rule
    # is to fail loudly: a plan whose cargo rests on conversion the feedstock
    # cannot fund is not a plan that merely misses a target.
    #
    # The other two are the spec's own triggers -- reports about when the
    # operator should trade, never an action the planner takes -- and they are
    # two categories because they say opposite things: one is a shortage about
    # to under-deliver routes, the other a surplus standing idle.
    NPC_CAPACITY_SHORT = "npc_capacity_short"
    NPC_WOOD_LOW = "npc_wood_low"
    NPC_CROP_BANKED = "npc_crop_banked"
    STOCK_FUNDED = "stock_funded"
    MERCHANT_MODEL_UNCALIBRATED = "merchant_model_uncalibrated"
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
    WHITELIST_VS_TRIBUTE = "whitelist_vs_tribute"
    UNREADABLE_RATE = "unreadable_rate"
    SEARCH_TRUNCATED = "search_truncated"
    STORE_FILLING = "store_filling"
    TRIBUTE_COLD_START = "tribute_cold_start"
    TRIBUTE_SPLIT = "tribute_split"
    SUSTAIN_NOOP = "sustain_noop"
    RELAY = "relay"
    # A declared material relay whose warehouse cannot absorb the pass-through,
    # in the two outcomes that are genuinely different things. TWO categories
    # and not one with two severities, because severity belongs to the category
    # here (see the module docstring, and `Finding.severity`, which reads it from
    # `_SPECS`) -- the same reason STARVATION and STARVATION_BY_DESIGN are two.
    # RELAY_BUFFER is cargo destroyed before anything was forwarded; _TIGHT is a
    # tier that delivers and then sheds what lands afterwards.
    RELAY_BUFFER = "relay_buffer"
    RELAY_BUFFER_TIGHT = "relay_buffer_tight"
    # Section 6's three night rules. NIGHT_OVERRUN is about the ROUTE SET (a
    # merchant still on the road when the day profile takes over), the other two
    # about the STATE the night hands over, at either end of it.
    NIGHT_OVERRUN = "night_overrun"
    MORNING_FLOOR = "morning_floor"
    PRE_NIGHT_BASELINE = "pre_night_baseline"


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
    Category.NIGHT_OVERRUN: _Spec(
        # Among the criticals, just after the overflow pair. It destroys nothing
        # directly, but the whole morning profile is planned on a merchant pool
        # that is not there -- so every figure on the day sheet is wrong at
        # 07:00, which is worse than one store shedding a batch.
        order=2.7,
        severity=Severity.CRITICAL,
        subject="route",
        headline="{count} {subject} still have merchants on the road when the night ends",
        action=(
            "Section 6 requires the night to CLOSE: nothing underway or returning at "
            "07:00, so the morning profile starts with a full pool everywhere. Each line "
            "gives the overrun. Shorten the cycle so the last send leaves earlier, ship "
            "from a nearer village, or phase the route so its last dispatch is a whole "
            "round trip inside the window. Not fixable by dropping a firing: the cargo "
            "was sized for the ones the plan counted, so trimming one under-delivers."
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
    Category.OVERFLOW_PROJECTED: _Spec(
        # First among the warnings: it is the only one measured in resources,
        # so it is the one an operator scanning for cost should meet first.
        # Ranks are per-severity, so this does not compete with the CRITICALs.
        order=9.5,
        severity=Severity.WARNING,
        subject="store",
        headline="{count} {subject} will reach their {resource} cap within the month",
        action=(
            "Nothing is lost yet. These stores are still filling; each line says when it "
            "arrives at the cap and what it will shed per day once it never leaves. Thirty "
            "days is a horizon rather than a forecast -- fields, troops and targets all "
            "move before then -- so this is worth a plan, not an emergency: give the "
            "{resource} somewhere to go, raise the store, or spend it in-village."
        ),
    ),
    Category.MORNING_FLOOR: _Spec(
        # A WARNING: the plan runs, and a village waking at 40% is short of a
        # target rather than losing anything. What it costs is a morning of
        # building and training, which is the operator's call to weigh -- and
        # the granary side of it has STARVATION above for the case where the
        # store actually empties.
        order=9.6,
        severity=Severity.WARNING,
        subject="store",
        headline="{count} {subject} wake below the morning fill floor",
        action=(
            "Section 6 asks every role village -- DEF and both OFF, capital excluded -- to "
            "be at 60% on warehouse AND granary at 07:00, so the day starts able to build "
            "and train without waiting for a delivery. Each line gives the measured "
            "percentage. Raise the night's share for those villages, draw on a nearer "
            "supplier, or lower the floor if the account cannot fund it."
        ),
    ),
    Category.PRE_NIGHT_BASELINE: _Spec(
        # Beside the morning floor: the two describe the same night from its two
        # ends, and reading them apart made the pair look like separate subjects.
        order=9.7,
        severity=Severity.WARNING,
        subject="store",
        headline="{count} {subject} start the night fuller than the profile assumes",
        action=(
            "The night is sized from the baseline the operator RE-ESTABLISHES by hand at "
            "the switch -- spend the stores down so no role village is above 25% -- and "
            "the room between that and the morning target is what the profile ships. A "
            "store starting higher has less room than was reserved for it, so the cargo "
            "arrives at a cap. Spend it down before the switch, or raise the baseline the "
            "profile is derived from so the arithmetic matches the account."
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
    Category.NPC_CAPACITY_SHORT: _Spec(
        # Beside OVER_ALLOCATED, which is the same fact seen without the
        # conversion: the account claims more than it has, and NPC was the thing
        # covering the gap. Reading them together is the point -- the operator
        # needs to know both that the plan over-claims and that the trading
        # meant to fund it cannot.
        order=3.5,
        severity=Severity.CRITICAL,
        subject="village",
        headline="{count} {subject} need more NPC conversion than their feedstock can fund",
        action=(
            "NPC exchanges one resource for another 1:1 inside one village, so a conversion "
            "is bounded by what that village RETAINS of the resources it is not shipping -- "
            "clay and crop at the balancing hub. Beyond that there is nothing to convert "
            "from and the routes it funds arrive short with nothing in the plan to say why. "
            "Ship less from it, raise the feedstock it keeps, or lower the claims it covers."
        ),
    ),
    Category.NPC_WOOD_LOW: _Spec(
        # Just above STOCK_FUNDED: that finding says the routes depend on NPC
        # trading, and this one says the trading is due now.
        order=19,
        severity=Severity.WARNING,
        subject="village",
        headline="{count} {subject} are at or below their wood floor",
        action=(
            "Section 7's first NPC trigger. The buffer these routes ship out of is gone, so "
            "convert clay or crop into lumber there before the cargo starts arriving short. "
            "The floor read is the village's own `stock_floor_fraction` of its warehouse -- "
            "the spec states no wood figure of its own, so this is the account's number."
        ),
    ),
    Category.NPC_CROP_BANKED: _Spec(
        order=35,
        severity=Severity.NOTE,
        subject="village",
        headline="{count} {subject} hold crop past the 700,000 NPC trigger",
        action=(
            "Section 7's second NPC trigger, and an opportunity rather than a fault: banked "
            "crop is feedstock. Convert it into whatever the account is short of -- wood, on "
            "this one -- or it sits there until the granary caps and sheds it."
        ),
    ),
    Category.STOCK_FUNDED: _Spec(
        # Immediately after NPC_WOOD_LOW, not two ranks below it: the two
        # describe one mechanism (what the conversion funds, and whether it is
        # due) and reading them either side of an unrelated note about the route
        # search made them look like separate subjects. Fractional, which is
        # what `order` being a float is for -- 11.5 does the same job for
        # MERCHANTS_CROWDED -- so no other rank moves. Ranks are per-severity,
        # so the CRITICAL 19 on UNREADABLE_RATE is a different scale and does
        # not collide with the WARNING 19 above.
        order=19.5,
        severity=Severity.WARNING,
        subject="village",
        headline="{count} {subject} ship more than they produce, funded from stock",
        action=(
            "These routes only deliver while the warehouse stays at its floor. That is an "
            "operator promise, not a fact of the account -- keep NPC trading, or the cargo "
            "arrives short with nothing in the plan to say why."
        ),
    ),
    Category.MERCHANT_MODEL_UNCALIBRATED: _Spec(
        # Just before MERCHANTS_BUSY: both are about the merchant budget, and
        # this one says how much to trust the numbers in the other.
        order=10.5,
        severity=Severity.WARNING,
        subject="village",
        headline="every merchant figure rests on an unmeasured Trade Office bonus",
        action=(
            "The base capacity was read off the game; the per-level bonus was carried "
            "over from the profile and never measured against it, so a village with a "
            "Trade Office has a capacity nobody has checked. Overstating capacity "
            "breaches the merchant budget invisibly, which is the unsafe direction. One "
            "Marketplace reading from a Trade Office 0 village settles the base with no "
            "inversion at all, and any second level then pins the bonus -- pass both "
            "through calibrate() and send the result as merchant_base_capacity and "
            "trade_office_bonus_per_level."
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
    Category.WHITELIST_VS_TRIBUTE: _Spec(
        order=18.5,
        severity=Severity.WARNING,
        # The HAUL, not the tribute: one finding is emitted per (origin, target)
        # row, so two restricted origins feeding ONE tribute counted as "2
        # tributes" and sent the operator looking for a second target that does
        # not exist. What there are two of is expensive hauls.
        subject="haul",
        headline="{count} {subject} leave a village restricted by ship_only_to for a tribute",
        action=(
            "`ship_only_to` restricts a village's OWN destinations only -- a foreign "
            "target is governed by its own `exclude_origins`, so no whitelist can stop "
            "one. That is deliberate: the list takes own village ids and a tribute has "
            "none, so binding it would leave 'keeps paying the tribute' impossible to "
            "say. But a distant tribute is the most expensive haul on the map and "
            "merchant thrift is usually why the whitelist exists, so this is worth "
            "seeing. Add the origin to that target's exclude_origins if you meant it "
            "kept off."
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
    Category.STARVATION_BY_DESIGN: _Spec(
        # First among the notes, and for the same reason STARVATION leads the
        # criticals: it is the only note carrying a countdown on something that
        # cannot be re-grown. The severity moved; the urgency of reading it did
        # not.
        order=29,
        severity=Severity.NOTE,
        subject="store",
        headline="{count} {subject} drain {resource} by design",
        action=(
            "Declared crop-negative: the troops eat more than the fields grow and the "
            "difference is shipped in, which is how an army village works rather than a "
            "fault to fix. The hours of cover on each line are still the number to act "
            "inside -- they say how long the granary lasts if the deliveries stop."
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
        # Groups are keyed by (category, resource), so this renders the resource
        # that is actually being relayed. It said "crop" until profile section
        # 5's DECLARED material tier existed, and a lumber pass-through reported
        # as a crop relay is a line the operator cannot reconcile with the sheet.
        headline="{count} {subject} carry {resource} through a hub instead of direct",
        action=(
            "Two rows on the sheet, one delivery: the hub forwards what it collects. The "
            "second leg is only as full as the first one made it, so creating one without "
            "the other ships nothing useful."
        ),
    ),
    Category.RELAY_BUFFER: _Spec(
        # Alongside the overflow criticals it belongs with: this IS a store
        # filling, and the resources are destroyed the same way. It reads after
        # them because it names a cause the generic overflow lines cannot.
        order=2.5,
        severity=Severity.CRITICAL,
        subject="relay",
        headline="{count} {subject} cannot hold the {resource} they are passing on",
        action=(
            # The remediation used to open with "shorten the COLLECTING leg's cycle", which
            # was written under the one-batch reading of the bound. On the operator's own
            # geometry the binding term is the FORWARD cycle and the collecting leg is
            # already at the 1h `DAILY_BEAT_CYCLES` minimum -- so the first thing offered
            # was unactionable in exactly the case the finding exists for.
            "A relay has to hold the collecting rate over the LONGER of its two cycles, and "
            "this one cannot -- so the cargo beyond the cap is destroyed at the relay, not "
            "late. Look at which cycle is the longer one on the sheet: if it is the FORWARD "
            "leg, more merchants at the relay buy it a shorter one (a leg already at 1h "
            "cannot be shortened further). Otherwise raise the relay's warehouse, or move "
            "those downstream villages to a relay that has the store for them."
        ),
    ),
    Category.RELAY_BUFFER_TIGHT: _Spec(
        # Beside RELAY_LATENCY, which is the other "the tier works, but not
        # comfortably" finding about the same two legs.
        order=12.5,
        severity=Severity.WARNING,
        subject="relay",
        headline="{count} {subject} run out of {resource} headroom later in the day",
        action=(
            "The relay does forward before it fills, so the tier is delivering -- but its "
            "warehouse tops out afterwards and sheds whatever lands next. What it has to "
            "hold is the collecting rate over the LONGER of its two cycles, so shorten "
            "whichever of them the sheet shows as the longer one -- more merchants at the "
            "relay for a forward leg, more at the source for a collecting one -- or raise "
            "the relay's warehouse."
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
