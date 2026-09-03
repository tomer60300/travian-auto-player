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
from travian_api.services.distribution.findings import Category, Severity

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


class TestNpcFundedShipping:
    """A village may ship more than it makes, by converting at the NPC merchant.

    The operator's day profile was refused as over-allocated -- "the remainder
    village would have to send more than it has" -- while 02 keeps its warehouse
    stocked with wood precisely so that it CAN send more than it makes.

    **RE-SEEDED from the previous model, which two of these tests pinned.** It
    passed ``own + allowance`` in as the village's "available" supply, so the
    allowance was an ADDEND: 1,000/h produced with a 500/h allowance read as
    1,500/h available, the remainder target came out at 1,500 - 1,200 = 300,
    and the village shipped 1,200/h -- 200/h more than it produced -- whether or
    not anything needed it. Here the allowance is a CAP consumed against unmet
    demand only, so the same fixture draws exactly the 200/h it is short by,
    the remainder target lands on 0, and a fixture with nothing short draws
    nothing at all.
    """

    def _plan(self, npc_allowance=None):
        """1,200/h claimed against 1,000/h produced: 200/h of unmet demand."""
        return resolve_resource(
            Resource.LUMBER,
            productions={1: 1000, 2: 0},
            allocations={
                1: Allocation(AllocationMode.REMAINDER),
                2: Allocation(AllocationMode.ABSOLUTE, 1200),
            },
            npc_allowance=npc_allowance,
        )

    def test_the_allowance_is_a_ceiling_and_the_draw_is_what_was_needed(self):
        plan = self._plan(npc_allowance={1: 500})
        hub = next(v for v in plan.villages if v.village_id == 1)

        assert hub.own_per_hour == 1000
        assert hub.npc_allowance_per_hour == 500, "the ceiling the operator declared"
        assert hub.npc_draw_per_hour == pytest.approx(200), "only the shortfall is converted"

    def test_ship_is_target_minus_own_minus_draw(self):
        """Known issue #1 in its new form: the cargo is the gap, and conversion
        closes part of it rather than adding to what the village has."""
        plan = self._plan(npc_allowance={1: 500})
        hub = next(v for v in plan.villages if v.village_id == 1)

        # 1,000 produced + 200 converted - 1,200 claimed = a target of 0.
        assert hub.target_per_hour == pytest.approx(0.0)
        assert hub.ship_per_hour == pytest.approx(-1200), "ships 200/h more than it produces"
        assert plan.is_conserved

    def test_a_negative_remainder_becomes_non_negative(self):
        """The exact complaint: 1,200/h claimed against 1,000/h produced."""
        without = self._plan()
        assert without.unallocated == pytest.approx(-200)
        assert any("exceed production" in w for w in without.warnings)

        with_npc = self._plan(npc_allowance={1: 500})

        assert with_npc.unallocated == pytest.approx(0.0)
        assert not any("exceed production" in w for w in with_npc.warnings)

    def test_total_production_stays_real_and_the_conversion_is_carried_apart(self):
        plan = self._plan(npc_allowance={1: 500})

        assert plan.total_production == 1000, "production must not be inflated by conversion"
        assert plan.total_npc_allowance == 500
        assert plan.total_npc_draw == pytest.approx(200)

    @pytest.mark.parametrize("npc_allowance", [None, {}, {1: 0.0}])
    def test_no_allowance_leaves_every_figure_identical(self, npc_allowance):
        """Regression guard: the whole existing planner runs with no conversion,
        and a declared allowance of 0.0 is the same as none at all."""
        baseline = self._plan()
        plan = self._plan(npc_allowance=npc_allowance)

        assert plan.total_production == baseline.total_production
        assert plan.total_npc_draw == 0
        assert plan.unallocated == baseline.unallocated
        assert [
            (v.village_id, v.own_per_hour, v.target_per_hour, v.ship_per_hour)
            for v in plan.villages
        ] == [
            (v.village_id, v.own_per_hour, v.target_per_hour, v.ship_per_hour)
            for v in baseline.villages
        ]

    def test_an_allowance_on_a_quiet_village_costs_nothing(self):
        """The compulsory-supply bug, pinned. Nothing is short here -- 1,000/h
        against a 600/h claim -- so a 400/h allowance must be drawn at zero and
        every figure must match a plan that declared no allowance at all."""
        quiet = resolve_resource(
            Resource.LUMBER,
            productions={1: 1000, 2: 0},
            allocations={
                1: Allocation(AllocationMode.REMAINDER),
                2: Allocation(AllocationMode.ABSOLUTE, 600),
            },
            npc_allowance={1: 400},
        )
        baseline = resolve_resource(
            Resource.LUMBER,
            productions={1: 1000, 2: 0},
            allocations={
                1: Allocation(AllocationMode.REMAINDER),
                2: Allocation(AllocationMode.ABSOLUTE, 600),
            },
        )
        hub = next(v for v in quiet.villages if v.village_id == 1)

        assert hub.npc_allowance_per_hour == 400
        assert hub.npc_draw_per_hour == 0.0
        assert quiet.total_npc_draw == 0.0
        assert quiet.unallocated == baseline.unallocated == pytest.approx(400)
        assert [(v.village_id, v.ship_per_hour) for v in quiet.villages] == [
            (v.village_id, v.ship_per_hour) for v in baseline.villages
        ]
        assert quiet.findings == baseline.findings

    def test_a_keep_village_with_an_allowance_still_ships_nothing(self):
        """Keep means neither send nor receive. The allowance must not turn
        into an instruction to convert resources and ship them away."""
        plan = resolve_resource(
            Resource.CLAY,
            productions={1: 1000, 2: 500},
            allocations={2: Allocation(AllocationMode.REMAINDER)},
            npc_allowance={1: 400},
        )
        kept = next(v for v in plan.villages if v.village_id == 1)

        assert kept.target_per_hour == 1000, "KEEP retains production, not production plus stock"
        assert kept.ship_per_hour == 0
        assert kept.npc_draw_per_hour == 0.0
        assert plan.is_conserved

    def test_a_quiet_sustain_village_with_an_allowance_still_ships_nothing(self):
        """The other mode `_explicit_target` had to stop reading as available.
        Sustain with non-negative production retains production, so a floor
        cannot turn it into a sender."""
        plan = resolve_resource(
            Resource.CLAY,
            productions={1: 1000, 2: 500},
            allocations={
                1: Allocation(AllocationMode.SUSTAIN, 10),
                2: Allocation(AllocationMode.REMAINDER),
            },
            npc_allowance={1: 400},
        )
        quiet = next(v for v in plan.villages if v.village_id == 1)

        assert quiet.target_per_hour == 1000
        assert quiet.ship_per_hour == 0
        assert quiet.npc_draw_per_hour == 0.0

    def test_a_draw_beyond_the_allowance_is_reported_critical(self):
        """Fail loudly: 200/h needed, 50/h of feedstock. The plan still emits
        what the conversion CAN fund, and says it is 150/h short."""
        plan = self._plan(npc_allowance={1: 50})

        short = [f for f in plan.findings if f.category is Category.NPC_CAPACITY_SHORT]
        assert len(short) == 1, plan.warnings
        assert short[0].severity is Severity.CRITICAL
        assert "150" in short[0].message
        assert plan.unallocated == pytest.approx(-150)

    def test_an_allowance_for_an_unknown_village_is_rejected(self):
        with pytest.raises(AllocationError, match="no production"):
            resolve_resource(
                Resource.IRON,
                productions={1: 100},
                allocations={},
                npc_allowance={99: 500},
            )

    def test_a_negative_allowance_is_rejected(self):
        with pytest.raises(AllocationError, match="cannot be negative"):
            resolve_resource(
                Resource.IRON,
                productions={1: 100},
                allocations={},
                npc_allowance={1: -500},
            )


class TestConsumption:
    """What a village SPENDS, kept apart from what it must hold.

    The two were one number, and the storage layer read the surviving one as
    permanent accumulation: an army village told to hold 14,751 lumber an hour
    was modelled as stockpiling all of it, so the day plan reported
    354,024/day (= 14,751 x 24) "lost at the store cap" for a village that in
    fact spends every unit. Consumption is the second number. It never touches
    the cargo -- known issue #1 stands, ``ship_per_hour`` is still the gap --
    and it never touches production.
    """

    def _plan(self, consumption=None, target=1200.0):
        return resolve_resource(
            Resource.LUMBER,
            productions={1: 1000, 2: 500},
            allocations={
                1: Allocation(AllocationMode.ABSOLUTE, target),
                2: Allocation(AllocationMode.REMAINDER),
            },
            consumption=consumption,
        )

    def test_net_is_the_target_less_what_the_village_spends(self):
        plan = self._plan(consumption={1: 800})
        army = next(v for v in plan.villages if v.village_id == 1)

        assert army.target_per_hour == 1200
        assert army.consumption_per_hour == 800
        assert army.net_per_hour == pytest.approx(400)

    def test_a_village_spending_exactly_its_target_holds_level(self):
        """The operator's intent: 01 is told to LAND 14,751/h because it BURNS
        14,751/h. Its store neither grows nor shrinks."""
        plan = self._plan(consumption={1: 1200})
        army = next(v for v in plan.villages if v.village_id == 1)

        assert army.net_per_hour == pytest.approx(0.0)

    def test_spending_more_than_it_lands_drains_the_store(self):
        plan = self._plan(consumption={1: 2000})
        army = next(v for v in plan.villages if v.village_id == 1)

        assert army.net_per_hour == pytest.approx(-800)

    def test_the_cargo_does_not_move(self):
        """Spec 2.2 is already right and must stay right: shipped = target
        minus own production. Consumption changes the STORE, not the sheet."""
        baseline = self._plan()
        consuming = self._plan(consumption={1: 1200})

        army = next(v for v in consuming.villages if v.village_id == 1)
        was = next(v for v in baseline.villages if v.village_id == 1)

        assert army.ship_per_hour == was.ship_per_hour == pytest.approx(200)
        assert [v.ship_per_hour for v in consuming.villages] == [
            v.ship_per_hour for v in baseline.villages
        ]
        assert consuming.is_conserved

    def test_production_and_the_remainder_are_untouched(self):
        """Consumption is not a claim on the account pool. The unallocated
        figure answers what the targets left over, and spending a resource is a
        different question from allocating it."""
        baseline = self._plan()
        consuming = self._plan(consumption={1: 1200, 2: 400})

        assert consuming.total_production == baseline.total_production
        assert consuming.unallocated == baseline.unallocated
        assert consuming.warnings == baseline.warnings

    @pytest.mark.parametrize("consumption", [None, {}, {1: 0.0}])
    def test_no_consumption_leaves_every_figure_identical(self, consumption):
        """Regression guard: the whole existing planner runs with none."""
        baseline = self._plan()
        plan = self._plan(consumption=consumption)

        assert [
            (v.village_id, v.own_per_hour, v.target_per_hour, v.ship_per_hour, v.net_per_hour)
            for v in plan.villages
        ] == [
            (v.village_id, v.own_per_hour, v.target_per_hour, v.ship_per_hour, v.net_per_hour)
            for v in baseline.villages
        ]

    def test_with_no_consumption_net_is_the_target(self):
        """The pre-consumption meaning, which is what the storage layer read."""
        army = next(v for v in self._plan().villages if v.village_id == 1)

        assert army.net_per_hour == pytest.approx(army.target_per_hour)

    def test_negative_consumption_is_rejected(self):
        """A village cannot spend a negative amount, and reading one as extra
        production is exactly the inference this design refused to make: the
        statistics page reports materials gross, so a consuming village reads
        positive and there is no signal to invert."""
        with pytest.raises(AllocationError, match="consumption cannot be negative"):
            self._plan(consumption={1: -500})

    def test_consumption_for_an_unknown_village_is_rejected(self):
        with pytest.raises(AllocationError, match="no production"):
            resolve_resource(
                Resource.IRON,
                productions={1: 100},
                allocations={},
                consumption={99: 500},
            )

    def test_consumption_composes_with_an_npc_draw(self):
        """An NPC allowance funds what the SENDER ships; consumption lowers what
        the receiver's store keeps. Both at once must not collide: the draw
        belongs to the sender, so the receiver's net stays target minus spend."""
        plan = resolve_resource(
            Resource.LUMBER,
            productions={1: 1000, 2: 0},
            allocations={
                1: Allocation(AllocationMode.REMAINDER),
                2: Allocation(AllocationMode.ABSOLUTE, 1200),
            },
            npc_allowance={1: 500},
            consumption={2: 1200},
        )
        receiver = next(v for v in plan.villages if v.village_id == 2)
        sender = next(v for v in plan.villages if v.village_id == 1)

        assert receiver.ship_per_hour == pytest.approx(1200)
        assert receiver.net_per_hour == pytest.approx(0.0)
        assert sender.npc_draw_per_hour == pytest.approx(200)
