"""Allocation resolution.

Known issue #1 — shipping the target instead of the gap — is the error this
module exists to prevent, so it gets the most direct tests.
"""

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationError,
    AllocationMode,
    Resource,
    resolve_resource,
)

KEEP = Allocation(AllocationMode.KEEP)


def pct(value: float) -> Allocation:
    return Allocation(AllocationMode.PERCENTAGE, value)


class TestShipVersusTarget:
    def test_ship_is_the_gap_not_the_target(self):
        """Known issue #1. A village producing 400 that should hold 1,000
        receives 600 -- not 1,000."""
        plan = resolve_resource(
            Resource.IRON,
            productions={1: 400, 2: 600},
            allocations={1: Allocation(AllocationMode.ABSOLUTE, 1000), 2: KEEP},
        )
        village = next(v for v in plan.villages if v.village_id == 1)

        assert village.target_per_hour == 1000
        assert village.ship_per_hour == 600

    def test_a_village_keeping_exactly_what_it_makes_ships_nothing(self):
        plan = resolve_resource(
            Resource.LUMBER,
            productions={1: 500},
            allocations={1: Allocation(AllocationMode.ABSOLUTE, 500)},
        )

        assert plan.villages[0].ship_per_hour == 0
        assert not plan.villages[0].is_receiver
        assert not plan.villages[0].is_sender

    def test_a_village_below_its_own_production_becomes_a_sender(self):
        plan = resolve_resource(
            Resource.CLAY,
            productions={1: 900},
            allocations={1: Allocation(AllocationMode.ABSOLUTE, 200)},
        )

        assert plan.villages[0].ship_per_hour == -700
        assert plan.villages[0].is_sender


class TestConservation:
    def test_shipping_nets_to_zero_with_a_remainder_village(self):
        """Nothing is created or destroyed in transit."""
        plan = resolve_resource(
            Resource.IRON,
            productions={1: 1000, 2: 2000, 3: 3000},
            allocations={
                1: pct(50),
                2: Allocation(AllocationMode.ABSOLUTE, 500),
                3: Allocation(AllocationMode.REMAINDER),
            },
        )

        assert plan.is_conserved
        assert sum(v.ship_per_hour for v in plan.receivers) == pytest.approx(
            -sum(v.ship_per_hour for v in plan.senders)
        )

    def test_remainder_absorbs_the_slack_from_partial_percentages(self):
        """Known issue #9: percentages summing to 96% left 4% unassigned."""
        plan = resolve_resource(
            Resource.LUMBER,
            productions={1: 5000, 2: 3000, 3: 2000},
            allocations={1: pct(60), 2: pct(36), 3: Allocation(AllocationMode.REMAINDER)},
        )
        remainder = next(v for v in plan.villages if v.village_id == 3)

        assert plan.unallocated == pytest.approx(400)  # 4% of 10,000
        assert remainder.target_per_hour == pytest.approx(400)
        assert plan.is_conserved


class TestRemainderRules:
    def test_two_remainder_villages_are_rejected(self):
        with pytest.raises(AllocationError, match="exactly one remainder"):
            resolve_resource(
                Resource.CROP,
                productions={1: 100, 2: 100},
                allocations={
                    1: Allocation(AllocationMode.REMAINDER),
                    2: Allocation(AllocationMode.REMAINDER),
                },
            )

    def test_unassigned_slack_without_a_remainder_warns(self):
        plan = resolve_resource(
            Resource.LUMBER,
            productions={1: 5000, 2: 5000},
            allocations={1: pct(60), 2: pct(36)},
        )

        assert not plan.is_conserved
        assert any("unallocated" in w for w in plan.warnings)

    def test_over_allocation_warns_about_the_remainder_going_negative(self):
        """No single percentage may exceed 100, but a set of them can."""
        plan = resolve_resource(
            Resource.IRON,
            productions={1: 1000, 2: 1000, 3: 1000},
            allocations={1: pct(80), 2: pct(60), 3: Allocation(AllocationMode.REMAINDER)},
        )

        assert plan.unallocated == pytest.approx(-1200)  # 140% of 3,000
        assert any("exceed production" in w for w in plan.warnings)


class TestSustainMode:
    def test_sustain_covers_the_deficit_plus_headroom(self):
        """Real figures: village 03 drains 5,556 crop/h. At 13% headroom it
        must receive 6,278/h and ends up 722/h positive."""
        plan = resolve_resource(
            Resource.CROP,
            productions={20003: -5556, 2: 20000},
            allocations={
                20003: Allocation(AllocationMode.SUSTAIN, 13),
                2: Allocation(AllocationMode.REMAINDER),
            },
        )
        army = next(v for v in plan.villages if v.village_id == 20003)

        assert army.ship_per_hour == pytest.approx(6278.28)
        assert army.target_per_hour == pytest.approx(722.28)
        assert plan.is_conserved

    def test_sustain_on_a_healthy_village_ships_nothing_and_warns(self):
        """Sustain is a floor, not a top-up; a positive village needs no crop."""
        plan = resolve_resource(
            Resource.CROP,
            productions={1: 4000},
            allocations={1: Allocation(AllocationMode.SUSTAIN, 13)},
        )

        assert plan.villages[0].ship_per_hour == 0
        assert any("nothing to sustain" in w for w in plan.warnings)

    def test_negative_headroom_is_rejected(self):
        with pytest.raises(AllocationError):
            Allocation(AllocationMode.SUSTAIN, -5)


class TestDefaultsAndValidation:
    def test_unlisted_villages_keep_their_own_production(self):
        plan = resolve_resource(Resource.IRON, productions={1: 700, 2: 300}, allocations={})

        assert all(v.ship_per_hour == 0 for v in plan.villages)
        assert plan.is_conserved

    def test_allocation_for_an_unknown_village_is_rejected(self):
        with pytest.raises(AllocationError, match="no production"):
            resolve_resource(Resource.IRON, productions={1: 100}, allocations={99: pct(50)})

    @pytest.mark.parametrize("value", [-1, 101])
    def test_percentage_outside_range_is_rejected(self, value):
        with pytest.raises(AllocationError):
            Allocation(AllocationMode.PERCENTAGE, value)

    def test_negative_absolute_retention_is_rejected(self):
        """-4,000/h means 'retain less than nothing': the sender is handed a
        route it cannot fund and `unallocated` exceeds the whole account."""
        with pytest.raises(AllocationError, match="absolute retention cannot be negative"):
            Allocation(AllocationMode.ABSOLUTE, -4000)

    def test_percentage_against_a_crop_negative_account_is_rejected(self):
        """30% of a net -4,000/h account is a target of -1,200 — an instruction
        to ship crop out of a starving village. A warning is not enough: the
        resolved routes are wrong, not merely noisy, so the plan must not
        resolve at all."""
        with pytest.raises(AllocationError, match="negative"):
            resolve_resource(
                Resource.CROP,
                productions={1: -5000, 2: 1000},
                allocations={2: pct(30)},
            )

    def test_negative_total_production_still_resolves(self):
        """An account can be crop-negative overall; the planner must not break."""
        plan = resolve_resource(
            Resource.CROP,
            productions={1: -5000, 2: 1000},
            allocations={2: Allocation(AllocationMode.REMAINDER)},
        )

        assert plan.total_production == -4000
        assert plan.is_conserved


class TestStockFundedSupply:
    """A village may ship more than it makes, drawing on a warehouse stock it
    keeps topped up by NPC trading.

    The operator's day profile was refused as over-allocated -- "the remainder
    village would have to send more than it has" -- while 02 sat on a warehouse
    it keeps 30% full precisely so that it CAN send more than it makes. The
    supplement is extra SUPPLY for one resource, never production: every figure
    that says "production" must keep meaning production.
    """

    def _plan(self, supplement=None):
        return resolve_resource(
            Resource.LUMBER,
            productions={1: 1000, 2: 0},
            allocations={
                1: Allocation(AllocationMode.REMAINDER),
                2: Allocation(AllocationMode.ABSOLUTE, 1200),
            },
            supplement=supplement,
        )

    def test_a_supplement_raises_what_the_village_has_available(self):
        plan = self._plan(supplement={1: 500})
        hub = next(v for v in plan.villages if v.village_id == 1)

        assert hub.own_per_hour == 1000
        assert hub.supplement_per_hour == 500
        assert hub.available_per_hour == 1500

    def test_ship_is_the_gap_to_available_so_the_village_can_ship_more_than_it_makes(self):
        """Known issue #1 in its new form: the cargo is target minus what the
        village has, and what it has now includes the stock it draws on."""
        plan = self._plan(supplement={1: 500})
        hub = next(v for v in plan.villages if v.village_id == 1)

        assert hub.target_per_hour == pytest.approx(300)  # 1,500 available - 1,200 claimed
        assert hub.ship_per_hour == pytest.approx(-1200), "ships 200/h more than it produces"
        assert plan.is_conserved

    def test_a_negative_remainder_becomes_non_negative(self):
        """The exact complaint: 1,200/h claimed against 1,000/h produced."""
        without = self._plan()
        assert without.unallocated == pytest.approx(-200)
        assert any("exceed production" in w for w in without.warnings)

        with_stock = self._plan(supplement={1: 500})

        assert with_stock.unallocated == pytest.approx(300)
        assert not any("exceed production" in w for w in with_stock.warnings)

    def test_total_production_stays_real_and_the_supplement_is_carried_apart(self):
        plan = self._plan(supplement={1: 500})

        assert plan.total_production == 1000, "production must not be inflated by stock"
        assert plan.total_supplement == 500

    @pytest.mark.parametrize("supplement", [None, {}, {1: 0.0}])
    def test_no_supplement_leaves_every_figure_identical(self, supplement):
        """Regression guard: the whole existing planner runs with no supplement."""
        baseline = self._plan()
        plan = self._plan(supplement=supplement)

        assert plan.total_production == baseline.total_production
        assert plan.total_supplement == 0
        assert plan.unallocated == baseline.unallocated
        assert plan.warnings == baseline.warnings
        assert [
            (v.village_id, v.own_per_hour, v.target_per_hour, v.ship_per_hour)
            for v in plan.villages
        ] == [
            (v.village_id, v.own_per_hour, v.target_per_hour, v.ship_per_hour)
            for v in baseline.villages
        ]

    def test_a_keep_village_with_a_supplement_still_ships_nothing(self):
        """Keep means neither send nor receive. The allowance must not turn
        into an instruction to ship the stock away to the remainder."""
        plan = resolve_resource(
            Resource.CLAY,
            productions={1: 1000, 2: 500},
            allocations={2: Allocation(AllocationMode.REMAINDER)},
            supplement={1: 400},
        )
        kept = next(v for v in plan.villages if v.village_id == 1)

        assert kept.ship_per_hour == 0
        assert plan.is_conserved

    def test_a_supplement_for_an_unknown_village_is_rejected(self):
        with pytest.raises(AllocationError, match="no production"):
            resolve_resource(
                Resource.IRON,
                productions={1: 100},
                allocations={},
                supplement={99: 500},
            )
