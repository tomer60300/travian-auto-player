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


def _craft(villages, productions, allocations, *, banned):
    """Plan *villages*, with the crop obligation at 9 banned to *banned* or open.

    The three fixtures below differ only in their villages and their numbers,
    and this tail was copied out three times -- including the clause that
    encodes what "no exclusion" means. `{9: set(banned)} if banned else {}` is
    the whole contract that `None` and `[]` are both "unrestricted", and three
    copies is three places to get it wrong.
    """
    config = PlannerConfig(
        geometry=GEOMETRY,
        merchant_model=EUROPE2_TEUTON,
        max_latency_hours=None,
        excluded_origins_by_destination={9: set(banned)} if banned else {},
    )
    return craft_plan(villages, productions, allocations, config)


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
    return _craft(villages, productions, allocations, banned=banned)


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


def _crosswise_plan(*, banned=None):
    """Two obligations laid out so a 2x2 swap is available and worth taking.

    The fixture above has ONE destination, and a 2x2 swap needs two flows with
    DIFFERENT destinations -- which is why every test above passes while the
    exclusion is being violated. Here `hub` sits one field from `tribute` and
    `spare` one field from `other`, so the cheap pairing is hub->tribute.
    Banning `hub` from `tribute` forces the seed to cross the two hauls at ~100
    fields each, and uncrossing them is a large improvement the search will take
    unless something stops it naming a forbidden origin.
    """
    villages = {
        1: VillageState(village_id=1, x=0, y=0, merchant_count=40, name="hub"),
        2: VillageState(village_id=2, x=100, y=0, merchant_count=40, name="spare"),
        8: VillageState(village_id=8, x=101, y=0, merchant_count=40, name="other"),
        9: VillageState(village_id=9, x=1, y=0, merchant_count=40, name="tribute"),
    }
    productions = {Resource.CROP: {1: 5_000.0, 2: 6_000.0, 8: 0.0, 9: 0.0}}
    allocations = {
        Resource.CROP: {
            1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            2: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            8: Allocation(mode=AllocationMode.ABSOLUTE, value=5_000.0),
            9: Allocation(mode=AllocationMode.ABSOLUTE, value=6_000.0),
        }
    }
    return _craft(villages, productions, allocations, banned=banned)


def _relay_plan(*, banned=None):
    """A haul the sender cannot staff, with the banned village next door.

    `supplier` has 4 merchants and a 200-field obligation it cannot cover, so
    the search wants to move that commitment somewhere. `hub` is one field away
    with 40 merchants, which makes relaying through it a strict improvement on
    the over-budget key. The relayed second leg is `hub -> tribute` -- a pair the
    exclusion forbids, and one the seed never had the chance to veto because
    relay invents it.
    """
    villages = {
        1: VillageState(
            village_id=1, x=1, y=0, merchant_count=40, name="hub", crop_per_hour=1_000.0
        ),
        2: VillageState(
            village_id=2, x=0, y=0, merchant_count=4, name="supplier", crop_per_hour=1_000.0
        ),
        8: VillageState(village_id=8, x=2, y=0, merchant_count=40, name="near", crop_per_hour=0.0),
        9: VillageState(
            village_id=9, x=200, y=0, merchant_count=40, name="tribute", crop_per_hour=0.0
        ),
    }
    productions = {Resource.CROP: {1: 300.0, 2: 500.0, 8: 0.0, 9: 0.0}}
    allocations = {
        Resource.CROP: {
            1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            2: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            8: Allocation(mode=AllocationMode.ABSOLUTE, value=300.0),
            9: Allocation(mode=AllocationMode.ABSOLUTE, value=500.0),
        }
    }
    return _craft(villages, productions, allocations, banned=banned)


class TestTheImprovementSearchCannotUndoTheBan:
    """The ban is applied when the greedy seed picks senders. It was never
    applied to the moves that run AFTERWARDS.

    Found 2026-09-02 on the operator's live account: village 02 was excluded
    from the foreign tribute, and the night plan emitted a direct 02 -> tribute
    route carrying 10,266 crop/h on 9 merchants anyway. Turning the improvement
    search off removed it; turning it on put it back. The exclusion was also
    actively counterproductive -- with it ON the plan drew from six origins
    including the banned one, and with it OFF from four that did not include it.
    """

    def test_a_swap_may_not_reintroduce_a_banned_origin(self):
        assert 1 not in _origins_to(_crosswise_plan(banned=[1]), 9)

    def test_the_crosswise_fixture_really_does_tempt_the_search(self):
        # Guards the fixture: if the unbanned plan did not pair hub->tribute,
        # the test above would pass while tempting nothing.
        assert 1 in _origins_to(_crosswise_plan(), 9), "fixture no longer creates the temptation"

    def test_a_relay_may_not_make_a_banned_origin_the_forwarding_hub(self):
        assert 1 not in _origins_to(_relay_plan(banned=[1]), 9)

    def test_the_relay_fixture_really_does_route_through_the_hub(self):
        assert 1 in _origins_to(_relay_plan(), 9), "fixture no longer creates the temptation"

    def test_the_obligation_is_still_met_from_a_permitted_origin(self):
        # The fix must refuse the forbidden move, not abandon the delivery.
        plan = _crosswise_plan(banned=[1])
        assert _origins_to(plan, 9) == [2]
        assert not plan.shortfalls, "the demand is coverable without the banned origin"
