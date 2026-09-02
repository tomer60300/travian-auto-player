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
from travian_api.services.distribution.findings import Category, Severity, summarise
from travian_api.services.distribution.optimizer import Route
from travian_api.services.distribution.schedule import build_beat
from travian_api.services.distribution.storage import (
    DEFAULT_WARN_HOURS,
    OverflowEvent,
    Trend,
    simulate_day,
    storage_findings,
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

    def test_a_batch_bigger_than_the_store_overflows_every_single_day(self):
        """The exact shape of known issue #12, in its RECURRING form.

        Village 2 burns 5,000/h and receives 5,000/h on average, so on a
        continuous view it is perfectly level -- the rate check reports STEADY
        and raises nothing, forever. But the delivery is a 24h cycle, so a whole
        day's worth (120,000) lands in one lump on a store that only holds
        80,000. At least 40,000 is lost, and it is lost again tomorrow, and the
        day after: the batch does not fit no matter what the village started
        with. Averages cannot see this; only replaying the beat can.
        """
        # The continuous check, on the same numbers, is content.
        averaged = store_status(2, Resource.LUMBER, stock=40_000, capacity=80_000, net_per_hour=0.0)
        assert averaged.trend is Trend.STEADY
        assert storage_warnings([averaged], []) == ()

        beat = build_beat((self._daily_route(1, 2, 5_000),))
        overflows = simulate_day(
            beat,
            stocks={1: {Resource.LUMBER: 120_000}, 2: {Resource.LUMBER: 40_000}},
            capacities={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: 80_000}},
            net_per_hour={1: {Resource.LUMBER: 5_000}, 2: {Resource.LUMBER: -5_000}},
        )

        event = next((e for e in overflows if e.village_id == 2), None)
        assert event is not None, "a 120,000 batch cannot fit an 80,000 store, ever"
        assert event.resource is Resource.LUMBER
        assert event.wasted_per_day >= 40_000
        assert any("hits the cap" in w for w in storage_warnings([averaged], overflows))

    def test_a_store_that_is_merely_full_today_is_not_called_a_daily_loss(self):
        """A one-off transient must not be reported as a recurring rate.

        A village sitting near its cap loses one batch, settles, and never loses
        anything again -- the beat is fine, the village just needs draining
        today. Reporting that as 'loses N per day' overstates it indefinitely,
        and it is already covered by the continuous fill-time check. Only waste
        that survives to a settled day belongs here.
        """
        beat = build_beat((self._daily_route(1, 2, 1_000),))

        overflows = simulate_day(
            beat,
            # 24,000 lands daily and 24,000 is consumed daily: the batch fits the
            # 80,000 store comfortably. Only the 70,000 opening stock makes day
            # one overflow.
            stocks={1: {Resource.LUMBER: 24_000}, 2: {Resource.LUMBER: 70_000}},
            capacities={1: {Resource.LUMBER: 800_000}, 2: {Resource.LUMBER: 80_000}},
            net_per_hour={1: {Resource.LUMBER: 1_000}, 2: {Resource.LUMBER: -1_000}},
        )

        assert not [e for e in overflows if e.village_id == 2], (
            "day-one transient reported as a recurring daily loss"
        )

    def test_a_delivery_the_origin_cannot_fund_is_not_invented(self):
        """Cargo must be conserved. If the origin has nothing to ship, taking
        the shortfall out at zero while still crediting the destination a full
        batch creates resources -- and the invented cargo then reappears as an
        overflow at the far end."""
        beat = build_beat((self._daily_route(1, 2, 1_000),))

        overflows = simulate_day(
            beat,
            # Village 1 has no stock and no production: it can ship nothing.
            stocks={1: {Resource.LUMBER: 0}, 2: {Resource.LUMBER: 60_000}},
            capacities={1: {Resource.LUMBER: 800_000}, 2: {Resource.LUMBER: 80_000}},
            net_per_hour={1: {Resource.LUMBER: 0}, 2: {Resource.LUMBER: 0}},
        )

        assert overflows == (), (
            "nothing was ever shipped, so nothing can overflow at the destination"
        )

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


class TestWhyItOverflowed:
    """The reason given in the warning has to be the actual reason.

    Every overflow line said "an arriving batch overflows even though the average
    rate fits". On the account that motivated this work, all 52 of them were
    wrong: the average did not fit in a single one of those villages. The two
    failures have opposite fixes -- shorten the cycle, versus give the surplus
    somewhere to go -- so telling them apart is not cosmetic.
    """

    def _daily_route(self, origin: int, destination: int, per_hour: float) -> Route:
        return Route(
            origin=origin,
            destination=destination,
            cargo_per_hour={Resource.LUMBER: per_hour},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=60.0,
        )

    def _full_warehouse(self) -> tuple[OverflowEvent, ...]:
        """A village that keeps everything it makes, already at its cap."""
        return simulate_day(
            build_beat(()),
            stocks={1: {Resource.CLAY: 799_000}},
            capacities={1: {Resource.CLAY: 800_000}},
            net_per_hour={1: {Resource.CLAY: 926}},
        )

    def test_a_surplus_with_nowhere_to_go_is_structural(self):
        """Such a village loses its whole production every day, forever, and no
        schedule can change that."""
        overflows = self._full_warehouse()

        assert len(overflows) == 1
        event = overflows[0]
        assert event.structural, "926/h into a full store is not a batch problem"
        assert event.net_gain_per_day == pytest.approx(926 * 24)
        assert event.wasted_per_day == pytest.approx(926 * 24)

    def test_a_batch_too_big_for_the_store_is_not_structural(self):
        """Issue #12's own case: the average fits exactly, the lump does not."""
        overflows = simulate_day(
            build_beat((self._daily_route(1, 2, 5_000),)),
            stocks={1: {Resource.LUMBER: 120_000}, 2: {Resource.LUMBER: 40_000}},
            capacities={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: 80_000}},
            net_per_hour={1: {Resource.LUMBER: 5_000}, 2: {Resource.LUMBER: -5_000}},
        )

        event = next(e for e in overflows if e.village_id == 2)
        assert not event.structural, "120,000 in and 120,000 out is a batch problem"
        assert event.net_gain_per_day == pytest.approx(0.0, abs=1.0)

    def test_a_structural_overflow_is_not_dated_to_a_minute(self):
        """A store that never leaves its cap is already there at 00:00, so the
        clock was an artifact of the grid rather than the time of any event --
        and it sent the operator looking for a midnight arrival."""
        status = store_status(1, Resource.CLAY, 799_000, 800_000, 926)

        warnings = storage_warnings([status], self._full_warehouse(), names={1: "19"})

        overflow_line = next(w for w in warnings if "hits the cap" in w)
        assert "00:00" not in overflow_line
        assert "never leaves its cap" in overflow_line
        assert "average rate fits" not in overflow_line

    def test_a_store_reported_as_overflowing_is_not_also_reported_as_filling(self):
        """The same store, twice, from two angles. On the real account this was
        half the warning list: 51 "fills its store in 1.1h" lines describing the
        same 51 stores the overflow lines had already priced."""
        status = store_status(1, Resource.CLAY, 799_000, 800_000, 926)
        assert status.is_urgent, "the fixture must trip the continuous check too"

        warnings = storage_warnings([status], self._full_warehouse(), names={1: "19"})

        assert len(warnings) == 1
        assert "hits the cap" in warnings[0]

    def test_a_store_filling_without_an_overflow_is_still_reported(self):
        """The dedupe must not swallow the fill-time note in general -- only
        where the overflow line has already said the cap is reached."""
        status = store_status(1, Resource.CLAY, 700_000, 800_000, 20_000)

        warnings = storage_warnings([status], [], names={1: "19"})

        assert len(warnings) == 1
        assert "fills its store" in warnings[0]

    def test_the_two_kinds_of_overflow_are_different_findings(self):
        """Grouped together they would be handed one action, and one of the two
        halves would be told to do the wrong thing."""
        structural = storage_findings([], self._full_warehouse(), names={1: "19"})
        burst = storage_findings(
            [],
            simulate_day(
                build_beat((self._daily_route(1, 2, 5_000),)),
                stocks={1: {Resource.LUMBER: 120_000}, 2: {Resource.LUMBER: 40_000}},
                capacities={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: 80_000}},
                net_per_hour={1: {Resource.LUMBER: 5_000}, 2: {Resource.LUMBER: -5_000}},
            ),
            names={2: "02"},
        )

        assert structural[0].category is Category.OVERFLOW_STRUCTURAL
        assert burst[0].category is Category.OVERFLOW_BURST
        assert structural[0].action != burst[0].action
        assert structural[0].loss_per_day > 0, "the cost is carried, not just described"


class TestBeyondTheSettlingHorizon:
    """A store the horizon never saw settle is still heading somewhere.

    simulate_day gives up after MAX_SETTLING_DAYS and used to report whatever
    the last day happened to clamp: nothing at all for a store that reaches its
    cap the day after, half a day's loss for one that got there at noon on the
    last day. But a store gaining more each day than it passes on WILL sit at
    its cap, and once there everything beyond what leaves is lost -- so its
    recurring loss is its net gain, whether or not the replay ran long enough
    to watch it happen.
    """

    def _lone_store(self, stock: int, capacity: int, per_hour: float) -> tuple[OverflowEvent, ...]:
        return simulate_day(
            build_beat(()),
            stocks={1: {Resource.CROP: stock}},
            capacities={1: {Resource.CROP: capacity}},
            net_per_hour={1: {Resource.CROP: per_hour}},
        )

    def test_a_store_that_fills_after_the_horizon_is_reported_at_its_net_gain(self):
        # +1,000/h into a 400,000 store holding 40,000: full on day 15, one day
        # past the horizon, and losing 24,000/day every day after that.
        events = self._lone_store(40_000, 400_000, 1_000.0)

        assert len(events) == 1, "a store certain to sit at its cap reported no overflow"
        event = events[0]
        assert event.projected is True
        assert event.net_gain_per_day == pytest.approx(24_000.0, abs=1.0)
        assert event.wasted_per_day == pytest.approx(event.net_gain_per_day)
        assert event.structural, "a surplus with nowhere to go is not a batch problem"
        assert event.days_to_cap == pytest.approx(15.0, abs=0.1)

    def test_a_store_that_first_clamps_inside_the_last_day_reports_the_whole_recurring_loss(
        self,
    ):
        # Full at noon on day 14, so the last day clamped for only half of it.
        events = self._lone_store(76_000, 400_000, 1_000.0)

        assert len(events) == 1
        assert events[0].projected is True
        assert events[0].wasted_per_day == pytest.approx(24_000.0, abs=1.0), (
            "half a day's clamping was reported as the daily loss"
        )

    def test_a_settled_overflow_is_observed_not_projected(self):
        # 926/h into a store 1,000 short of its cap: at the cap inside the first
        # day, and the second day repeats the first. Exactly the event the replay
        # reported before there was any projection.
        events = self._lone_store(799_000, 800_000, 926.0)

        assert len(events) == 1
        event = events[0]
        assert event.projected is False
        # None, not 0.0: `projected` is now derived from this field's absence,
        # so an observed event has no countdown rather than a zero-length one
        # that would have printed as "in about 0 days".
        assert event.days_to_cap is None
        assert event.minute == 0
        assert event.wasted_per_day == pytest.approx(926 * 24)
        assert event.net_gain_per_day == pytest.approx(926 * 24)

    def test_a_draining_store_is_not_projected_however_long_the_run(self):
        # Village 2 empties 24,000/day out of 500,000 and never settles, so the
        # whole account is still drifting at the horizon; village 1 is the
        # day-15 store above, which proves the projection path did run.
        events = simulate_day(
            build_beat(()),
            stocks={1: {Resource.CROP: 40_000}, 2: {Resource.CROP: 500_000}},
            capacities={1: {Resource.CROP: 400_000}, 2: {Resource.CROP: 800_000}},
            net_per_hour={1: {Resource.CROP: 1_000.0}, 2: {Resource.CROP: -1_000.0}},
        )

        assert [e.village_id for e in events] == [1], (
            "a store that is emptying can never sit at its cap"
        )

    def test_a_store_a_month_or_more_from_its_cap_is_not_a_plan_defect(self):
        """Rates change before then. A store 133 days from its cap was reported
        as a CRITICAL structural loss; nothing about this plan is wrong with it."""
        # +500/h into a 400,000 store: 12,000/day, so 28,000 in store is 31
        # days from the cap and 52,000 is 29.
        assert self._lone_store(28_000, 400_000, 500.0) == ()

    def test_a_store_inside_thirty_days_of_its_cap_is_still_projected(self):
        events = self._lone_store(52_000, 400_000, 500.0)

        assert len(events) == 1
        assert events[0].projected is True
        assert events[0].days_to_cap == pytest.approx(29.0, abs=0.1)

    def test_a_store_exactly_at_the_horizon_is_not_dropped_by_float_residue(self):
        """+500/h out of 40,000 into 400,000 lands on 30.000000000000004 days.

        The bound is a month, chosen because the account has changed by then --
        so a store the report would call "about 30 days" and a store one float
        step past 30 are the same store. Compared after rounding, which is also
        how the message states it, so the number admitted and the number printed
        cannot disagree.
        """
        events = self._lone_store(40_000, 400_000, 500.0)

        assert len(events) == 1, "a store 30.000000000000004 days out was dropped"
        assert events[0].days_to_cap == pytest.approx(30.0, abs=0.01)

    def test_a_projected_overflow_says_when_and_how_much(self):
        findings = storage_findings([], self._lone_store(40_000, 400_000, 1_000.0), names={1: "19"})

        assert len(findings) == 1
        assert "will reach its cap in about 15 days" in findings[0].message
        assert "24,000/day" in findings[0].message
        assert "00:00" not in findings[0].message, (
            "a store that has not reached its cap has no minute of the day"
        )


class TestAProjectedLossIsNotAPresentOne:
    """A store 20 days from its cap was reported as a CRITICAL structural
    overflow and its future loss was summed into "This account loses N
    resources a day at its store caps" -- present tense, about something that
    has not started. The 30-day horizon fixed how far ahead the planner would
    look and not the tense it reported in, so one quiet village 20 days out
    produced a red headline claiming a 24,000/day loss that was not happening.
    """

    def _projected(self) -> tuple[OverflowEvent, ...]:
        # +1,000/h into a 400,000 store holding 40,000: full on day 15.
        return simulate_day(
            build_beat(()),
            stocks={1: {Resource.CROP: 40_000}},
            capacities={1: {Resource.CROP: 400_000}},
            net_per_hour={1: {Resource.CROP: 1_000.0}},
        )

    def test_it_is_a_warning_in_its_own_category(self):
        findings = storage_findings([], self._projected(), names={1: "19"})

        assert len(findings) == 1
        assert findings[0].category is Category.OVERFLOW_PROJECTED
        assert findings[0].severity is Severity.WARNING, (
            "a loss that has not started cannot be a reason to refuse the plan"
        )

    def test_it_costs_nothing_per_day_yet(self):
        findings = storage_findings([], self._projected(), names={1: "19"})

        assert findings[0].loss_per_day == 0.0, (
            "the future loss was counted as a present one; the account's daily "
            "total then describes a day that has not happened"
        )
        assert "24,000" in findings[0].detail, (
            "the figure still has to be visible -- excluded from the total is "
            "not the same as hidden"
        )

    def test_the_account_headline_does_not_claim_the_loss_is_happening(self):
        diagnostics = summarise(list(storage_findings([], self._projected(), names={1: "19"})))

        assert diagnostics.total_loss_per_day == 0.0
        assert "loses" not in diagnostics.headline, diagnostics.headline
        assert "Nothing is being wasted" in diagnostics.headline, diagnostics.headline

    def test_an_observed_overflow_still_counts_in_full(self):
        """The distinction is projected-vs-watched, not overflow-vs-nothing."""
        observed = simulate_day(
            build_beat(()),
            stocks={1: {Resource.CROP: 799_000}},
            capacities={1: {Resource.CROP: 800_000}},
            net_per_hour={1: {Resource.CROP: 926.0}},
        )

        findings = storage_findings([], observed, names={1: "19"})

        assert findings[0].category is Category.OVERFLOW_STRUCTURAL
        assert findings[0].loss_per_day == pytest.approx(926 * 24)
        assert summarise(list(findings)).total_loss_per_day == pytest.approx(926 * 24)


class TestStockFloors:
    """A store the operator keeps topped up by NPC never runs dry in the replay.

    Without a floor the simulation is right to ship only what the origin holds:
    cargo is conserved. But a village that NPCs its crop surplus into lumber
    whenever the warehouse dips below 30% does hold that 30%, every hour, so a
    departure from it always finds its full batch. Modelling it as an ordinary
    store made every stock-funded route ship a fraction of its cargo and the
    receivers' whole day understated.
    """

    def _daily_route(self, origin: int, destination: int, per_hour: float) -> Route:
        return Route(
            origin=origin,
            destination=destination,
            cargo_per_hour={Resource.LUMBER: per_hour},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=60.0,
        )

    def _replay(self, **kwargs) -> tuple[OverflowEvent, ...]:
        # Village 1 makes 24,000/day and is asked to ship 120,000/day -- five
        # times its production -- out of a 40,000 stock. Village 2 burns exactly
        # the 120,000 a day it would receive, but its store only holds 80,000,
        # so a FULL batch overflows it by 40,000 (issue #12's shape) while a
        # batch the origin could not fund does not.
        return simulate_day(
            build_beat((self._daily_route(1, 2, 5_000),)),
            stocks={1: {Resource.LUMBER: 40_000}, 2: {Resource.LUMBER: 40_000}},
            capacities={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: 80_000}},
            net_per_hour={1: {Resource.LUMBER: 1_000}, 2: {Resource.LUMBER: -5_000}},
            **kwargs,
        )

    def test_an_unfloored_origin_still_ships_only_what_it_holds(self):
        """Cargo stays conserved for every store that has no floor: the origin
        runs down to its 24,000/day and the receiver never sees a full batch."""
        overflows = self._replay()

        assert not [e for e in overflows if e.village_id == 2], (
            "the origin cannot fund 120,000/day, so no full batch ever lands"
        )

    def test_a_floored_origin_ships_its_full_batch_every_day(self):
        overflows = self._replay(floors={1: {Resource.LUMBER: 40_000}})

        event = next((e for e in overflows if e.village_id == 2), None)
        assert event is not None, "a floored origin must fund the whole 120,000 batch"
        # Exactly one full batch's excess over the store, on the settled day:
        # the origin was topped back to its floor before every departure.
        assert event.wasted_per_day == pytest.approx(40_000, abs=1.0)

    def test_the_floor_applies_only_to_the_store_that_has_one(self):
        """A floor on the receiver's own store must not fund the origin."""
        overflows = self._replay(floors={2: {Resource.LUMBER: 40_000}})

        assert not [e for e in overflows if e.village_id == 2]
