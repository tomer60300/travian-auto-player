"""Rounding a mixed cargo must not substitute one resource for another.

A Gold Club route carries one fixed integer cargo, repeated. So the rounding
that turns a per-hour rate into that cargo is not a per-send approximation that
averages out over the day -- it is the cargo, every single departure, forever.

The planner rounded the mix while preserving only its AGGREGATE total:

    target_total = ceil(sum(every resource's amount))

That keeps the merchant budget honest, which is why it was written that way, but
it lets one resource take another's share. 0.6 lumber + 0.4 crop becomes 1 lumber
and 0 crop -- and because the identical cargo repeats, the crop is never
delivered at all, while the lumber is permanently overdrawn.

Travian keeps crop in a separate store and starvation depends on crop
specifically, so the case that matters is a crop recipient the float-based
simulation believes is being fed while the emitted integer schedule sends it
nothing.

The fix is a floor, not a redistribution: a resource with a nonzero rate ships at
least one unit per departure. That overshoots a sub-unit rate -- by definition,
since one unit is the smallest thing a route can carry -- but it delivers the
resource, and the overshoot is bounded at under one unit per resource per send.

The related question of whether a route moving twenty-four units a DAY should
exist at all is separate, and left alone here: that is about the row footprint,
not about shipping the wrong resource.
"""

import math

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.planner import (
    CEIL_DUST_TOLERANCE,
    round_preserving_total,
)


def _round(batch):
    """Exactly what the planner does when it builds a sheet row."""
    return round_preserving_total(
        batch,
        target_total=math.ceil(sum(batch.values()) - CEIL_DUST_TOLERANCE),
        min_each=1,
    )


class TestEveryRequestedResourceIsActuallyShipped:
    def test_a_minority_resource_is_not_dropped(self):
        # The reviewer's case. Previously: {lumber: 1} -- crop silently gone.
        out = _round({Resource.LUMBER: 0.6, Resource.CROP: 0.4})
        assert out.get(Resource.CROP, 0) >= 1, f"crop must actually travel: {out}"
        assert out.get(Resource.LUMBER, 0) >= 1

    def test_it_holds_whichever_way_round_the_fractions_fall(self):
        out = _round({Resource.LUMBER: 0.4, Resource.CROP: 0.6})
        assert out.get(Resource.LUMBER, 0) >= 1
        assert out.get(Resource.CROP, 0) >= 1

    def test_a_third_resource_is_not_squeezed_out_by_two_larger_ones(self):
        out = _round({Resource.CROP: 0.9, Resource.LUMBER: 0.9, Resource.IRON: 0.2})
        assert out.get(Resource.IRON, 0) >= 1, f"iron must travel too: {out}"

    def test_a_tiny_share_survives_beside_a_large_one(self):
        # The starvation shape: a big materials cargo and a trickle of crop. The
        # crop is the half that keeps troops alive and the half that was dropped.
        out = _round({Resource.LUMBER: 1200.6, Resource.CROP: 0.4})
        assert out.get(Resource.CROP, 0) >= 1
        assert out.get(Resource.LUMBER, 0) >= 1200

    def test_a_resource_that_was_never_asked_for_stays_absent(self):
        # The floor applies to what the plan wants shipped, not to everything: a
        # route asked to carry lumber must not start carrying crop.
        out = _round({Resource.LUMBER: 5.0})
        assert out.get(Resource.CROP, 0) == 0
        assert out.get(Resource.IRON, 0) == 0


class TestTheOvershootStaysBounded:
    def test_whole_number_cargo_is_untouched(self):
        # Nothing about ordinary traffic should move. This is the common case and
        # it must round to itself exactly.
        batch = {Resource.LUMBER: 7000.0, Resource.CLAY: 3000.0, Resource.CROP: 2000.0}
        out = _round(batch)
        assert out == {Resource.LUMBER: 7000, Resource.CLAY: 3000, Resource.CROP: 2000}

    def test_no_resource_is_overshot_by_a_whole_unit(self):
        # One unit is the smallest thing a route can carry, so a sub-unit rate is
        # necessarily overshot -- but never by more than that.
        batch = {Resource.LUMBER: 0.6, Resource.CROP: 0.4, Resource.IRON: 0.1}
        out = _round(batch)
        for resource, wanted in batch.items():
            got = out.get(resource, 0)
            assert got - wanted < 1.0, f"{resource.value}: {wanted} -> {got}"

    def test_a_large_mixed_cargo_still_sums_to_its_budgeted_total(self):
        # The reason aggregate rounding existed: route_cost sized the merchants
        # for ceil(sum). Where no resource needs the floor, that must still hold
        # exactly, or the sheet quietly outgrows its merchant budget.
        batch = {Resource.LUMBER: 4000.4, Resource.CLAY: 3000.3, Resource.CROP: 2000.3}
        out = _round(batch)
        assert sum(out.values()) == math.ceil(sum(batch.values()) - CEIL_DUST_TOLERANCE)


class TestTheFloorMustNotBreachTheMerchantBudget:
    """Found by self-review, and it is the dangerous direction.

    `route_cost` sizes a route's merchants from `ceil(sum)` -- the same target the
    rounding is given. Adding a unit AFTER that total is fixed can push the send
    past a merchant boundary the budget never accounted for: a cargo of 23,999.49
    + 0.3 + 0.2 targets 24,000, fits two 12,000 merchants, and shipped 24,002 --
    which needs three. The sheet would then understate its own cost, and the
    village's budget is breached invisibly, which is exactly the failure the
    codebase warns about elsewhere.

    So the floor is satisfied by REDISTRIBUTION wherever the total allows it:
    take from the largest entries, give to the starved ones, and the sum is
    untouched. Only a total smaller than the number of resources cannot go round,
    and there the excess is at most a few units on a route already carrying
    almost nothing.
    """

    CAP = 12_000

    def _merchants(self, units):
        return math.ceil(units / self.CAP)

    def test_a_cargo_on_a_merchant_boundary_stays_within_its_budget(self):
        batch = {Resource.LUMBER: 23_999.49, Resource.CROP: 0.3, Resource.IRON: 0.2}
        target = math.ceil(sum(batch.values()) - CEIL_DUST_TOLERANCE)
        out = _round(batch)

        assert sum(out.values()) == target, "redistribution must not change the total"
        assert self._merchants(sum(out.values())) == self._merchants(target)

    def test_and_every_resource_still_travels(self):
        # The original fix must survive the correction to it.
        out = _round({Resource.LUMBER: 23_999.49, Resource.CROP: 0.3, Resource.IRON: 0.2})
        assert out[Resource.CROP] >= 1
        assert out[Resource.IRON] >= 1
        assert out[Resource.LUMBER] >= 23_990, "the donor keeps almost all of it"

    def test_it_holds_across_the_boundaries_that_triggered_it(self):
        for base in (self.CAP - 1, 2 * self.CAP - 1, 3 * self.CAP - 1):
            for frac in (0.4, 0.49):
                batch = {
                    Resource.LUMBER: base + frac,
                    Resource.CROP: 0.3,
                    Resource.IRON: 0.2,
                }
                target = math.ceil(sum(batch.values()) - CEIL_DUST_TOLERANCE)
                out = _round(batch)
                assert self._merchants(sum(out.values())) == self._merchants(target), (
                    f"base {base} +{frac}: {sum(out.values())} needs more merchants "
                    f"than the {target} budgeted"
                )

    def test_a_total_too_small_to_go_round_still_ships_everything(self):
        # Two resources and a total of one: no redistribution can give both a
        # unit, so this is the one case that must exceed the target. The route is
        # carrying two units, so the cost of that is a rounding artefact at worst.
        out = _round({Resource.LUMBER: 0.6, Resource.CROP: 0.4})
        assert out[Resource.LUMBER] >= 1 and out[Resource.CROP] >= 1
        assert sum(out.values()) == 2
