"""Derive a night profile's allocations from the account's own shape.

A night profile is not a preference, it is arithmetic. With the operator asleep
nothing is spent, so every unit that arrives stays, and the most a village may
accumulate per hour is fixed by its store:

    ceiling = (target_fill - baseline_fill) * capacity / window_hours

Measured from the BASELINE the operator leaves behind before sleeping, not from
whatever a snapshot happens to catch. Those are different numbers and only the
first repeats: a mid-afternoon reading is mid-accumulation, so a profile built
from one goes stale within the hour -- 34 of 100 allocations moved in a single
hour on the real account, three of them changing sign. Emptying before sleep is
what makes the baseline reproducible, and reproducible is what lets one profile
hold for weeks.

Two rules do the rest.

*Nobody exports what they have room for.* At night a village with space in its
warehouse gains nothing by shipping: moving 400 clay an hour fifteen hours across
the map buys nothing and spends merchants the plan needs elsewhere. So only two
kinds of village send -- one already past the target, and one drawn in to cover
what the army villages need, nearest first.

*Demand comes before supply.* A ceiling is an upper bound, not a target. The
stores can absorb far more than the account produces, so letting every village
claim its ceiling over-claims the account and drives the remainder negative.

Pure: no requests, no clock, no I/O. Everything it needs is passed in.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

# MATERIALS moved to `allocation` once four modules needed it; imported here
# both because this module uses it and so the older `night_profile.MATERIALS`
# import keeps resolving.
from .allocation import MATERIALS, Allocation, AllocationMode, Resource

DEFAULT_TARGET_FILL = 0.80
DEFAULT_BASELINE_FILL = 0.30


@dataclass(frozen=True)
class NightVillage:
    """What deriving a night profile needs to know about one village."""

    village_id: int
    name: str
    x: int
    y: int
    merchants_total: int
    trade_office_level: int
    warehouse_capacity: int
    granary_capacity: int
    production: Mapping[Resource, float]
    """Own net production per hour, after whatever the village SPENDS.

    Crop may be negative for an army village. A material may be negative too,
    once the operator declares a spend larger than the village makes: the
    caller nets `consumption_per_hour` off the material rates here, because a
    village that burns its whole lumber production has none to keep overnight
    and none to shed. Crop is NOT netted -- the snapshot's crop rate is already
    net of upkeep, so subtracting a spend would count the same troops twice.
    """

    max_busy_merchants: int | None = None
    """The operator's ceiling on merchants underway or returning at once.

    It belongs here for the same reason `production` is netted here: how much a
    village can SHED overnight is its fleet times what a merchant carries times
    the trips it can make, and under a cap the fleet that matters is the cap. A
    derivation that ignored it would hand a capped village a retention it has no
    way to honour -- which is the defect a declared spend had on this path
    before, seen from the other side. ``None`` is no ceiling declared."""

    def capacity_for(self, resource: Resource) -> int:
        return self.granary_capacity if resource is Resource.CROP else self.warehouse_capacity


@dataclass
class NightProfile:
    """The derived allocations, and what deriving them decided."""

    allocations: dict[Resource, dict[int, Allocation]] = field(default_factory=dict)
    forced_senders: dict[Resource, list[int]] = field(default_factory=dict)
    """Villages already past the target, which must give whatever the distance."""
    drawn_in: dict[Resource, list[int]] = field(default_factory=dict)
    """Villages pulled in to cover demand, nearest first."""
    unmet: dict[Resource, float] = field(default_factory=dict)
    """Demand no village could cover, per hour. Reported, never hidden."""
    residual_trimmed: float = 0.0
    """Crop per hour taken back off the largest share to stop the retentions
    claiming more than the account produces. A unit or two is the rounding of
    each retention to a whole number; more than that is a real deficit, and
    whatever the largest share could not cover lands in `unmet` instead."""


def _wrapped_fields(dx: float, dy: float, map_span: int) -> float:
    """Field distance on Travian's wrapped map.

    The map is a torus: the shortest path between two edge villages can cross
    the seam, and the game routes merchants that way. Raw hypot made a hub at
    (-200|0) see a supplier at (200|0) as 400 fields away when it is 1, so the
    night draw picked a genuinely distant village over a next-door one and
    understated how much edge villages can shed overnight.
    """
    half = map_span / 2.0
    dx = abs(dx) % map_span
    dy = abs(dy) % map_span
    if dx > half:
        dx = map_span - dx
    if dy > half:
        dy = map_span - dy
    return math.hypot(dx, dy)


def _hours(a: NightVillage, b: NightVillage, speed: float, map_span: int) -> float:
    return _wrapped_fields(a.x - b.x, a.y - b.y, map_span) / speed


def derive_night_profile(
    villages: Sequence[NightVillage],
    *,
    window_hours: float,
    speed_fields_per_hour: float,
    map_span: int,
    day_retention: Mapping[Resource, Mapping[int, float]],
    hub_id: int,
    consumer_ids: Sequence[int] = (),
    tribute_per_hour: float = 0.0,
    tribute_at: tuple[int, int] | None = None,
    baseline_fill: float = DEFAULT_BASELINE_FILL,
    target_fill: float = DEFAULT_TARGET_FILL,
    merchant_base_capacity: int = 2500,
    trade_office_bonus_per_level: float = 0.2,
    merchant_reserve: int = 2,
) -> NightProfile:
    """Allocations for one night, from capacities and production alone.

    Args:
        day_retention: what each village retains per hour under the DAY profile,
            per resource. The army villages' shares come from here rather than
            being invented, so the night is the day's plan bounded by the stores.
        hub_id: the village that absorbs surplus materials. At night its
            consumers fill up while it has room, so the day's direction reverses.
        consumer_ids: villages that consume crop. They break even -- ending the
            night at the fill they started -- which is what makes the profile
            hold rather than drift.
        tribute_per_hour / tribute_at: a foreign obligation and where it is. Paid
            by the villages nearest to it, since distance is what makes a tribute
            expensive in merchants.
    """
    by_id = {v.village_id: v for v in villages}
    if hub_id not in by_id:
        raise ValueError(f"hub {hub_id} is not among the villages given")
    consumers = [vid for vid in consumer_ids if vid in by_id]

    def ceiling(v: NightVillage, resource: Resource) -> int:
        return round((target_fill - baseline_fill) * v.capacity_for(resource) / window_hours)

    def shed_limit(v: NightVillage) -> float:
        """The most this village can send per hour and still be shippable.

        A retention below production only means something if the merchants exist
        to carry the difference, and how much a fleet moves in a night depends on
        how far it goes: a village one field from its neighbour turns round dozens
        of times, one an hour away twice.
        """
        capacity = merchant_base_capacity * (
            1 + trade_office_bonus_per_level * v.trade_office_level
        )
        fleet = max(0, v.merchants_total - merchant_reserve)
        # The operator's cap, where there is one, is what may actually be in the
        # air -- so it is the fleet this village ships the night with. The
        # tighter of the two, the same rule `VillageState.merchant_budget`
        # applies on the plan side: a cap above the fleet is not extra merchants.
        if v.max_busy_merchants is not None:
            fleet = min(fleet, v.max_busy_merchants)
        others = [_hours(v, o, speed_fields_per_hour, map_span) for o in villages if o is not v]
        if not others:
            return 0.0
        trips = max(1, int(window_hours // (2 * min(others))))
        return fleet * capacity * trips / window_hours

    def capped(v: NightVillage, resource: Resource) -> int:
        """Its ceiling, never asking for more export than it can ship."""
        own = v.production.get(resource, 0.0)
        return max(ceiling(v, resource), round(own - shed_limit(v)))

    profile = NightProfile()

    # ── Materials ────────────────────────────────────────────────────────────
    # The hub absorbs whatever its consumers cannot hold, which at night is most
    # of it: their stores fill in hours while the hub has room to spare.
    for resource in MATERIALS:
        entries: dict[int, Allocation] = {
            hub_id: Allocation(mode=AllocationMode.REMAINDER, value=0.0)
        }
        demand = -by_id[hub_id].production.get(resource, 0.0)
        day = day_retention.get(resource, {})
        forced: list[int] = []
        for vid, village in by_id.items():
            if vid == hub_id:
                continue
            own = village.production.get(resource, 0.0)
            room = ceiling(village, resource)
            wanted = day.get(vid)
            if wanted is not None and wanted > own:
                # A receiver under the day plan: give it what it can still hold.
                take = min(wanted, room)
                entries[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(take))
                demand += take - own
                continue
            if room < own:
                # Already past the target. It gives whatever the distance, because
                # leaving it to overflow is a certain loss.
                entries[vid] = Allocation(
                    mode=AllocationMode.ABSOLUTE, value=float(capped(village, resource))
                )
                demand -= own - capped(village, resource)
                forced.append(vid)
                continue
            if own < 0:
                # It spends more of this material than it makes, so it has
                # nothing to keep and needs the difference DELIVERED -- exactly
                # the claim the receiver branch above books as `take - own` with
                # a `take` of zero. Booked here, before the draw, so a village
                # with slack can cover it and whatever nobody covers is reported
                # in `unmet`. Its own retention is clamped to zero below: a
                # negative absolute retention is refused in `Allocation`, and
                # dropping the deficit instead would hand back a profile that
                # starves the village while claiming nothing was outstanding.
                demand += -own
        drawn: list[int] = []
        if demand > 0:
            order = sorted(
                (vid for vid in by_id if vid != hub_id and vid not in entries),
                key=lambda vid: _hours(by_id[vid], by_id[hub_id], speed_fields_per_hour, map_span),
            )
            for vid in order:
                if demand <= 0:
                    break
                own = by_id[vid].production.get(resource, 0.0)
                keep = day.get(vid, own)
                # Never more than it can carry. A retention below production is
                # a promise to ship the difference, and `shed_limit` is what
                # that promise costs -- the bound `capped()` already puts under
                # a FORCED sender, and the draw is the same promise made for a
                # different reason.
                give = min(own - keep, demand, shed_limit(by_id[vid]))
                if give <= 0:
                    continue
                entries[vid] = Allocation(
                    mode=AllocationMode.ABSOLUTE, value=float(round(own - give))
                )
                demand -= give
                drawn.append(vid)
        # Everyone untouched keeps exactly what it makes, stated rather than
        # omitted so it stays inside the conservation sum instead of leaving it.
        # Clamped at zero, for the reason the crop loop below is: a declared
        # spend larger than the village's production makes the rate NEGATIVE
        # (the caller nets it off, and `NightVillage.production` says so), and a
        # negative absolute retention is refused in `Allocation.__post_init__`
        # -- so `/night-profile` answered 500 for a spend one unit past
        # production. Nothing to keep is zero; the difference is already booked
        # as demand above, so the clamp reports it rather than hiding it.
        for vid, village in by_id.items():
            if vid not in entries:
                entries[vid] = Allocation(
                    mode=AllocationMode.ABSOLUTE,
                    value=float(max(0, round(village.production.get(resource, 0.0)))),
                )
        profile.allocations[resource] = entries
        profile.forced_senders[resource] = sorted(forced)
        profile.drawn_in[resource] = drawn
        profile.unmet[resource] = max(0.0, demand)

    # ── Crop ─────────────────────────────────────────────────────────────────
    crop: dict[int, Allocation] = {}
    demand = tribute_per_hour
    for vid in consumers:
        # Break even: end the night at the fill it started, which is what stops
        # the profile drifting from night to night.
        crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=0.0)
        demand += -by_id[vid].production.get(Resource.CROP, 0.0)

    forced_crop: list[int] = []
    for vid, village in by_id.items():
        if vid in crop:
            continue
        own = village.production.get(Resource.CROP, 0.0)
        if ceiling(village, Resource.CROP) < own:
            value = capped(village, Resource.CROP)
            crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(value))
            demand -= own - value
            forced_crop.append(vid)

    # The tribute is paid from nearest to it: distance is exactly what makes an
    # obligation expensive, because a short cycle over a long haul keeps many
    # sends in flight at once.
    drawn_crop: list[int] = []
    if demand > 0:
        if tribute_at is not None:
            tx, ty = tribute_at

            def _cost(vid: int) -> float:
                v = by_id[vid]
                return _wrapped_fields(v.x - tx, v.y - ty, map_span)
        else:

            def _cost(vid: int) -> float:
                return _hours(by_id[vid], by_id[hub_id], speed_fields_per_hour, map_span)

        for vid in sorted((v for v in by_id if v not in crop), key=_cost):
            if demand <= 0:
                break
            own = by_id[vid].production.get(Resource.CROP, 0.0)
            if own <= 0:
                continue
            # Bounded by what it can move, as the material draw above is.
            give = min(own, demand, shed_limit(by_id[vid]))
            if give <= 0:
                continue
            crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(round(own - give)))
            demand -= give
            drawn_crop.append(vid)

        # A ceiling is an upper bound on what a village may ACCUMULATE, not a
        # floor under what it may give. A forced sender was set to shed exactly
        # the excess over its ceiling and then skipped, so a hub making 60,000 an
        # hour offered 10,000 and the rest of the demand was reported unmet while
        # its crop sat there. When demand survives the first pass, come back to
        # them and take more -- down to retaining nothing, never below.
        for vid in sorted(forced_crop, key=_cost):
            if demand <= 0:
                break
            held = crop[vid].value
            own = by_id[vid].production.get(Resource.CROP, 0.0)
            # Down to retaining nothing, never below -- and never past what it
            # can ship. The forced pass already booked `own - held` of its shed
            # limit, so only the rest of that limit is still available here.
            give = min(held, demand, shed_limit(by_id[vid]) - (own - held))
            if give <= 0:
                continue
            crop[vid] = Allocation(mode=AllocationMode.ABSOLUTE, value=float(held - give))
            demand -= give
            if vid not in drawn_crop:
                drawn_crop.append(vid)

    for vid, village in by_id.items():
        if vid not in crop:
            # Clamped at zero. `consumer_ids` defaults to `()` and
            # `NightVillage.production` is documented as possibly negative for an
            # army village, so a caller can legally reach here with a village
            # that is LOSING crop -- and a negative absolute retention is refused
            # in `Allocation.__post_init__`, so the derivation raised at the
            # caller for supplying exactly what the signature invites. A village
            # losing crop retains none of it; the deficit belongs to the
            # receiving side, which is what `consumer_ids` is for. The HTTP path
            # never gets here (`post_night_profile` classifies consumers from
            # `crop < 0` first), so this is the library contract, not a route.
            crop[vid] = Allocation(
                mode=AllocationMode.ABSOLUTE,
                value=float(max(0, round(village.production.get(Resource.CROP, 0.0)))),
            )

    # Every retention above is an integer and production is not, so the rounded
    # parts miss the fractional whole by a fraction of a unit. The planner treats
    # ANY negative residual as the allocation over-claiming the account, so 0.29
    # crop an hour out of 106,558 made a plan that simulates perfectly read as not
    # executable. Give the rounding somewhere to land, taking it from the largest
    # entry -- the one least disturbed by losing a unit. Only when the demand was
    # covered, though: an unmet shortfall is not rounding, it is already reported
    # as ``unmet``, and trimming it would push a retention below zero.
    produced = sum(v.production.get(Resource.CROP, 0.0) for v in villages)
    claimed = sum(a.value for a in crop.values())
    residual = produced - tribute_per_hour - claimed
    over_claimed = 0.0
    if residual < 0 and demand <= 0:
        largest = max(crop, key=lambda vid: crop[vid].value)
        # Never more than that entry actually holds. The trim builds another
        # absolute retention, and `Allocation` refuses a negative one, so a
        # slack larger than the largest entry would raise instead of rounding.
        # `residual_trimmed` reports what was taken rather than what was wanted,
        # so the figure the operator reads is the one the profile used.
        taken = min(int(-residual) + 1, crop[largest].value)
        if taken > 0:
            crop[largest] = Allocation(
                mode=AllocationMode.ABSOLUTE, value=crop[largest].value - taken
            )
            profile.residual_trimmed = float(taken)
        # And what the clamp could NOT take is reported, not dropped. The gap is
        # only rounding when it is a unit or two; a crop-negative village that no
        # caller named a consumer is clamped to zero retention here, so the gap
        # can be that village's whole deficit -- 20,000/h against a 5,000/h share
        # to take it from, or against nothing at all when every retention is
        # already zero. Silently discarding it returned a profile that still
        # claimed more crop than the account makes while reporting no shortfall.
        # Reported rather than raised: the demand is real and the profile is
        # usable, and `unmet` is exactly the channel for demand nobody covered.
        over_claimed = max(0.0, -residual - taken)

    profile.allocations[Resource.CROP] = crop
    profile.forced_senders[Resource.CROP] = sorted(forced_crop)
    profile.drawn_in[Resource.CROP] = drawn_crop
    # Disjoint by construction: the trim above only runs when `demand <= 0`, so
    # at most one of the two terms is ever non-zero.
    profile.unmet[Resource.CROP] = max(0.0, demand) + over_claimed
    return profile
