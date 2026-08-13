"""Merchant capacity model and route cost.

The V10 -> V02 case is the operator's own hand-computed route from
docs/25-resource-distribution-planner.md. It is the golden case because it was
produced independently of this code and exhibits the non-monotonicity that makes
the cycle choice non-obvious.
"""

import pytest

from travian_api.services.distribution.merchants import (
    ALL_CYCLES,
    DAILY_BEAT_CYCLES,
    STOCK_TEUTON,
    CalibrationError,
    CapacityObservation,
    MerchantModel,
    calibrate,
    cheapest_cycle,
    cycle_sweep,
    route_cost,
)

# V10 -> V02: 9,323/h of cargo, 532 min round trip, 5,720 per merchant.
V10_CARGO = 9323
V10_ROUND_TRIP = 532.0
V10_CAPACITY = 5720


class TestCapacity:
    def test_capacity_scales_with_trade_office_level(self):
        model = MerchantModel(1000, 0.10, 12.0)

        assert model.capacity(0) == 1000
        assert model.capacity(10) == 2000
        assert model.capacity(20) == 3000

    def test_capacity_rounds_down(self):
        """Understating capacity over-provisions (safe); overstating breaches
        the merchant budget invisibly (unsafe). Only round the safe way."""
        model = MerchantModel(1000, 0.13, 12.0)

        assert model.capacity(1) == 1130
        assert model.capacity(3) == 1390  # 1390.0000000000002 must not become 1391

    def test_negative_trade_office_level_is_rejected(self):
        with pytest.raises(ValueError):
            STOCK_TEUTON.capacity(-1)


class TestCalibration:
    """Resolves R1 without trusting any published constant."""

    def test_two_observations_recover_base_and_bonus(self):
        model = calibrate(
            [CapacityObservation(0, 1000), CapacityObservation(10, 2000)],
            speed_fields_per_hour=12.0,
        )

        assert model.base_capacity == 1000
        assert model.bonus_per_trade_office_level == pytest.approx(0.10)

    def test_calibration_works_without_a_zero_level_sample(self):
        """The two villages to hand may both have a Trade Office."""
        model = calibrate(
            [CapacityObservation(4, 1400), CapacityObservation(11, 2100)],
            speed_fields_per_hour=12.0,
        )

        assert model.base_capacity == 1000
        assert model.bonus_per_trade_office_level == pytest.approx(0.10)

    def test_calibration_distinguishes_the_two_disputed_models(self):
        """The whole point of R1: which model the account actually follows.

        A TO-11 village reading 2,100 is base 1000 / +10%. Reading 7,040 it is
        base 2200 / +20%. One observation pair settles it.
        """
        teuton = calibrate([CapacityObservation(0, 1000), CapacityObservation(11, 2100)], 12.0)
        doc_model = calibrate([CapacityObservation(0, 2200), CapacityObservation(11, 7040)], 12.0)

        assert teuton.capacity(11) == 2100
        assert doc_model.capacity(11) == 7040
        # A 3.35x difference in how many merchants every route needs.
        assert doc_model.capacity(11) / teuton.capacity(11) == pytest.approx(3.35, abs=0.01)

    def test_inconsistent_observations_raise_rather_than_average(self):
        """Per-village variation means a Trade artifact, not a fitting problem."""
        with pytest.raises(CalibrationError, match="Trade artifact"):
            calibrate(
                [
                    CapacityObservation(0, 1000),
                    CapacityObservation(10, 2000),
                    CapacityObservation(5, 3000),  # doubled: artifact village
                ],
                speed_fields_per_hour=12.0,
            )

    def test_single_trade_office_level_cannot_solve_two_unknowns(self):
        with pytest.raises(CalibrationError, match="two different"):
            calibrate([CapacityObservation(5, 1500), CapacityObservation(5, 1500)], 12.0)

    def test_one_observation_is_rejected(self):
        with pytest.raises(CalibrationError, match="at least two"):
            calibrate([CapacityObservation(0, 1000)], 12.0)


class TestRouteCost:
    def test_reproduces_the_hand_computed_route(self):
        """Independently derived by the operator; must reproduce exactly."""
        expected = {
            1: (2, 9, 18),
            2: (4, 5, 20),
            3: (5, 3, 15),
            4: (7, 3, 21),
        }

        for cycle, (send, sets, pool) in expected.items():
            cost = route_cost(V10_CARGO, cycle, V10_ROUND_TRIP, V10_CAPACITY)
            assert (cost.merchants_per_send, cost.sets_in_flight) == (send, sets), cycle
            assert cost.merchants_committed == pool, cycle

    def test_merchant_cost_is_not_monotonic_in_cycle_length(self):
        """Why the optimizer must sweep instead of hill-climbing."""
        pools = [
            route_cost(V10_CARGO, cycle, V10_ROUND_TRIP, V10_CAPACITY).merchants_committed
            for cycle in (1, 2, 3, 4)
        ]

        assert pools == [18, 20, 15, 21]
        assert pools[1] > pools[0] and pools[2] < pools[1] and pools[3] > pools[2]

    def test_sets_in_flight_at_an_exact_multiple_of_the_cycle(self):
        """Round trip 360 min on a 3h cycle: the first set lands exactly as the
        third send is due, so two sets suffice."""
        cost = route_cost(1000, 3, 360.0, 10_000)

        assert cost.sets_in_flight == 2

    def test_a_trip_shorter_than_the_cycle_needs_one_set(self):
        cost = route_cost(1000, 4, 90.0, 10_000)

        assert cost.sets_in_flight == 1

    def test_zero_cargo_commits_no_merchants(self):
        cost = route_cost(0, 3, V10_ROUND_TRIP, V10_CAPACITY)

        assert cost.merchants_committed == 0

    @pytest.mark.parametrize(
        "cargo,cycle,capacity",
        [(100, 0, 1000), (100, -1, 1000), (100, 3, 0), (-1, 3, 1000)],
    )
    def test_invalid_inputs_are_rejected(self, cargo, cycle, capacity):
        with pytest.raises(ValueError):
            route_cost(cargo, cycle, 100.0, capacity)


class TestCycleSelection:
    def test_picks_the_cheapest_cycle(self):
        best = cheapest_cycle(V10_CARGO, V10_ROUND_TRIP, V10_CAPACITY)

        assert best.cycle_hours == 3
        assert best.merchants_committed == 15

    def test_default_cycles_all_divide_a_day(self):
        """R5: a schedule is only expressible as a daily beat if they do."""
        assert all(24 % cycle == 0 for cycle in DAILY_BEAT_CYCLES)

    def test_the_wider_sweep_is_available_to_price_the_restriction(self):
        """The daily-beat restriction must be measurable, not hidden."""
        restricted = cheapest_cycle(V10_CARGO, V10_ROUND_TRIP, V10_CAPACITY)
        unrestricted = cheapest_cycle(V10_CARGO, V10_ROUND_TRIP, V10_CAPACITY, ALL_CYCLES)

        assert unrestricted.merchants_committed <= restricted.merchants_committed

    def test_ties_resolve_to_the_shorter_cycle(self):
        """Equal merchants, sooner delivery -- objective 2."""
        costs = cycle_sweep(1, 60.0, 10_000, cycles=(1, 2, 3))
        assert {c.merchants_committed for c in costs} == {1}

        assert cheapest_cycle(1, 60.0, 10_000, cycles=(3, 2, 1)).cycle_hours == 1

    def test_empty_cycle_set_is_rejected(self):
        with pytest.raises(ValueError):
            cheapest_cycle(V10_CARGO, V10_ROUND_TRIP, V10_CAPACITY, cycles=())
