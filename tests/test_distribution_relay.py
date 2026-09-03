"""Relayed crop must be visible, and its latency measured end to end.

Two things were wrong at once, and they compound.

The optimizer may reroute a crop flow through an intermediate village, and the
sheet then shows ``V22 -> V02`` and ``V02 -> V17`` as two unrelated rows. An
audit of 66 synthetic accounts found 45 of them using a relay and not one of
8,784 warning lines mentioning that a relay existed. The operator types both
rows into the game with no way to know the second depends on the first.

And the latency target is checked per leg while the documented target is
end-to-end. Two legs each comfortably inside a 2h target composed to ~30h on one
audited account, and the plan reported no latency finding at all -- the one
number that would have exposed the relay was the number nobody computed.

The reporting unit is the HUB, not a path through it. Crop pools in the hub's
granary, so which origin's crop reaches which destination is not something the
plan decides; enumerating the combinations turned 6 real hubs into 41 claimed
deliveries on one account, every one of them asserting a provenance the flow
graph cannot support.
"""

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import Category, Severity
from travian_api.services.distribution.optimizer import Route, relay_findings, relay_hubs
from travian_api.services.distribution.schedule import MINUTES_PER_DAY, build_beat, time_relays

NAMES = {22: "V22", 23: "V23", 2: "V02", 17: "V17", 18: "V18"}


def _route(
    origin: int,
    destination: int,
    *,
    cycle: int = 1,
    one_way: float = 30.0,
    resource: Resource = Resource.CROP,
    amount: float = 500.0,
) -> Route:
    return Route(
        origin=origin,
        destination=destination,
        cargo_per_hour={resource: amount},
        cycle_hours=cycle,
        merchants_per_send=2,
        sets_in_flight=1,
        one_way_minutes=one_way,
    )


class TestFindingTheHubs:
    def test_a_village_that_receives_and_sends_crop_is_a_hub(self):
        hubs = relay_hubs([_route(22, 2), _route(2, 17)])

        assert len(hubs) == 1
        assert (hubs[0].hub, hubs[0].origins, hubs[0].destinations) == (2, (22,), (17,))

    def test_unrelated_legs_are_not_a_relay(self):
        assert relay_hubs([_route(22, 2), _route(3, 17)]) == ()

    def test_a_direct_leg_alone_is_not_a_relay(self):
        assert relay_hubs([_route(22, 2)]) == ()

    def test_material_legs_never_form_a_relay(self):
        """Relay is crop-only by design (profile section 3.5): materials must not
        chain A->B->C. A lumber leg into a village that also ships lumber out is
        two independent flows, not a relay, and calling it one would invent a
        dependency the plan does not have."""
        legs = [
            _route(22, 2, resource=Resource.LUMBER),
            _route(2, 17, resource=Resource.LUMBER),
        ]
        assert relay_hubs(legs) == ()

    def test_a_declared_material_relay_forwards_only_to_its_declared_downstreams(self):
        """Section 5's tier is a DECLARATION, so its legs are the declared ones.

        A relay village also ships material of its own -- 18 and 14 are feeders
        that grow lumber -- and an outbound leg that is not one of the
        downstreams the operator named is not part of the delivery. Reporting it
        as one claims a leg of a delivery it isn't, and hands
        ``relay_buffer_findings`` an unrelated dispatch to read as a "forward
        send", which is what turns a CRITICAL relay-buffer finding into a
        WARNING.

        Crop is untouched -- a crop hub is SEARCHED, not declared, so there is
        no list to check it against.
        """
        legs = [
            _route(22, 2, resource=Resource.LUMBER),
            _route(2, 17, resource=Resource.LUMBER),
            _route(2, 18, resource=Resource.LUMBER),
            # 2's own lumber, to a village nobody named.
            _route(2, 7, resource=Resource.LUMBER),
        ]

        hubs = relay_hubs(legs, material_relays={2: (17, 18)})

        assert len(hubs) == 1
        assert hubs[0].destinations == (17, 18), (
            f"the tier claims {hubs[0].destinations}, which includes a leg nobody declared"
        )

    def test_a_declared_relay_with_no_declared_leg_built_is_not_a_relay(self):
        """Every outbound leg is the relay's own. There is no delivery to report."""
        legs = [
            _route(22, 2, resource=Resource.LUMBER),
            _route(2, 7, resource=Resource.LUMBER),
        ]

        assert relay_hubs(legs, material_relays={2: (17,)}) == ()

    def test_a_mixed_cargo_leg_counts_for_its_crop(self):
        """Routes merge all four resources into one row, so a relay leg can
        arrive carrying lumber as well. What makes it a relay is the crop."""
        legs = [
            Route(
                origin=22,
                destination=2,
                cargo_per_hour={Resource.CROP: 400.0, Resource.LUMBER: 100.0},
                cycle_hours=1,
                merchants_per_send=2,
                sets_in_flight=1,
                one_way_minutes=30.0,
            ),
            _route(2, 17),
        ]
        assert len(relay_hubs(legs)) == 1

    def test_fan_in_and_fan_out_are_ONE_hub_not_their_product(self):
        """The provenance bug, stated as a count.

        Two origins and two destinations is four ways crop *could* travel and
        zero evidence about which it does -- the hub pools it. Reporting four
        deliveries claims something the plan never chose; reporting one hub with
        both lists says exactly what is known.
        """
        hubs = relay_hubs([_route(22, 2), _route(23, 2), _route(2, 17), _route(2, 18)])

        assert len(hubs) == 1
        assert hubs[0].origins == (22, 23)
        assert hubs[0].destinations == (17, 18)

    def test_two_separate_hubs_are_two_relays(self):
        legs = [_route(22, 2), _route(2, 17), _route(23, 18), _route(18, 19)]
        assert {relay.hub for relay in relay_hubs(legs)} == {2, 18}

    def test_a_self_loop_is_not_a_hub(self):
        """A village shipping crop to itself cannot happen, but a self-loop in
        the input must not produce a relay through nowhere."""
        assert relay_hubs([_route(2, 2), _route(2, 17)]) == ()

    def test_a_two_way_pair_is_not_a_relay(self):
        # A -> B and B -> A is not a hub forwarding anything: "ship after you
        # collect" cannot hold at both ends at once. The optimizer refuses to
        # build one, and deriving one from the routes would be worse than
        # silence -- it would name a delivery that cannot happen.
        assert relay_hubs([_route(22, 2), _route(2, 22)]) == ()

    def test_a_hub_that_also_returns_crop_reports_only_the_onward_legs(self):
        hubs = relay_hubs([_route(22, 2), _route(2, 22), _route(2, 17)])

        assert len(hubs) == 1
        assert hubs[0].destinations == (17,), "V22 is a feeder, not a destination"

    def test_worst_relay_reads_first(self):
        hubs = relay_hubs(
            [_route(22, 2, cycle=1), _route(23, 18, cycle=8), _route(18, 19), _route(2, 17)]
        )
        assert [relay.hub for relay in hubs] == [18, 2]

    def test_no_hub_is_another_hubs_origin(self):
        """Two-hop is all the optimizer can build (``_crop_shape_ok`` forbids an
        edge between two hubs), and this derivation models nothing deeper. If
        that ever changes, a 3-hop waterfall would show up here as one hub
        feeding another, and the figures would silently describe two overlapping
        pairs rather than the real delivery."""
        hubs = relay_hubs([_route(22, 2), _route(2, 17), _route(2, 18), _route(23, 2)])
        hub_ids = {relay.hub for relay in hubs}

        for relay in hubs:
            assert not hub_ids & set(relay.origins), "a chain deeper than two hops"


class TestEndToEndLatency:
    def test_it_is_both_waits_in_turn(self):
        """Cargo waits for its origin's next send, travels, then waits for the
        hub's next send and travels again. Worst case is both, which is what a
        per-leg check cannot see."""
        relay = relay_hubs(
            [_route(22, 2, cycle=4, one_way=30.0), _route(2, 17, cycle=8, one_way=90.0)]
        )[0]

        assert relay.collect_hours == pytest.approx(4.5)
        assert relay.forward_hours == pytest.approx(9.5)
        assert relay.end_to_end_hours == pytest.approx(14.0)

    def test_the_worst_leg_sets_each_half(self):
        # One slow feeder is enough to make the delivery slow, so the figure
        # cannot be an average.
        relay = relay_hubs(
            [_route(22, 2, cycle=1), _route(23, 2, cycle=8), _route(2, 17, cycle=1)]
        )[0]

        assert relay.collect_hours == pytest.approx(8.5)

    def test_two_compliant_legs_can_still_miss_the_target(self):
        """The bug, stated as a number. Neither leg exceeds 2h; the delivery
        takes 3h."""
        collect = _route(22, 2, cycle=1, one_way=30.0)
        forward = _route(2, 17, cycle=1, one_way=30.0)

        relay = relay_hubs([collect, forward])[0]

        assert collect.latency_hours <= 2.0
        assert forward.latency_hours <= 2.0
        assert relay.end_to_end_hours > 2.0


class TestThePlanReportsThem:
    def _findings(self, routes, *, target=2.0):
        return relay_findings(relay_hubs(routes), names=NAMES, max_latency_hours=target)

    def test_a_relay_is_reported_even_when_it_meets_the_target(self):
        # Visibility is the point: a compliant relay is still a dependency
        # between two rows the operator is about to type in by hand.
        findings = self._findings(
            [_route(22, 2, cycle=1, one_way=6.0), _route(2, 17, cycle=1, one_way=6.0)],
            target=6.0,
        )

        assert len(findings) == 1
        assert findings[0].category is Category.RELAY
        assert findings[0].severity is Severity.NOTE
        assert "V22" in findings[0].message
        assert "V02" in findings[0].message
        assert "V17" in findings[0].message

    def test_a_relay_over_the_target_is_a_warning_naming_the_hours(self):
        findings = self._findings([_route(22, 2), _route(2, 17)])

        assert len(findings) == 1, "one finding per hub, not one per leg"
        finding = findings[0]
        assert finding.category is Category.RELAY_LATENCY
        assert finding.severity is Severity.WARNING
        assert "3.0h" in finding.message
        assert "V02" in finding.message

    def test_the_figure_is_stated_as_an_upper_bound(self):
        # It is a worst case, like every other latency figure on the sheet.
        # "takes 3.0h" would assert an exactness the schedule does not promise.
        findings = self._findings([_route(22, 2), _route(2, 17)])
        assert "up to" in findings[0].message

    def test_a_relay_is_not_reported_twice(self):
        findings = self._findings([_route(22, 2), _route(2, 17)])
        assert [f.category for f in findings] == [Category.RELAY_LATENCY]

    def test_no_relay_means_no_findings(self):
        assert self._findings([_route(22, 2), _route(3, 17)]) == []

    def test_with_no_target_relays_are_still_announced(self):
        # A None target switches the latency pass off entirely; it must not also
        # hide the relay's existence.
        findings = self._findings([_route(22, 2), _route(2, 17)], target=None)

        assert [f.category for f in findings] == [Category.RELAY]

    def test_both_wordings_say_the_word_relay(self):
        """An operator scanning the warning list for "relay" has to find it.

        The over-target wording originally opened "crop V22 -> V02 -> V17 takes
        3.0h end-to-end", which never says what it is -- so on a plan where every
        relay missed the target, nothing in the output contained the word at all.
        """
        over = self._findings([_route(22, 2), _route(2, 17)])
        under = self._findings([_route(22, 2), _route(2, 17)], target=6.0)

        assert "relay" in over[0].message.lower()
        assert "relay" in under[0].message.lower()

    def test_a_long_village_list_is_abridged(self):
        # Six feeder names inline is the 153-warning problem in miniature.
        legs = [_route(origin, 2) for origin in (22, 23, 24, 25, 26, 27)] + [_route(2, 17)]
        message = self._findings(legs)[0].message

        assert "and 3 more" in message
        assert "village 27" not in message

    def test_the_finding_carries_the_hours_for_ranking(self):
        findings = self._findings([_route(22, 2, cycle=8), _route(2, 17, cycle=8)])
        assert "17.0h" in findings[0].detail


class TestTimingAgainstTheRealSchedule:
    """``relay_hubs`` estimates each wait from a cycle length, which assumes the
    route fires all day. Inside a profile window it does not -- the beat drops
    every firing outside it -- so the cargo can land at the hub after that
    window's last forward send and wait until tomorrow. That is the case the app
    is normally used in (night 8h, startday 2h, endday 1h), so an estimate that
    silently understates it is the same class of bug as not reporting relays at
    all.
    """

    def _timed(self, routes, window=None):
        beat = build_beat(routes, dispatch_window=window)
        return time_relays(beat, relay_hubs(routes), window)

    def test_the_collecting_leg_matches_its_cycle_with_no_window(self):
        # Every firing happens, so the longest gap between sends IS the cycle.
        routes = [_route(22, 2, cycle=4, one_way=30.0), _route(2, 17, cycle=4, one_way=30.0)]
        estimate = relay_hubs(routes)[0]

        timed = self._timed(routes)[0]

        assert timed.collect_hours == pytest.approx(estimate.collect_hours)

    def test_the_estimate_is_pessimistic_where_the_beat_phases_well(self):
        """The other half of measuring instead of assuming.

        The cycle-based estimate charges the hub a whole forwarding cycle of
        waiting. The beat does not schedule it that way: it phases a hub to ship
        soon after it collects, so the real wait is minutes and the estimate
        overstates the delivery by hours. Reading the schedule cuts false
        latency warnings as well as exposing the windowed ones.
        """
        routes = [_route(22, 2, cycle=4, one_way=30.0), _route(2, 17, cycle=4, one_way=30.0)]

        estimate = relay_hubs(routes)[0]
        timed = self._timed(routes)[0]

        assert timed.forward_hours < estimate.forward_hours
        assert timed.end_to_end_hours < estimate.end_to_end_hours

    def test_a_one_hour_window_exposes_the_day_long_wait(self):
        """One firing a day, so a batch produced just after it waits ~24h.

        The cycle-based estimate calls this 1h; the schedule says otherwise, and
        the schedule is what the operator will actually run.
        """
        window = (6 * 60, 7 * 60)
        routes = [_route(22, 2, cycle=1, one_way=10.0), _route(2, 17, cycle=1, one_way=10.0)]

        estimate = relay_hubs(routes)[0]
        timed = self._timed(routes, window)[0]

        assert estimate.end_to_end_hours < 3.0, "what the estimate believed"
        assert timed.collect_hours >= 24.0, "one send a day is a day of waiting"
        assert timed.end_to_end_hours > estimate.end_to_end_hours

    def test_the_hub_wait_is_measured_from_when_cargo_lands(self):
        # A hub that forwards shortly after collecting waits minutes, not a
        # cycle -- which is what the beat's own collect-then-ship preference is
        # for, and a cycle-based figure can never show.
        routes = [_route(22, 2, cycle=1, one_way=6.0), _route(2, 17, cycle=1, one_way=6.0)]

        timed = self._timed(routes)[0]

        assert timed.forward_hours <= 1.1, "at most one cycle plus the short trip"

    def test_every_relay_survives_the_re_timing(self):
        routes = [_route(22, 2), _route(2, 17), _route(23, 18), _route(18, 19)]

        assert len(self._timed(routes)) == len(relay_hubs(routes)) == 2

    def test_the_worst_onward_leg_sets_the_hub_wait(self):
        # Fan-out: the cargo is pooled, so any of it can be what waits longest
        # for any onward send. Averaging the destinations would hide the slow one.
        routes = [
            _route(22, 2, cycle=1, one_way=6.0),
            _route(2, 17, cycle=1, one_way=6.0),
            _route(2, 18, cycle=8, one_way=200.0),
        ]
        timed = self._timed(routes)[0]

        assert timed.destinations == (17, 18)
        assert timed.forward_hours > 3.0, "the 8h/200min leg, not the fast one"

    def test_a_windowed_hub_is_phased_against_real_arrivals_only(self):
        """A firing outside the profile's hours never leaves, so neither does its
        arrival exist.

        Scoring the hub's phase against every theoretical arrival let it be
        placed just after a *phantom* one -- and therefore before the first real
        one, which is the whole failure the collect-then-ship ordering exists to
        prevent. What is pinned here is the outcome: the forwarding send happens
        after cargo has actually landed, so the wait is under a full turn of the
        clock rather than just short of one.
        """
        window = (6 * 60, 10 * 60)
        routes = [_route(22, 2, cycle=2, one_way=25.0), _route(2, 17, cycle=2, one_way=25.0)]

        timed = self._timed(routes, window)[0]

        assert timed.forward_hours < 24.0, "the hub is forwarding cargo it has not got"

    def test_a_wait_is_never_longer_than_a_day_plus_the_trip(self):
        # Both schedules repeat daily, so nothing can wait more than one full
        # turn of the clock plus its trip. A modulo mistake would show up here as
        # a negative or a multi-day figure.
        routes = [_route(22, 2, cycle=3, one_way=45.0), _route(2, 17, cycle=6, one_way=90.0)]

        timed = self._timed(routes, (22 * 60, 2 * 60))[0]

        for hours, travel in ((timed.collect_hours, 45.0), (timed.forward_hours, 90.0)):
            assert 0.0 <= hours <= (MINUTES_PER_DAY + travel) / 60.0


class TestTheEndpointShowsThem:
    """Through /plan, on a synthetic account that actually relays.

    The audit that motivated this found 45 of 66 accounts relaying and not one
    of 8,784 warning lines mentioning it, so the claim being pinned is end to
    end: the relay reaches the response, and it reaches the prose.
    """

    def _plan(self):
        import asyncio
        from types import SimpleNamespace

        from tests.distribution_synthetic import random_account
        from travian_api.web.routes import distribution as dist

        request = random_account(0, with_profiles=False).plan_request
        return asyncio.run(dist.post_plan(request, SimpleNamespace(id=1, username="t")))

    def test_a_relaying_account_reports_its_relays(self):
        result = self._plan()
        assert result.relays, "this seed relays; a response that says nothing is the bug"

    def test_one_row_per_hub_not_one_per_combination(self):
        result = self._plan()
        assert len({relay.hub for relay in result.relays}) == len(result.relays)

    def test_every_leg_of_every_relay_is_a_real_sheet_row(self):
        # Otherwise the panel names a hop the operator cannot find in the sheet.
        result = self._plan()
        pairs = {(row.origin, row.destination) for row in result.rows}

        for relay in result.relays:
            for origin in relay.origins:
                assert (origin, relay.hub) in pairs
            for destination in relay.destinations:
                assert (relay.hub, destination) in pairs

    def test_the_names_line_up_with_the_ids(self):
        # The panel reads the names and the sheet joins on the ids, so a pair
        # that drifted out of step would label the wrong village.
        result = self._plan()
        for relay in result.relays:
            assert len(relay.origins) == len(relay.origin_names)
            assert len(relay.destinations) == len(relay.destination_names)

    def test_every_relay_is_mentioned_in_the_prose(self):
        result = self._plan()
        relay_lines = [line for line in result.warnings if "relay" in line.lower()]

        assert relay_lines
        for relay in result.relays:
            assert any(relay.hub_name in line for line in relay_lines), (
                f"{relay.hub_name} relays and nothing says so"
            )

    def test_the_diagnostics_carry_a_relay_group(self):
        result = self._plan()
        categories = {group.category for group in result.diagnostics.groups}

        assert {"relay", "relay_latency"} & categories


class TestRelayTimingOnlyFiltersAWindowThatIsEnforced:
    """A windowed profile the executor will NOT prune fires round the clock.

    Travian fans a repeat interval across the whole day and offers nothing to
    confine it, which is why `prune_to_window` exists: with pruning the
    out-of-window rows are deleted after creation and the window is real,
    without it every firing happens and the plan says so (a CRITICAL
    WINDOW_NOT_ENFORCEABLE finding). `time_relays` filters each route's sends to
    the window, so handing it a window that is not enforced re-times the hub
    against a schedule nobody runs: the firings it drops are exactly the ones
    that make the wait short. Measured on this fixture, that reported 53.0h of
    relay latency where the truthful worst case is 13.0h -- and relay latency is
    what the plan tells the operator to buy merchants for.

    `_storage_findings` already gates its window on `prune_to_window`; this path
    is the same question about the same schedule.
    """

    WINDOW = (6 * 60, 7 * 60)

    def _relays(self, *, prune: bool):
        from travian_api.services.distribution.allocation import Allocation, AllocationMode
        from travian_api.services.distribution.geometry import MapGeometry
        from travian_api.services.distribution.merchants import EUROPE2_TEUTON
        from travian_api.services.distribution.optimizer import VillageState
        from travian_api.services.distribution.planner import PlannerConfig, craft_plan

        villages = {
            1: VillageState(1, 0, 0, merchant_count=20, trade_office_level=15, crop_per_hour=0.0),
            2: VillageState(2, 60, 0, merchant_count=20, trade_office_level=10, crop_per_hour=0.0),
            3: VillageState(
                3, 120, 0, merchant_count=6, trade_office_level=10, crop_per_hour=9000.0
            ),
        }
        productions = {Resource.CROP: {1: 0.0, 2: 0.0, 3: 9000.0}}
        allocations = {
            Resource.CROP: {
                1: Allocation(AllocationMode.REMAINDER),
                2: Allocation(AllocationMode.ABSOLUTE, 0.0),
                3: Allocation(AllocationMode.ABSOLUTE, 0.0),
            }
        }
        config = PlannerConfig(
            geometry=MapGeometry(span=401, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            max_latency_hours=None,
            dispatch_window=self.WINDOW,
            prune_to_window=prune,
        )
        return craft_plan(villages, productions, allocations, config).relays

    def test_the_fixture_still_relays_under_both_settings(self):
        assert self._relays(prune=True), "no relay: the comparison below is vacuous"
        assert self._relays(prune=False), "no relay: the comparison below is vacuous"

    def test_an_unpruned_window_is_timed_against_every_firing(self):
        unpruned = self._relays(prune=False)[0]
        pruned = self._relays(prune=True)[0]

        assert unpruned.end_to_end_hours < pruned.end_to_end_hours, (
            f"an unpruned windowed profile was re-timed against the pruned "
            f"schedule: {unpruned.end_to_end_hours:.1f}h reported where every "
            f"firing really happens"
        )
        assert unpruned.end_to_end_hours < 24.0, (
            "a route that fires round the clock cannot wait a day for its next send"
        )
