"""Who may supply a target, when cadence makes distance expensive.

A hourly cycle costs one merchant per send in flight, and the number in flight is
the round trip in hours. So a supplier 8.5 hours away commits nine merchants to
that route -- however little it carries. Nine merchants to move 3,930 crop an hour
is the worst use of a fleet on the account, and it is what the optimizer chose:
correctly, by its own measure, because it was minimising merchants across the
whole plan and never told that this destination had a cadence.

The optimizer cannot be left to work this out. It has no way to know that the
operator would rather draw the last few thousand from a nearer village at a worse
rate than spend nine merchants reaching for it. That is a judgement about the
account, so it belongs to the operator: `exclude_origins` on the target.

Deliberately a denylist and not a distance rule. Any threshold would be arbitrary,
and the villages worth excluding are the ones the operator knows are needed
elsewhere -- which no distance can tell you.
"""

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
)
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import VillageState
from travian_api.services.distribution.planner import PlannerConfig, craft_plan

GEOMETRY = MapGeometry(span=401, speed_fields_per_hour=12.0)


def _plan(*, banned=None):
    """Two villages with spare crop, one target. `2` is far, `3` is near."""
    villages = {
        1: VillageState(village_id=1, x=0, y=0, merchant_count=20, name="near"),
        2: VillageState(village_id=2, x=90, y=0, merchant_count=20, name="far"),
        9: VillageState(village_id=9, x=5, y=0, merchant_count=20, name="target"),
    }
    productions = {Resource.CROP: {1: 3_000.0, 2: 9_000.0, 9: 0.0}}
    allocations = {
        Resource.CROP: {
            1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            2: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            9: Allocation(mode=AllocationMode.ABSOLUTE, value=12_000.0),
        }
    }
    config = PlannerConfig(
        geometry=GEOMETRY,
        merchant_model=EUROPE2_TEUTON,
        max_latency_hours=None,
        excluded_origins_by_destination={9: set(banned)} if banned else {},
    )
    return craft_plan(villages, productions, allocations, config)


def _origins_to(plan, destination):
    return sorted(r.origin for r in plan.rows if r.destination == destination)


class TestExcludingASupplierFromOneDestination:
    def test_both_villages_supply_it_by_default(self):
        assert _origins_to(_plan(), 9) == [1, 2]

    def test_a_banned_origin_ships_nothing_there(self):
        assert _origins_to(_plan(banned=[2]), 9) == [1]

    def test_the_ban_is_per_destination_not_global(self):
        # The excluded village keeps its own surplus rather than being frozen out
        # of the plan: it simply is not used for THIS obligation.
        plan = _plan(banned=[2])
        assert all(r.destination == 9 for r in plan.rows), "only one destination exists here"
        assert 2 not in _origins_to(plan, 9)

    def test_banning_every_supplier_leaves_the_demand_reported_short(self):
        # Refusing to plan is right; pretending it is covered is not.
        plan = _plan(banned=[1, 2])
        assert _origins_to(plan, 9) == []
        assert [s.village_id for s in plan.shortfalls] == [9]
        assert not plan.is_feasible

    def test_no_exclusions_leaves_the_plan_exactly_as_it_was(self):
        assert _origins_to(_plan(banned=None), 9) == _origins_to(_plan(), 9)
