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
