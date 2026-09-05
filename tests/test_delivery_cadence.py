"""Some obligations are about cadence, not just about volume.

The planner optimises a route's rate: 47,167 crop an hour is satisfied by 47,167
every hour, and equally by 94,334 every two, or 377,336 every eight. For a store
that is arithmetically the same. For an ally being fed it is not -- a tribute that
lands in one lump every eight hours is eight hours of nothing followed by a burst,
and the operator asked for it hourly, within a couple of per cent.

Nothing could express that. The optimizer picks whichever cycle commits the
fewest merchants and then spends idle ones buying latency, so the suppliers of one
foreign target came out on 2h, 4h and 8h cycles -- correct by every measure it
was given, and wrong for the obligation.

`max_cycle_hours` on a target is that missing constraint. It bounds the cycles
the optimizer may consider for routes to that destination, which is enough: every
piece of cycle machinery already reads its candidates from one sequence.

Cadence costs merchants, and a lot of them. A one-hour cycle over a seven-hour
round trip keeps seven sends in the air at once, where an eight-hour cycle keeps
one. That is the trade the constraint makes explicit rather than hiding.
"""

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import DAILY_BEAT_CYCLES, EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import VillageState, _cycles_for
from travian_api.services.distribution.planner import PlannerConfig, craft_plan

GEOMETRY = MapGeometry(span=401, speed_fields_per_hour=12.0)
# Chosen so a LONG cycle is genuinely the merchant-minimal answer: at this rate
# one merchant carries a whole 8-hour batch, so eight hourly sends cost seven
# merchants in flight where one eight-hourly send costs two. Measured, not
# guessed -- at 20,000/h the shortest cycle already wins on merchants and ties
# break to it, so the cap would have had nothing to prove.
SUPPLY = 2_000.0


def _plan(*, cap=None, distance=40):
    """One well-supplied village feeding a distant one."""
    villages = {
        1: VillageState(
            village_id=1, x=0, y=0, merchant_count=200, trade_office_level=19, name="src"
        ),
        2: VillageState(village_id=2, x=distance, y=0, merchant_count=20, name="dst"),
    }
    productions = {Resource.CROP: {1: SUPPLY, 2: 0.0}}
    allocations = {
        Resource.CROP: {
            1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            2: Allocation(mode=AllocationMode.ABSOLUTE, value=SUPPLY),
        }
    }
    config = PlannerConfig(
        geometry=GEOMETRY,
        merchant_model=EUROPE2_TEUTON,
        # No latency target: with one, the idle-merchant pass already buys the
        # shortest affordable cycle and there would be nothing for the cap to do.
        # This isolates the cap as the only thing that can shorten a cycle.
        max_latency_hours=None,
        max_cycle_by_destination={2: cap} if cap else {},
    )
    return craft_plan(villages, productions, allocations, config)


def _route_to(plan, destination):
    return next(r for r in plan.rows if r.destination == destination)


class TestBoundingTheCycleForOneDestination:
    def test_without_a_cap_the_optimizer_picks_a_long_cycle(self):
        # The premise. Nothing here is wrong -- a long cycle is cheaper in
        # merchants, and that is what it was told to minimise.
        assert _route_to(_plan(), 2).cycle_hours > 1

    def test_a_cap_of_one_hour_gives_hourly_departures(self):
        assert _route_to(_plan(cap=1), 2).cycle_hours == 1

    def test_a_cap_of_two_is_honoured_too(self):
        # Equality, not `<=`: "honoured" means the cap is REACHED. A cap read as
        # exclusive answers 1 here, which respects the bound and disobeys the
        # instruction -- and `<=` cannot tell the two apart.
        assert _route_to(_plan(cap=2), 2).cycle_hours == 2

    def test_a_cap_inside_the_range_is_the_cycle_that_comes_back(self):
        """The cap is inclusive, and only a cap strictly between the shortest
        cycle and the uncapped optimum can show it.

        At a cap of 1 an exclusive reading leaves no candidate at all, and the
        empty-list fallback below rescues it straight back to 1 -- so the
        hourly test passes on either reading and launders the off-by-one into
        the right answer."""
        assert _route_to(_plan(), 2).cycle_hours > 3, "the premise: 3h is a real constraint"

        assert _route_to(_plan(cap=3), 2).cycle_hours == 3

    def test_the_hourly_rate_is_unchanged_by_the_cadence(self):
        # The constraint is about WHEN, not how much. A cap that quietly changed
        # the volume would be a different bug wearing this one's clothes.
        loose = _route_to(_plan(), 2)
        tight = _route_to(_plan(cap=1), 2)
        loose_rate = loose.total_cargo / loose.cycle_hours
        tight_rate = tight.total_cargo / tight.cycle_hours
        assert tight_rate == pytest.approx(loose_rate, rel=0.02), (
            "the operator asked for the same volume, delivered more often"
        )

    def test_it_costs_merchants_and_says_so_in_the_plan(self):
        # A 1h cycle over a long round trip keeps many sends in the air. The cost
        # is in merchants COMMITTED, not merchants per send -- a SheetRow's
        # `merchants` is one send's worth, and both cases fit one merchant. What
        # changes is how many of those sends are away at once.
        loose, tight = _plan(), _plan(cap=1)
        assert tight.merchants_committed[1] > loose.merchants_committed[1], (
            f"cadence is bought with merchants: "
            f"{loose.merchants_committed[1]} -> {tight.merchants_committed[1]}"
        )


class TestItOnlyConstrainsTheDestinationItNames:
    def test_another_destination_is_left_free(self):
        villages = {
            1: VillageState(
                village_id=1, x=0, y=0, merchant_count=200, trade_office_level=19, name="src"
            ),
            2: VillageState(village_id=2, x=40, y=0, merchant_count=20, name="capped"),
            3: VillageState(village_id=3, x=0, y=40, merchant_count=20, name="free"),
        }
        productions = {Resource.CROP: {1: 4_000.0, 2: 0.0, 3: 0.0}}
        allocations = {
            Resource.CROP: {
                1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
                2: Allocation(mode=AllocationMode.ABSOLUTE, value=2_000.0),
                3: Allocation(mode=AllocationMode.ABSOLUTE, value=2_000.0),
            }
        }
        config = PlannerConfig(
            geometry=GEOMETRY,
            merchant_model=EUROPE2_TEUTON,
            max_latency_hours=None,
            max_cycle_by_destination={2: 1},
        )
        plan = craft_plan(villages, productions, allocations, config)

        assert _route_to(plan, 2).cycle_hours == 1
        assert _route_to(plan, 3).cycle_hours > 1, "an uncapped destination stays optimised"

    def test_no_caps_at_all_leaves_every_plan_exactly_as_it_was(self):
        assert _route_to(_plan(cap=None), 2).cycle_hours == _route_to(_plan(), 2).cycle_hours


class TestACapThatExcludesEverything:
    """Unreachable through the endpoint -- ``ForeignTarget.max_cycle_hours`` is
    validated into ``DAILY_BEAT_CYCLES``, so 1 always qualifies -- but reachable
    from ``PlannerConfig``, which takes any int. The docstring states the
    contract: a cadence preference is not worth failing a whole plan for, so the
    route keeps the SHORTEST cycle available. Falling back to all of them is the
    opposite of what a cadence constraint means."""

    def test_it_keeps_the_shortest_cycle_not_all_of_them(self):
        assert _cycles_for(2, DAILY_BEAT_CYCLES, {2: 0}) == [1]

    def test_a_cap_that_admits_something_admits_only_that(self):
        assert list(_cycles_for(2, DAILY_BEAT_CYCLES, {2: 3})) == [1, 2, 3]
