"""Storage safety: overflow in one direction, starvation in the other.

Review R7 is the reason this module exists at all: the profile's
``fill_time = (capacity - stock) / net`` silently returns a NEGATIVE number for a
village whose crop is draining, and a negative "hours until full" reads as no
problem while the granary empties and troops starve. Every test here is really
asking the same question -- does the answer stay honest when the village is
heading the other way?
"""

import inspect
from pathlib import Path

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import Category, Severity, summarise
from travian_api.services.distribution.npc import NpcReserve
from travian_api.services.distribution.optimizer import Route
from travian_api.services.distribution.schedule import build_beat
from travian_api.services.distribution.storage import (
    DEFAULT_WARN_HOURS,
    MAX_SETTLING_DAYS,
    OverflowEvent,
    ProfileSegment,
    Trend,
    _accrue,
    _npc_top_up,
    simulate_day,
    simulate_profile_cycle,
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

    def test_a_rate_that_empties_a_granary_in_a_day_is_not_level(self):
        """The other side of the negligible-rate threshold, which nothing else
        pins: 0.2/h is rounding error, 400/h is a granary gone by tomorrow. Read
        as STEADY it would carry no hours of cover and could never be urgent."""
        status = store_status(1, Resource.CROP, stock=4_000, capacity=None, net_per_hour=-400.0)

        assert status.trend is Trend.DRAINING
        assert status.hours_remaining == pytest.approx(10.0)
        assert status.is_urgent

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

    def test_exactly_the_warn_horizon_of_cover_is_not_yet_urgent(self):
        """The boundary the threshold names. Eighteen hours is the warning's
        span, not a point inside it, so a store with exactly that much cover has
        not yet crossed anything."""
        on_the_hour = store_status(1, Resource.CROP, 18_000, None, -1_000)
        one_short = store_status(1, Resource.CROP, 17_999, None, -1_000)

        assert on_the_hour.hours_remaining == pytest.approx(DEFAULT_WARN_HOURS)
        assert not on_the_hour.is_urgent
        assert one_short.is_urgent


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


class TestNpcReserves:
    """A store the operator NPCs back up funds its departures -- as far as the
    conversion goes, and no further.

    Without a reserve the simulation is right to ship only what the origin
    holds: cargo is conserved. A village that converts its crop surplus into
    lumber does hold more than it produces, so a departure from it finds more
    than the store made -- but only what the conversion could actually fund.

    **RE-SEEDED from the previous model, which two of these tests pinned.** It
    took ``floors`` alone and did ``level = max(floor, level - batch)``, an
    INFINITE reservoir: the origin below funded the whole 120,000/day batch off
    24,000/day of production and 96,000/day of nothing at all. Now the
    conversion is a finite budget out of a named feedstock store, so the same
    120,000/day is funded only when 96,000/day of feedstock is there to fund it
    -- and the arithmetic is written down in each case.
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

    def _reserve(self, vid: int, allowance_per_day: float) -> NpcReserve:
        return NpcReserve(
            village_id=vid,
            floor_level=40_000,
            allowance_per_day=allowance_per_day,
            sources=(Resource.CROP,) if allowance_per_day else (),
            shares=(1.0,) if allowance_per_day else (),
            drawn=frozenset({Resource.LUMBER}) if allowance_per_day else frozenset(),
        )

    def _replay(
        self,
        *,
        crop_stock: float = 200_000,
        crop_capacity: float = 1_000_000,
        crop_per_hour: float = 4_000,
        **kwargs,
    ) -> tuple[OverflowEvent, ...]:
        # Village 1 makes 24,000 lumber/day (1,000/h) and is asked to ship
        # 120,000/day -- five times its production -- out of a 40,000 stock. The
        # 96,000/day gap is exactly what its 4,000/h of crop can convert into,
        # 1:1, which is the whole of section 7's mechanism written as a fixture.
        # Village 2 burns exactly the 120,000 a day it would receive, but its
        # store only holds 80,000, so a FULL batch overflows it by 40,000
        # (issue #12's shape) while a batch the origin could not fund does not.
        return simulate_day(
            build_beat((self._daily_route(1, 2, 5_000),)),
            stocks={
                1: {Resource.LUMBER: 40_000, Resource.CROP: crop_stock},
                2: {Resource.LUMBER: 40_000},
            },
            capacities={
                1: {Resource.LUMBER: 1_000_000, Resource.CROP: crop_capacity},
                2: {Resource.LUMBER: 80_000},
            },
            net_per_hour={
                1: {Resource.LUMBER: 1_000, Resource.CROP: crop_per_hour},
                2: {Resource.LUMBER: -5_000},
            },
            **kwargs,
        )

    def test_an_origin_with_no_reserve_ships_only_what_it_holds(self):
        """Cargo stays conserved for every store with no conversion behind it:
        the origin runs down to its 24,000/day and the receiver never sees a
        full batch."""
        overflows = self._replay()

        assert not [e for e in overflows if e.village_id == 2], (
            "the origin cannot fund 120,000/day, so no full batch ever lands"
        )

    def test_a_funded_reserve_ships_the_full_batch_every_day(self):
        """96,000/day of allowance against a 96,000/day gap, out of a granary
        producing exactly that: the batch is funded and the receiver overflows
        by exactly one batch's excess over its 80,000 store."""
        overflows = self._replay(npc={1: self._reserve(1, allowance_per_day=96_000)})

        event = next((e for e in overflows if e.village_id == 2), None)
        assert event is not None, "a funded reserve must cover the whole 120,000 batch"
        assert event.wasted_per_day == pytest.approx(40_000, abs=1.0)

    def test_the_reservoir_is_finite_and_exhausts(self):
        """The infinite-reservoir bug, pinned. Same 96,000/day gap and a granary
        with a million crop in it, but only 10,000/day of conversion behind it:
        the batch cannot be funded, so no full batch ever lands."""
        overflows = self._replay(
            crop_stock=1_000_000, npc={1: self._reserve(1, allowance_per_day=10_000)}
        )

        assert not [e for e in overflows if e.village_id == 2], (
            "a 10,000/day allowance cannot fund a 96,000/day gap"
        )

    def test_an_empty_feedstock_store_funds_nothing(self):
        """NPC is an exchange: the allowance is a rate, but there still has to be
        crop in the granary to convert. 96,000/day of allowance against a
        granary that is empty and grows nothing funds nothing at all."""
        overflows = self._replay(
            crop_stock=0,
            crop_per_hour=0,
            npc={1: self._reserve(1, allowance_per_day=96_000)},
        )

        assert not [e for e in overflows if e.village_id == 2]

    def test_the_feedstock_store_is_really_debited(self):
        """The half that makes the 700,000 crop trigger honest: crop converted
        into wood is crop the granary no longer banks.

        The granary here holds 100,000 and gains 96,000/day, so undebited it
        tops out and sheds every one of them. Paying for the conversion is the
        only thing that can keep it level -- so the crop overflow disappearing
        IS the debit, measured rather than asserted."""
        undebited = self._replay(crop_stock=50_000, crop_capacity=100_000)
        debited = self._replay(
            crop_stock=50_000,
            crop_capacity=100_000,
            npc={1: self._reserve(1, allowance_per_day=96_000)},
        )

        shed = next(e for e in undebited if e.village_id == 1 and e.resource is Resource.CROP)
        assert shed.wasted_per_day == pytest.approx(96_000, abs=1.0)
        assert not [e for e in debited if e.village_id == 1 and e.resource is Resource.CROP], (
            "the conversion must take the crop out of the granary, not leave it to shed"
        )
        assert any(e.village_id == 2 for e in debited), "and it did fund the batch"

    def test_a_reserve_applies_only_to_the_village_that_has_one(self):
        """A reserve on the receiver must not fund the origin."""
        overflows = self._replay(npc={2: self._reserve(2, allowance_per_day=96_000)})

        assert not [e for e in overflows if e.village_id == 2]


class TestTheBudgetIsCappedAtOneDaysAllowance:
    """The infinite reservoir, one level up from where it was fixed.

    ``_npc_top_up``'s budget bound is what makes the reservoir finite *within* a
    day; ``_accrue``'s cap is what makes the budget mean "one day's allowance"
    rather than "everything since the replay started". Both replays run to a
    steady state over as many as ``MAX_SETTLING_DAYS`` days, so an uncapped
    accrual is fourteen days of conversion available to fund a single departure.

    Pinned on the accrual itself rather than through a replay, deliberately: at
    a steady state the daily accrual and the daily draw balance, so the *stock*
    of budget cancels out of every periodic fixture. The cap is a property of
    the accrual, and this is where it is observable.
    """

    def _reserve(self, allowance_per_day: float) -> NpcReserve:
        return NpcReserve(
            village_id=1,
            floor_level=40_000,
            allowance_per_day=allowance_per_day,
            sources=(Resource.CROP,),
            shares=(1.0,),
            drawn=frozenset({Resource.LUMBER}),
        )

    def test_a_budget_nobody_spends_stops_at_one_days_allowance(self):
        budget: dict[int, float] = {}

        for _ in range(MAX_SETTLING_DAYS):
            _accrue({1: self._reserve(96_000)}, budget, 24.0)

        assert budget[1] == pytest.approx(96_000), (
            "fourteen settling days must not leave fourteen days of conversion in hand"
        )

    def test_a_part_day_accrues_at_the_rate(self):
        """The control: the cap is a ceiling on an accrual, not the accrual."""
        budget: dict[int, float] = {}

        _accrue({1: self._reserve(96_000)}, budget, 6.0)

        assert budget[1] == pytest.approx(24_000)


class TestTheFloorOnAFeedstockStoreIsKept:
    """Section 7's buffer level, seen from the paying side.

    Every reserve elsewhere in this file converts out of CROP, where the floor
    is deliberately ``0`` -- a granary has no NPC-fed buffer. The branch that
    differs is a MATERIAL feedstock: the operator's declared floor on lumber is
    the thing the whole mechanism exists to protect, and a conversion that spent
    it would empty the store by the machinery meant to defend it.
    """

    def _reserve(self) -> NpcReserve:
        return NpcReserve(
            village_id=1,
            floor_level=40_000,
            allowance_per_day=96_000,
            sources=(Resource.IRON,),
            shares=(1.0,),
            drawn=frozenset({Resource.LUMBER}),
        )

    def test_a_store_sitting_exactly_on_its_floor_funds_nothing(self):
        budget = {1: 96_000.0}

        funded, paid = _npc_top_up(
            self._reserve(),
            Resource.LUMBER,
            {(1, Resource.IRON): 40_000.0},
            budget,
            shortfall=50_000.0,
        )

        assert funded == 0.0
        assert paid == {}
        assert budget[1] == 96_000.0, "an unfunded top-up spends none of the budget either"

    def test_only_what_stands_above_the_floor_is_spendable(self):
        budget = {1: 96_000.0}

        funded, paid = _npc_top_up(
            self._reserve(),
            Resource.LUMBER,
            {(1, Resource.IRON): 45_000.0},
            budget,
            shortfall=50_000.0,
        )

        assert funded == pytest.approx(5_000.0)
        assert paid == {Resource.IRON: pytest.approx(5_000.0)}, (
            "the iron store pays 5,000 and closes on its 40,000 floor, never below"
        )


class TestConsumptionIsNotAccumulation:
    """A village's target is what must LAND; what it SPENDS is a second number.

    Until they were told apart the replay read the surviving one as permanent
    accumulation, so an army village told to land 5,000/h of lumber -- because
    it burns 5,000/h -- was modelled as banking 120,000 a day and reported as
    losing all of it at the warehouse cap. The loss was arithmetic, not a fact
    about the account: it was always exactly ``target x 24``.

    ``consumption`` is per village per resource, subtracted from that village's
    own production before the beat is replayed. Absent, every figure here is
    the pre-consumption one.
    """

    CAP = 80_000

    def _hourly_route(self, per_hour: float) -> Route:
        # An hourly cycle so nothing here is a BURST overflow: the batch is one
        # hour of flow, which is small against the cap. Only the average is
        # under test.
        return Route(
            origin=1,
            destination=2,
            cargo_per_hour={Resource.LUMBER: per_hour},
            cycle_hours=1,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=30.0,
        )

    def _replay(self, *, landing: float, consumption: float | None, stock: int = 40_000):
        beat = build_beat((self._hourly_route(landing),))
        return simulate_day(
            beat,
            stocks={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: stock}},
            capacities={1: {Resource.LUMBER: 10_000_000}, 2: {Resource.LUMBER: self.CAP}},
            # The receiver produces nothing of its own; everything it holds
            # arrived, and everything it spends is the consumption figure.
            net_per_hour={1: {Resource.LUMBER: landing}, 2: {Resource.LUMBER: 0.0}},
            consumption=None if consumption is None else {2: {Resource.LUMBER: consumption}},
        )

    def test_without_consumption_the_whole_target_reads_as_a_daily_loss(self):
        """The artifact this feature exists to remove, reproduced first."""
        events = self._replay(landing=5_000, consumption=None)
        receiver = next(e for e in events if e.village_id == 2)

        assert receiver.structural
        assert receiver.wasted_per_day == pytest.approx(5_000 * 24, abs=1.0)

    def test_a_village_spending_exactly_what_lands_reports_nothing(self):
        """Target equals consumption: the store is level, so there is no
        overflow to report -- and none is reported, without any check being
        weakened."""
        events = self._replay(landing=5_000, consumption=5_000)

        assert [e for e in events if e.village_id == 2] == []

    def test_landing_above_consumption_overflows_at_the_difference(self):
        """The surplus is real and must still be reported -- at the rate of the
        surplus, not of the target."""
        events = self._replay(landing=5_000, consumption=3_000)
        receiver = next(e for e in events if e.village_id == 2)

        assert receiver.structural
        assert receiver.wasted_per_day == pytest.approx((5_000 - 3_000) * 24, abs=1.0)
        assert receiver.net_gain_per_day == pytest.approx((5_000 - 3_000) * 24, abs=1.0)

    def test_spending_more_than_lands_empties_the_store_instead(self):
        """Section 9: 01 is permanently crop-negative by design. A store that
        drains is not an overflow, and inventing one would be the same error in
        the opposite direction."""
        events = self._replay(landing=5_000, consumption=9_000)

        assert [e for e in events if e.village_id == 2] == []

    @pytest.mark.parametrize("consumption", [None, {}, {2: {}}, {2: {Resource.LUMBER: 0.0}}])
    def test_no_consumption_leaves_the_replay_identical(self, consumption):
        beat = build_beat((self._hourly_route(5_000),))
        args = dict(
            stocks={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: 40_000}},
            capacities={1: {Resource.LUMBER: 10_000_000}, 2: {Resource.LUMBER: self.CAP}},
            net_per_hour={1: {Resource.LUMBER: 5_000}, 2: {Resource.LUMBER: 0.0}},
        )

        assert simulate_day(beat, **args, consumption=consumption) == simulate_day(beat, **args)


class TestConsumptionAcrossTheProfileDay:
    """The composite replay needs the same second number.

    Both replays, not one: a previous feature was threaded into ``simulate_day``
    and not into ``simulate_profile_cycle``, so /plan and /day-check answered the
    same account differently.
    """

    def _segments(self, per_hour: float) -> list[ProfileSegment]:
        route = Route(
            origin=1,
            destination=2,
            cargo_per_hour={Resource.CROP: per_hour},
            cycle_hours=1,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=30.0,
        )
        beat = build_beat((route,))
        return [ProfileSegment(name="All day", start_minute=0, end_minute=1439, routes=beat.routes)]

    def _run(self, *, own: float, consumption: float | None, stock: int = 200_000):
        return simulate_profile_cycle(
            self._segments(4_000),
            own_rates={1: {Resource.CROP: 10_000.0}, 2: {Resource.CROP: own}},
            stocks={1: {Resource.CROP: 2_000_000}, 2: {Resource.CROP: stock}},
            capacities={1: {Resource.CROP: 5_000_000}, 2: {Resource.CROP: 400_000}},
            consumption=None if consumption is None else {2: {Resource.CROP: consumption}},
            step_minutes=5,
            max_days=6,
        )

    def test_the_daily_net_drops_by_a_day_of_consumption(self):
        without, _ = self._run(own=1_000, consumption=None)
        with_spend, _ = self._run(own=1_000, consumption=2_000)

        was = next(t for t in without if t.village_id == 2)
        now = next(t for t in with_spend if t.village_id == 2)

        assert now.daily_net == pytest.approx(was.daily_net - 2_000 * 24, abs=50.0)

    def test_a_producing_village_that_overspends_is_reported_as_draining(self):
        """The subtle one. ``draining`` used to be read off own production, and
        a village whose production is POSITIVE while its consumption exceeds it
        is exactly the operator's 01 -- gross positive, net -5,880/h. Reading
        the sign off production alone would run its granary to zero and call it
        nothing."""
        _trajectories, breaches = self._run(own=1_000, consumption=20_000, stock=50_000)

        assert (2, Resource.CROP, "empty") in {(b.village_id, b.resource, b.kind) for b in breaches}

    def test_no_consumption_leaves_the_composite_identical(self):
        was_rows, was_breaches = self._run(own=1_000, consumption=None)
        now_rows, now_breaches = self._run(own=1_000, consumption=0.0)

        assert now_rows == was_rows
        assert now_breaches == was_breaches


class TestNpcAcrossTheProfileDay:
    """The composite replay needs section 7 too, and needs it per segment.

    Both replays, not one: a previous feature was threaded into ``simulate_day``
    and not into ``simulate_profile_cycle``, so /plan and /day-check answered
    the same account differently. And here attendance matters, because the
    reservoir refills only while a profile the operator is awake for is running.
    """

    RESERVE = NpcReserve(
        village_id=1,
        floor_level=40_000,
        # 96,000/day: exactly the gap between 1's 24,000/day of lumber and the
        # 120,000/day it is asked to ship.
        allowance_per_day=96_000,
        sources=(Resource.CROP,),
        shares=(1.0,),
        drawn=frozenset({Resource.LUMBER}),
    )

    def _segments(self, *, attended: bool) -> list[ProfileSegment]:
        route = Route(
            origin=1,
            destination=2,
            cargo_per_hour={Resource.LUMBER: 5_000},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=60.0,
        )
        beat = build_beat((route,))
        return [
            ProfileSegment(
                name="All day",
                start_minute=0,
                end_minute=1439,
                routes=beat.routes,
                npc_attended=attended,
            )
        ]

    def _run(self, *, attended: bool, npc):
        return simulate_profile_cycle(
            self._segments(attended=attended),
            own_rates={1: {Resource.LUMBER: 1_000, Resource.CROP: 4_000}},
            stocks={1: {Resource.LUMBER: 40_000, Resource.CROP: 200_000}},
            capacities={1: {Resource.LUMBER: 1_000_000, Resource.CROP: 1_000_000}},
            step_minutes=5,
            max_days=6,
            npc=npc,
        )

    def test_the_conversion_moves_crop_into_lumber_one_for_one(self):
        """96,000/day out of the granary and 96,000/day into the warehouse, on
        top of the 24,000/day the fields make -- so the lumber store's day nets
        120,000 of inflow against the 120,000 that ships out."""
        without, _ = self._run(attended=True, npc=None)
        with_npc, _ = self._run(attended=True, npc={1: self.RESERVE})

        crop_before = next(t for t in without if t.resource is Resource.CROP)
        crop_after = next(t for t in with_npc if t.resource is Resource.CROP)

        assert crop_before.daily_net == pytest.approx(96_000, abs=100.0)
        assert crop_after.daily_net == pytest.approx(0.0, abs=100.0)

    def test_an_unattended_profile_converts_nothing(self):
        """The operator is asleep, so the granary keeps every unit and the
        warehouse gets none -- the whole reason attendance is stated per
        segment rather than inferred from the clock."""
        asleep, _ = self._run(attended=False, npc={1: self.RESERVE})

        crop = next(t for t in asleep if t.resource is Resource.CROP)
        assert crop.daily_net == pytest.approx(96_000, abs=100.0)


class TestADesignedCropDeficitIsNotAnEmergency:
    """Profile sections 9.1-9.2: 01 and 03 are permanently crop-negative.

    That is not a fault to be fixed, it is how a hammer works -- the troops eat
    more than the village grows and the difference is shipped in every day. The
    countdown is still the number that matters (review R7 exists because the
    profile's own arithmetic made a draining store read as "no problem"), but
    reporting it as CRITICAL says "resources or troops are being destroyed" of
    an account running exactly as designed, and a critical signal that cries
    wolf is worth less than none. On the operator's own account that was two of
    the twenty-seven reds.

    Severity belongs to the CATEGORY in this codebase and never to the
    individual finding -- whether a fact is worth interrupting someone over
    cannot depend on which village it is about. So the downgrade is a second
    category rather than a per-finding severity, and every village WITHOUT the
    declaration keeps the CRITICAL it has today.
    """

    HAMMER, ORDINARY = 1, 2
    NAMES = {HAMMER: "01", ORDINARY: "17"}

    def _draining(self, vid: int):
        # -5,880/h is village 01's live reading; 100,000 in the granary is 17h.
        return store_status(vid, Resource.CROP, 100_000, 400_000, -5_880)

    def test_without_the_declaration_it_is_still_critical(self):
        """The control. A village nobody described is a village nobody has
        accounted for, and an emptying granary there is an emergency."""
        findings = storage_findings(
            [self._draining(self.ORDINARY)], [], warn_hours=24.0, names=self.NAMES
        )

        assert [f.category for f in findings] == [Category.STARVATION]
        assert findings[0].severity is Severity.CRITICAL

    def test_declaring_it_by_design_makes_it_a_note(self):
        findings = storage_findings(
            [self._draining(self.HAMMER)],
            [],
            warn_hours=24.0,
            names=self.NAMES,
            crop_negative_by_design={self.HAMMER},
        )

        assert [f.category for f in findings] == [Category.STARVATION_BY_DESIGN]
        assert findings[0].severity is Severity.NOTE

    def test_the_note_still_carries_the_hours_of_cover(self):
        """The whole point of R7, and the reason this is a downgrade rather than
        a suppression: -5,880/h with 100,000 in store is seventeen hours, and
        seventeen hours is what the operator has to act inside. A silenced
        finding would take that number away with the red."""
        finding = storage_findings(
            [self._draining(self.HAMMER)],
            [],
            warn_hours=24.0,
            names=self.NAMES,
            crop_negative_by_design={self.HAMMER},
        )[0]

        assert "01" in finding.message
        assert "17.0h" in finding.message, finding.message
        assert "5,880" in finding.message, finding.message
        assert "by design" in finding.message
        assert "17.0h" in finding.detail, finding.detail

    def test_a_declared_village_and_an_undeclared_one_are_reported_apart(self):
        """Both drain at the same rate with the same cover, so nothing but the
        declaration separates them -- which is exactly what has to separate
        them."""
        findings = storage_findings(
            [self._draining(self.HAMMER), self._draining(self.ORDINARY)],
            [],
            warn_hours=24.0,
            names=self.NAMES,
            crop_negative_by_design={self.HAMMER},
        )

        by_village = {f.village: f for f in findings}
        assert by_village["01"].severity is Severity.NOTE
        assert by_village["17"].severity is Severity.CRITICAL

    def test_the_note_does_not_reach_the_accounts_loss_total(self):
        """A NOTE that still summed into "this account loses N a day" would have
        moved the headline instead of the severity."""
        diagnostics = summarise(
            list(
                storage_findings(
                    [self._draining(self.HAMMER)],
                    [],
                    warn_hours=24.0,
                    names=self.NAMES,
                    crop_negative_by_design={self.HAMMER},
                )
            )
        )

        assert diagnostics.total_loss_per_day == 0.0
        assert diagnostics.counts.get("critical", 0) == 0

    def test_a_designed_deficit_does_not_silence_an_overflow_at_the_same_village(self):
        """Only the crop countdown is declared away. A hammer whose warehouse is
        losing lumber is still losing lumber, and the declaration says nothing
        about that."""
        overflow = simulate_day(
            build_beat(()),
            stocks={self.HAMMER: {Resource.LUMBER: 399_000}},
            capacities={self.HAMMER: {Resource.LUMBER: 400_000}},
            net_per_hour={self.HAMMER: {Resource.LUMBER: 1_000.0}},
        )

        findings = storage_findings(
            [self._draining(self.HAMMER)],
            overflow,
            warn_hours=24.0,
            names=self.NAMES,
            crop_negative_by_design={self.HAMMER},
        )

        categories = [f.category for f in findings]
        assert Category.STARVATION_BY_DESIGN in categories
        assert Category.OVERFLOW_STRUCTURAL in categories

    def test_nothing_declared_is_the_default(self):
        """The parameter is optional and its absence must mean the old
        behaviour, because three callers pass it and a fourth (storage_warnings)
        does not."""
        assert (
            storage_warnings([self._draining(self.HAMMER)], [], warn_hours=24.0, names=self.NAMES)[
                0
            ]
            == storage_findings(
                [self._draining(self.HAMMER)], [], warn_hours=24.0, names=self.NAMES
            )[0].message
        )


# ── The partial send is an assumption about the game ─────────────────────────


class TestThePartialSendIsAnAssumptionAboutTheGame:
    """`shipped = min(batch, available)` is a guess, not a law.

    Whether Travian skips an under-funded send outright, ships a partial load,
    or tops the missed amount up from the next cycle is **UNVERIFIED**
    (reference I.5.4), and it qualifies every rate the tool prints. Two things
    were wrong about how that was recorded rather than about the behaviour:
    `docs/25` stated the code assumes the send is SKIPPED, which is the
    opposite of what the replay does, and the replay's own docstring presented
    partial shipping as a property ("cargo is conserved") rather than as an
    assumption -- so a reader of either could not tell there was a question
    open at all.

    The behaviour is deliberately unchanged. What these pin is that it is
    STATED where it is used, and that it is partial rather than skip -- nothing
    distinguished the two before, so a future edit could have flipped it
    silently.
    """

    @staticmethod
    def _route(origin, destination, per_hour):
        return Route(
            origin=origin,
            destination=destination,
            cargo_per_hour={Resource.LUMBER: per_hour},
            cycle_hours=24,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=60.0,
        )

    def test_an_under_funded_send_ships_what_the_origin_holds(self):
        """The behaviour the three possibilities differ on, pinned by number.

        A 24,000 batch leaves village 1 daily, which makes only 6,000 a day. It
        ships that 6,000 -- not zero (skip) and not the full 24,000 (top up) --
        so village 2, already at its cap, loses exactly 6,000 a day.
        """
        beat = build_beat((self._route(1, 2, 1_000),))

        overflows = simulate_day(
            beat,
            stocks={1: {Resource.LUMBER: 0}, 2: {Resource.LUMBER: 10_000}},
            capacities={1: {Resource.LUMBER: 1_000_000}, 2: {Resource.LUMBER: 10_000}},
            net_per_hour={1: {Resource.LUMBER: 250}, 2: {Resource.LUMBER: 0}},
        )

        event = next((e for e in overflows if e.village_id == 2), None)
        assert event is not None, (
            "a skipping replay would deliver nothing and report no loss at all"
        )
        assert event.net_gain_per_day == pytest.approx(6_000.0), (
            "0 would be skip, 24,000 would be a topped-up batch the origin never had"
        )
        assert event.wasted_per_day == pytest.approx(6_000.0)

    def test_the_assumption_is_stated_in_both_replays(self):
        """Stated where it is USED, which is the check the operator's ruling
        register asks for -- not that it is right, which nobody knows."""
        for replay in (simulate_day, simulate_profile_cycle):
            source = inspect.getsource(replay)
            where = replay.__name__
            assert "ASSUMPTION" in source, where
            assert "UNVERIFIED" in source, where
            assert "partial" in source.lower(), where
            assert "skip" in source.lower(), f"{where}: the alternative must be named"
            assert "resource-starved" in source, (
                f"{where}: the one in-game test that settles it must be named"
            )

    def test_the_document_says_the_same_thing_the_code_does(self):
        doc = (
            Path(__file__).resolve().parents[1] / "docs" / "25-resource-distribution-planner.md"
        ).read_text(encoding="utf-8")

        assert "simply SKIPS that send" not in doc, (
            "docs/25 stated the opposite of what both replays do"
        )
        assert "UNVERIFIED" in doc, "and it must still be marked unverified"
        assert "resource-starved" in doc, "and still name the test that settles it"
