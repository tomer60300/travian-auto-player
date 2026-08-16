"""Storage safety: overflow in one direction, starvation in the other.

Review R7 is the reason this module exists at all: the profile's
``fill_time = (capacity - stock) / net`` silently returns a NEGATIVE number for a
village whose crop is draining, and a negative "hours until full" reads as no
problem while the granary empties and troops starve. Every test here is really
asking the same question -- does the answer stay honest when the village is
heading the other way?
"""

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.optimizer import Route
from travian_api.services.distribution.schedule import build_beat
from travian_api.services.distribution.storage import (
    DEFAULT_WARN_HOURS,
    Trend,
    simulate_day,
    storage_warnings,
    store_status,
)


class TestBothDirections:
    def test_a_filling_store_reports_time_until_it_overflows(self):
        status = store_status(1, Resource.LUMBER, stock=10_000, capacity=80_000, net_per_hour=7_000)

        assert status.trend is Trend.FILLING
        assert status.hours_remaining == pytest.approx(10.0)

    def test_a_draining_store_reports_time_until_it_is_empty(self):
        """R7. The old one-directional formula would give
        (80000 - 12000) / -4000 = -17h, which sorts and displays as 'fine'."""
        status = store_status(1, Resource.CROP, stock=12_000, capacity=80_000, net_per_hour=-4_000)

        assert status.trend is Trend.DRAINING
        assert status.hours_remaining == pytest.approx(3.0)
        assert status.hours_remaining > 0, "a countdown to disaster must never be negative"

    def test_starvation_is_answerable_without_knowing_the_capacity(self):
        """The draining branch needs only the stock, so a village whose capacity
        was never fetched still gets the warning that actually matters."""
        status = store_status(1, Resource.CROP, stock=6_000, capacity=None, net_per_hour=-3_000)

        assert status.trend is Trend.DRAINING
        assert status.hours_remaining == pytest.approx(2.0)

    def test_a_filling_store_without_capacity_says_so_rather_than_guessing(self):
        status = store_status(1, Resource.IRON, stock=6_000, capacity=None, net_per_hour=3_000)

        assert status.trend is Trend.FILLING
        assert status.hours_remaining is None
        assert not status.is_urgent

    def test_a_level_store_is_not_divided_by_its_own_rounding_error(self):
        """A rate of 0.2/h is level, not 'full in 400,000 hours'."""
        status = store_status(1, Resource.CLAY, stock=5_000, capacity=80_000, net_per_hour=0.2)

        assert status.trend is Trend.STEADY
        assert status.hours_remaining is None

    def test_an_already_full_store_reports_zero_not_a_negative(self):
        status = store_status(1, Resource.CLAY, stock=90_000, capacity=80_000, net_per_hour=1_000)

        assert status.hours_remaining == 0.0


class TestWarnings:
    def test_starvation_is_reported_before_overflow(self):
        """Overflow wastes surplus; starvation destroys troops. When both are
        urgent the one that cannot be undone must be read first."""
        overflowing = store_status(1, Resource.LUMBER, 79_000, 80_000, 5_000)
        starving = store_status(2, Resource.CROP, 3_000, 80_000, -3_000)

        warnings = storage_warnings([overflowing, starving], [])

        assert len(warnings) == 2
        assert "runs out" in warnings[0]
        assert "fills its store" in warnings[1]

    def test_a_comfortable_store_is_not_mentioned(self):
        calm = store_status(1, Resource.IRON, 1_000, 800_000, 500)

        assert storage_warnings([calm], []) == ()
        assert (calm.hours_remaining or 0) > DEFAULT_WARN_HOURS


class TestDiscreteArrivals:
    """Known issue #12: the average fits, the batch does not."""

    def _daily_route(self, origin: int, destination: int, per_hour: float) -> Route:
        # A 24h cycle delivers a whole day of production in one lump.
        return Route(
            origin=origin,
            destination=destination,
            cargo_per_hour={Resource.LUMBER: per_hour},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=60.0,
        )

    def test_a_lumpy_batch_overflows_while_the_average_rate_says_steady(self):
        """The exact shape of known issue #12.

        Village 2 burns 1,000/h and receives 1,000/h on average, so on a
        continuous view it is perfectly level -- the rate check reports STEADY
        and raises nothing, forever. But the delivery is a 24h cycle: a whole
        day's worth lands in one lump on a store that is already nearly full,
        and everything past the cap is gone. Averages cannot see this; only
        replaying the actual beat can.
        """
        # The continuous check, on the same numbers, is content.
        averaged = store_status(2, Resource.LUMBER, stock=70_000, capacity=80_000, net_per_hour=0.0)
        assert averaged.trend is Trend.STEADY
        assert storage_warnings([averaged], []) == ()

        beat = build_beat((self._daily_route(1, 2, 1_000),))
        overflows = simulate_day(
            beat,
            stocks={1: {Resource.LUMBER: 0}, 2: {Resource.LUMBER: 70_000}},
            capacities={1: {Resource.LUMBER: 800_000}, 2: {Resource.LUMBER: 80_000}},
            net_per_hour={1: {Resource.LUMBER: 1_000}, 2: {Resource.LUMBER: -1_000}},
        )

        event = next((e for e in overflows if e.village_id == 2), None)
        assert event is not None, "the batch overflows, but the average-rate check passed"
        assert event.resource is Resource.LUMBER
        assert event.wasted_per_day > 0
        # And it is reported in terms the operator can act on.
        assert any("hits the cap" in w for w in storage_warnings([averaged], overflows))

    def test_a_sender_is_not_reported_as_overflowing_its_own_outbound(self):
        """Cargo leaves the origin when the merchants do. Counting only arrivals
        would let every sender's store grow without bound and invent an overflow
        that the outbound route is in fact preventing."""
        beat = build_beat((self._daily_route(1, 2, 1_000),))

        overflows = simulate_day(
            beat,
            stocks={1: {Resource.LUMBER: 70_000}, 2: {Resource.LUMBER: 0}},
            capacities={1: {Resource.LUMBER: 80_000}, 2: {Resource.LUMBER: 800_000}},
            net_per_hour={1: {Resource.LUMBER: 1_000}, 2: {Resource.LUMBER: 0}},
        )

        assert not [e for e in overflows if e.village_id == 1], (
            "the sender ships its production out daily; it must not read as overflowing"
        )

    def test_a_store_with_no_known_capacity_is_skipped_not_assumed(self):
        beat = build_beat((self._daily_route(1, 2, 1_000),))

        overflows = simulate_day(
            beat,
            stocks={2: {Resource.LUMBER: 79_000}},
            capacities={},  # capacity page was never fetched
            net_per_hour={2: {Resource.LUMBER: 5_000}},
        )

        assert overflows == ()
