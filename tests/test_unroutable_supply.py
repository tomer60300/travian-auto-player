"""Surplus a village cannot ship is not supply.

A route needs merchants at its origin. Travian grants merchants through the
Marketplace, so a village without one -- a freshly settled village, or one whose
Marketplace has not been built yet -- has a real resource surplus and no way to
move any of it.

The optimizer took every surplus village as a candidate sender and asserted much
later that each route origin had merchants, with a comment claiming the invariant
was "guaranteed by construction". It was not: the surplus map is built from
``plan.senders`` with no merchant filter at all, so a zero-merchant village
reached route construction and tripped the assert -- a 500 to the operator
instead of a plan.

The right answer is not to relax the assert. The assert is correct and caught a
real impossibility. What is wrong is upstream: such a village must be excluded
from routable supply, keep its surplus where it is, and the receiver that was
counting on it must be reported as short.
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


def _plan(*, sender_merchants, supply=5000.0):
    villages = {
        1: VillageState(village_id=1, x=0, y=0, merchant_count=sender_merchants, name="new"),
        2: VillageState(village_id=2, x=3, y=0, merchant_count=20, name="hub"),
    }
    productions = {Resource.LUMBER: {1: supply, 2: 0.0}}
    allocations = {
        Resource.LUMBER: {
            1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            2: Allocation(mode=AllocationMode.ABSOLUTE, value=supply),
        }
    }
    config = PlannerConfig(geometry=GEOMETRY, merchant_model=EUROPE2_TEUTON)
    return craft_plan(villages, productions, allocations, config)


class TestAVillageWithNoMerchantsCannotBeASender:
    def test_planning_does_not_crash(self):
        # Previously: AssertionError, "the optimizer built an impossible route".
        plan = _plan(sender_merchants=0)
        assert plan is not None

    def test_it_ships_nothing(self):
        plan = _plan(sender_merchants=0)
        assert [r for r in plan.rows if r.origin == 1] == [], (
            "a village with no merchants cannot originate a route"
        )

    def test_the_receiver_is_reported_short_rather_than_quietly_unfed(self):
        # The demand does not disappear because the supply is unreachable. A plan
        # that simply omitted the route would read as complete.
        plan = _plan(sender_merchants=0)
        short = [s for s in plan.shortfalls if s.village_id == 2]
        assert short, f"shortfalls: {plan.shortfalls}"
        assert short[0].resource is Resource.LUMBER
        assert not plan.is_feasible, "an unmet receiver is not a feasible sheet"

    def test_the_same_account_works_the_moment_merchants_exist(self):
        # The control: nothing else about this account prevents the route, so the
        # exclusion must be about merchants and nothing else.
        plan = _plan(sender_merchants=20)
        assert [r.origin for r in plan.rows] == [1]
        assert plan.shortfalls == ()
        assert plan.is_feasible
