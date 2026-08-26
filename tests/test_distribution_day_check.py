"""The full-day check: every profile in its own hours, one stock trajectory.

Profiles are planned in isolation, but the account lives through all of them
every day. What the day profile ships decides the stock the night profile
starts from, so "does the capital cross 90k during the night?" is unanswerable
per-profile -- it needs the composite, and these tests pin that the composite
answers it with an hour and a profile name attached.
"""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.optimizer import Route
from travian_api.services.distribution.schedule import ScheduledRoute
from travian_api.services.distribution.storage import ProfileSegment, simulate_profile_cycle
from travian_api.web.routes.distribution import DayCheckRequest, post_day_check


class TestSimulateProfileCycle:
    def test_the_night_profile_inherits_the_day_profiles_stock(self):
        """The user's scenario. The capital receives heavily by day (its own
        +5,000/h plus +10,000/h shipped in, 07:00-23:00) and receives almost
        nothing by night. Whether it crosses a 90,000 alert at night depends
        entirely on where the DAY profile left the stock -- which is exactly
        what per-profile planning cannot see."""
        day = ProfileSegment(
            name="Day",
            start_minute=7 * 60,
            end_minute=23 * 60,
            manual_rates={1: {Resource.CROP: 10_000.0}},
        )
        night = ProfileSegment(
            name="Night",
            start_minute=23 * 60,
            end_minute=7 * 60,
            manual_rates={1: {Resource.CROP: 500.0}},
        )

        trajectories, breaches = simulate_profile_cycle(
            [day, night],
            own_rates={1: {Resource.CROP: 5_000.0}},
            stocks={1: {Resource.CROP: 10_000}},
            capacities={1: {Resource.CROP: 800_000}},
            ceilings={1: 90_000.0},
        )

        ceiling_hits = [b for b in breaches if b.kind == "ceiling"]
        assert ceiling_hits, "the stock climbs 284k/day; the 90k alert must fire"
        first = ceiling_hits[0]
        # +15,000/h from 07:00 on 10,000 opening stock crosses 90,000 within
        # day one's Day window -- and the breach must say so by name.
        assert first.day == 0
        assert first.segment == "Day"
        assert 7 * 60 <= first.minute < 23 * 60

    def test_a_balanced_pair_settles_and_reports_its_swing(self):
        """Night keeps crop home (+4,000/h net), day ships hard (-2,000/h net):
        the day drains what the night banked. The composite must settle into a
        repeating day and report the low/high swing rather than drift."""
        day = ProfileSegment(
            name="Day",
            start_minute=8 * 60,
            end_minute=20 * 60,  # 12h at -2,000/h net = -24,000
            manual_rates={1: {Resource.CROP: -6_000.0}},
        )
        night = ProfileSegment(
            name="Night",
            start_minute=20 * 60,
            end_minute=8 * 60,  # 12h at +2,000/h net = +24,000
            manual_rates={1: {Resource.CROP: -2_000.0}},
        )

        trajectories, breaches = simulate_profile_cycle(
            [day, night],
            own_rates={1: {Resource.CROP: 4_000.0}},
            stocks={1: {Resource.CROP: 50_000}},
            capacities={1: {Resource.CROP: 800_000}},
        )

        (crop,) = [t for t in trajectories if t.resource is Resource.CROP]
        assert crop.settled, "a zero-daily-net pair must reach a repeating day"
        assert abs(crop.daily_net) < 1.0
        # The swing is 24,000 wide: banked by night, spent by day.
        assert crop.high - crop.low == pytest.approx(24_000, rel=0.02)
        assert not breaches

    def test_hours_with_no_profile_run_on_production_alone(self):
        """A gap between windows means no routes exist then -- production
        continues, shipping stops. The gap must not inherit either profile."""
        only_day = ProfileSegment(
            name="Day",
            start_minute=6 * 60,
            end_minute=18 * 60,
            manual_rates={1: {Resource.CROP: -5_000.0}},
        )

        trajectories, _ = simulate_profile_cycle(
            [only_day],
            own_rates={1: {Resource.CROP: 5_000.0}},
            stocks={1: {Resource.CROP: 100_000}},
            capacities={1: {Resource.CROP: 800_000}},
        )

        (crop,) = trajectories
        # 12h of net zero (day) + 12h of +5,000/h (gap) = +60,000/day. The
        # nominal drift must survive the store clamping at its cap -- measured
        # off simulated levels it would collapse to zero once pinned there,
        # reading as "stable" about a village overflowing daily.
        assert crop.daily_net == pytest.approx(60_000, rel=0.01)

    def test_a_store_already_above_its_alert_reports_standing_not_crossing(self):
        """A draining store that starts above the ceiling never crosses it
        upward. Claiming "crosses at 00:00" would be factually inverted -- the
        standing condition is its own kind, reported once."""
        segment = ProfileSegment(
            name="Day",
            start_minute=0,
            end_minute=720,
            manual_rates={},
        )

        _, breaches = simulate_profile_cycle(
            [segment],
            own_rates={1: {Resource.CROP: -500.0}},
            stocks={1: {Resource.CROP: 90_000}},
            capacities={1: {Resource.CROP: 240_000}},
            ceilings={1: 50_000.0},
        )

        kinds = {b.kind for b in breaches}
        assert "above" in kinds, f"the standing condition must be reported: {breaches}"
        assert "ceiling" not in kinds, "a store draining from above never crosses upward"
        above = next(b for b in breaches if b.kind == "above")
        assert (above.day, above.minute) == (0, 0)

    def test_a_ceiling_misconfigured_above_the_cap_never_fires(self):
        """The ceiling is checked on the post-clamp level: the store physically
        cannot exceed its cap, so an alert set above it must stay silent."""
        segment = ProfileSegment(name="Day", start_minute=0, end_minute=720, manual_rates={})

        _, breaches = simulate_profile_cycle(
            [segment],
            own_rates={1: {Resource.CROP: 4_000.0}},
            stocks={1: {Resource.CROP: 79_000}},
            capacities={1: {Resource.CROP: 80_000}},
            ceilings={1: 90_000.0},
        )

        kinds = {b.kind for b in breaches}
        assert "capacity" in kinds
        assert "ceiling" not in kinds, "90k is unreachable behind an 80k cap"
        assert "above" not in kinds


class TestDayCheckEndpoint:
    def _village(self, vid, name, crop, crop_stock, granary):
        return {
            "village_id": vid,
            "name": name,
            "x": 0,
            "y": vid,
            "merchants_total": 20,
            "merchants_free": 20,
            "lumber_per_hour": 0,
            "clay_per_hour": 0,
            "iron_per_hour": 0,
            "crop_per_hour": crop,
            "crop_stock": crop_stock,
            "granary_capacity": granary,
            "warehouse_capacity": 400_000,
        }

    def test_the_ceiling_breach_names_the_village_the_hour_and_the_profile(self):
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [
                    self._village(1, "02", crop=20_000, crop_stock=60_000, granary=800_000),
                    self._village(2, "03", crop=-2_000, crop_stock=90_000, granary=240_000),
                ],
                "segments": [
                    {
                        # By day everything flows to the capital.
                        "name": "Day",
                        "window": [420, 1380],
                        "allocations": {
                            "crop": {
                                "1": {"mode": "remainder"},
                                "2": {"mode": "sustain", "value": 5},
                            }
                        },
                    },
                    {
                        # By night villages keep their crop; nothing moves.
                        "name": "Night",
                        "window": [1380, 420],
                        "allocations": {},
                    },
                ],
                "crop_ceilings": {"1": 90_000},
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        alert = [w for w in res.warnings if "90,000 alert" in w]
        assert alert, f"no ceiling warning in {res.warnings}"
        assert alert[0].startswith("02:"), "the warning must name the village"
        # The composite's whole point: the breach happens at NIGHT, not by day.
        # By day the capital actually SENDS (remainder absorbs a negative
        # residue), but at night no routes run and its own 20,000/h keeps
        # flowing -- 60,000 stock crosses 90,000 at 01:30 during Night. A
        # per-profile view could never have said that.
        assert "during Night" in alert[0], "the warning must name the profile running"
        assert "01:30" in alert[0], "the warning must carry the hour"
        capital = next(v for v in res.villages if v.village_id == 1 and v.resource is Resource.CROP)
        assert capital.village_name == "02"
        assert capital.daily_net > 0

    def test_a_foreign_tribute_drains_the_day_it_would_otherwise_flatter(self):
        """The backend reviewer's measured failure: a hub producing 3,000/h
        owing a 4,000/h tribute showed +72,000/day when reality is net
        -1,000/h and dry in ~50 hours. The tribute must drain the composite
        exactly as POST /plan ships it."""
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [
                    self._village(1, "02", crop=3_000, crop_stock=50_000, granary=200_000)
                ],
                "segments": [
                    {
                        "name": "All day",
                        "window": [0, 1439],
                        "allocations": {"crop": {"1": {"mode": "remainder"}}},
                    }
                ],
                "foreign_targets": [
                    {"name": "Ally capital", "x": 10, "y": 10, "crop_per_hour": 4_000}
                ],
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        hub = next(v for v in res.villages if v.village_id == 1 and v.resource is Resource.CROP)
        assert hub.daily_net == pytest.approx(-24_000, rel=0.02), (
            "own 3,000/h minus a 4,000/h tribute is net -1,000/h, not +3,000/h"
        )
        dry = [w for w in res.warnings if "runs dry" in w]
        assert dry and dry[0].startswith("02:"), f"the starvation must be warned: {res.warnings}"
        assert all(v.village_id > 0 for v in res.villages), (
            "the sink has no trajectory of its own; its drain lives in the senders"
        )

    def test_a_profile_without_a_remainder_leaves_the_tribute_unpaid_and_says_so(self):
        """No remainder village means nothing funds the sink's absolute target:
        resolve leaves every real village at its own rate. Modeling that
        silently would just move the optimism, so it must be said out loud."""
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [
                    self._village(1, "02", crop=3_000, crop_stock=50_000, granary=200_000)
                ],
                "segments": [{"name": "Night", "window": [0, 1439], "allocations": {}}],
                "foreign_targets": [
                    {"name": "Ally capital", "x": 10, "y": 10, "crop_per_hour": 4_000}
                ],
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        unpaid = [w for w in res.warnings if "nothing funds the tribute" in w]
        assert unpaid and unpaid[0].startswith("Night:"), f"warnings: {res.warnings}"
        hub = next(v for v in res.villages if v.resource is Resource.CROP)
        assert hub.daily_net == pytest.approx(72_000, rel=0.02), (
            "unpaid means unpaid: the hub keeps its own production"
        )

    def test_a_nonconserved_profile_cannot_report_a_green_all_clear(self):
        """An absolute receiver target with no remainder to source it is not
        conserved: resolve_resource hands back inflow no route supplies. The
        day check must surface that warning rather than credit crop from
        nothing and fall through to 'all clear'."""
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [
                    self._village(1, "02", crop=1_000, crop_stock=50_000, granary=800_000),
                    self._village(2, "03", crop=1_000, crop_stock=50_000, granary=800_000),
                ],
                "segments": [
                    {
                        # 02 is told to hold 50,000/h with nobody assigned to send it.
                        "name": "Day",
                        "window": [0, 1439],
                        "allocations": {"crop": {"1": {"mode": "absolute", "value": 50_000}}},
                    }
                ],
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        assert res.warnings, "a non-conserved profile must not be silently accepted"
        assert any("unallocated" in w and w.startswith("Day:") for w in res.warnings)

    def test_an_unreadable_crop_rate_is_reported_not_silently_dropped(self):
        """A crop_per_hour=None village sits the check out -- allocations
        dropped, no crop row, its alert level ignored. The response must say
        so instead of merely looking complete."""
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [
                    self._village(1, "02", crop=5_000, crop_stock=10_000, granary=800_000),
                    self._village(2, "05", crop=None, crop_stock=79_000, granary=80_000),
                ],
                "segments": [{"name": "Day", "window": [0, 1439], "allocations": {}}],
                "crop_ceilings": {"2": 79_500},
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        note = [w for w in res.warnings if "no rate could be read" in w]
        assert note, f"warnings: {res.warnings}"
        assert "05" in note[0], "the sidelined village must be named"
        assert "crop alert levels included" in note[0], (
            "its ignored ceiling is the dangerous part -- 1,000 under the cap"
        )
        assert not any(v.village_id == 2 and v.resource is Resource.CROP for v in res.villages)

    def test_a_stock_already_above_its_alert_is_worded_as_standing(self):
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [
                    self._village(1, "02", crop=-2_000, crop_stock=373_000, granary=800_000)
                ],
                "segments": [{"name": "Day", "window": [0, 1439], "allocations": {}}],
                "crop_ceilings": {"1": 90_000},
            }
        )

        res = asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        alert = [w for w in res.warnings if "alert" in w]
        assert alert, f"warnings: {res.warnings}"
        assert "already above its 90,000 alert level" in alert[0]
        assert not any("crosses" in w for w in res.warnings), (
            "a draining store above the alert never crosses it upward"
        )

    def test_overlapping_windows_are_rejected(self):
        body = DayCheckRequest.model_validate(
            {
                "snapshot": [self._village(1, "02", 1000, 0, 80_000)],
                "segments": [
                    {"name": "Day", "window": [0, 720], "allocations": {}},
                    {"name": "Night", "window": [600, 1439], "allocations": {}},
                ],
            }
        )

        with pytest.raises(HTTPException) as exc:
            asyncio.run(post_day_check(body, SimpleNamespace(id=1)))

        assert exc.value.status_code == 400
        assert "overlap" in exc.value.detail

    def test_costs_no_game_requests(self):
        import inspect

        from travian_api.web.sessions import get_travian_session

        for parameter in inspect.signature(post_day_check).parameters.values():
            assert getattr(parameter.default, "dependency", None) is not get_travian_session


class TestImpactTimeAttribution:
    """Cargo belongs to the profile it LANDS in, not the one that sent it.

    A route dispatched at 22:00 under Day, travelling 100 minutes, arrives at
    23:40 -- under Night. Modelling each profile's shipping as a rate confined
    to its own hours drops that delivery entirely: the night's inflow is
    understated by whatever was in the air at the boundary, which is the
    optimistic direction for an overnight overflow.
    """

    @staticmethod
    def _route(origin, destination, crop_per_hour, cycle_hours, one_way_minutes, dispatch_minute):
        return ScheduledRoute(
            route=Route(
                origin=origin,
                destination=destination,
                cargo_per_hour={Resource.CROP: crop_per_hour},
                cycle_hours=cycle_hours,
                merchants_per_send=1,
                sets_in_flight=1,
                one_way_minutes=one_way_minutes,
            ),
            dispatch_minute=dispatch_minute,
        )

    def test_a_day_dispatch_landing_at_night_is_credited_at_night(self):
        # 20,000 crop leaves village 2 at 22:00 (inside Day) and lands at 23:40
        # (inside Night). The capital sits 5,000 under its alert and produces
        # nothing of its own, so ONLY that delivery can cross the alert -- and
        # the crossing must be reported at 23:40, during Night.
        day = ProfileSegment(
            name="Day",
            start_minute=7 * 60,
            end_minute=23 * 60,
            routes=(self._route(2, 1, 20_000 / 24, 24, 100.0, 22 * 60),),
        )
        night = ProfileSegment(name="Night", start_minute=23 * 60, end_minute=7 * 60, routes=())

        _, breaches = simulate_profile_cycle(
            [day, night],
            own_rates={1: {Resource.CROP: 0.0}, 2: {Resource.CROP: 900.0}},
            stocks={1: {Resource.CROP: 95_000}, 2: {Resource.CROP: 200_000}},
            capacities={1: {Resource.CROP: 800_000}, 2: {Resource.CROP: 2_000_000}},
            ceilings={1: 100_000.0},
        )

        hits = [b for b in breaches if b.kind == "ceiling" and b.village_id == 1]
        assert hits, "the delivery pushes the capital from 95k over its 100k alert"
        first = hits[0]
        assert first.minute == 22 * 60 + 100, "credited at the ARRIVAL minute, not the dispatch"
        assert first.segment == "Night", (
            "the cargo left under Day but lands under Night, so Night owns its impact"
        )

    def test_the_origin_is_debited_when_it_dispatches_not_when_it_arrives(self):
        # The same route seen from the sender: it loses the cargo at 22:00,
        # while it is still Day. Outflow keeps dispatch time; only the credit
        # moves to impact time.
        day = ProfileSegment(
            name="Day",
            start_minute=7 * 60,
            end_minute=23 * 60,
            routes=(self._route(2, 1, 20_000 / 24, 24, 100.0, 22 * 60),),
        )
        night = ProfileSegment(name="Night", start_minute=23 * 60, end_minute=7 * 60, routes=())

        _, breaches = simulate_profile_cycle(
            [day, night],
            own_rates={1: {Resource.CROP: 0.0}, 2: {Resource.CROP: 0.0}},
            # The sender holds exactly one batch, so dispatching empties it.
            stocks={1: {Resource.CROP: 0}, 2: {Resource.CROP: 20_000}},
            capacities={1: {Resource.CROP: 800_000}, 2: {Resource.CROP: 800_000}},
        )

        empties = [b for b in breaches if b.kind == "empty" and b.village_id == 2]
        assert empties, "the sender is emptied by its own dispatch"
        assert empties[0].minute == 22 * 60
        assert empties[0].segment == "Day"

    def test_cargo_is_conserved_when_the_origin_cannot_fund_the_batch(self):
        # The sender holds only a quarter of the batch. The destination must be
        # credited what actually left, not the nominal batch -- crediting the
        # full amount invents resources and then reports the invention as an
        # overflow at the far end.
        day = ProfileSegment(
            name="Day",
            start_minute=0,
            end_minute=23 * 60,
            routes=(self._route(2, 1, 20_000 / 24, 24, 60.0, 10 * 60),),
        )
        trajectories, _ = simulate_profile_cycle(
            [day],
            own_rates={1: {Resource.CROP: 0.0}, 2: {Resource.CROP: 0.0}},
            stocks={1: {Resource.CROP: 0}, 2: {Resource.CROP: 5_000}},
            capacities={1: {Resource.CROP: 800_000}, 2: {Resource.CROP: 800_000}},
        )
        capital = next(t for t in trajectories if t.village_id == 1)
        assert capital.high == pytest.approx(5_000.0), (
            "only the 5,000 the sender actually had may arrive, not the 20,000 batch"
        )


class TestNarrowProfileWindow:
    """A profile that runs two hours must still ship inside them.

    Measured: a farm making 200 crop/h 25 fields out is routed on a 6h cycle,
    and the beat -- which knew nothing of the profile's hours -- phased all four
    of its daily firings outside a 20:00-22:00 profile. The dispatch filter then
    correctly dropped every one of them, so the capital received +0/day while
    the farm banked the lot: a starved receiver and a hoarding sender, silently.
    """

    def _body(self):
        def village(vid, name, crop, x, y):
            return {
                "village_id": vid,
                "name": name,
                "x": x,
                "y": y,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 0,
                "clay_per_hour": 0,
                "iron_per_hour": 0,
                "crop_per_hour": crop,
                "crop_stock": 200_000,
                "granary_capacity": 800_000,
                "warehouse_capacity": 400_000,
            }

        return DayCheckRequest.model_validate(
            {
                "snapshot": [
                    village(1, "capital", crop=0, x=0, y=0),
                    village(2, "farm", crop=200, x=20, y=15),
                ],
                "segments": [
                    {
                        "name": "Burst",
                        "window": [20 * 60, 22 * 60],
                        "allocations": {
                            "crop": {
                                "1": {"mode": "remainder"},
                                "2": {"mode": "absolute", "value": 0},
                            }
                        },
                    }
                ],
            }
        )

    def test_the_receiver_is_not_starved_by_the_beats_phase(self):
        res = asyncio.run(post_day_check(self._body(), SimpleNamespace(id=1)))

        capital = next(v for v in res.villages if v.village_id == 1 and v.resource is Resource.CROP)
        farm = next(v for v in res.villages if v.village_id == 2 and v.resource is Resource.CROP)
        # One 6h batch is 1,200 crop. That is all a 2h profile can host, but it
        # must actually land -- the window-blind beat delivered nothing at all.
        assert capital.daily_net == pytest.approx(1_200, rel=0.01), (
            f"the capital receives {capital.daily_net:,.0f}/day; the route sends "
            f"nothing inside the profile's hours"
        )
        assert farm.daily_net == pytest.approx(4_800 - 1_200, rel=0.01), (
            "what leaves the farm must be what lands at the capital"
        )

    def test_a_cycle_longer_than_the_profile_warns_under_its_name(self):
        res = asyncio.run(post_day_check(self._body(), SimpleNamespace(id=1)))

        # Two findings now describe a 6h cycle in a 2h profile, and they say
        # different things: this one is the UNDER-delivery (it fires at most once
        # inside the hours), while WINDOW_NOT_ENFORCEABLE is the over-delivery the
        # game performs outside them. Select on the wording that distinguishes
        # them, or this asserts against whichever happens to sort first.
        unachievable = [w for w in res.warnings if "once a day" in w]
        assert unachievable, f"warnings: {res.warnings}"
        assert unachievable[0].startswith("Burst:"), "the warning must name the profile"
        assert "repeats every 6h" in unachievable[0]


class TestCargoConservationAndSinks:
    """Two regressions the impact-time refactor introduced, both found by review."""

    @staticmethod
    def _route(origin, destination, crop_per_hour, cycle_hours, one_way_minutes, dispatch_minute):
        return ScheduledRoute(
            route=Route(
                origin=origin,
                destination=destination,
                cargo_per_hour={Resource.CROP: crop_per_hour},
                cycle_hours=cycle_hours,
                merchants_per_send=1,
                sets_in_flight=1,
                one_way_minutes=one_way_minutes,
            ),
            dispatch_minute=dispatch_minute,
        )

    def test_an_arrival_that_wraps_past_midnight_invents_no_cargo(self):
        """A firing whose arrival minute precedes its dispatch minute lands
        before the dispatch on day 0. Standing in the nominal batch credits
        cargo the origin never had, and nothing ever drains it -- so the
        invention survives into the settled day, `high` and `daily_net`,
        contradicting the conservation the module promises."""
        # Village 1 holds nothing and produces nothing, so it can never ship.
        day = ProfileSegment(
            name="All day",
            start_minute=0,
            end_minute=1439,
            routes=(self._route(1, 2, 24_000 / 24, 24, 120.0, 23 * 60 + 20),),
        )
        trajectories, _ = simulate_profile_cycle(
            [day],
            own_rates={1: {Resource.CROP: 0.0}, 2: {Resource.CROP: 0.0}},
            stocks={1: {Resource.CROP: 0}, 2: {Resource.CROP: 0}},
            capacities={1: {Resource.CROP: 800_000}, 2: {Resource.CROP: 800_000}},
        )
        receiver = next(t for t in trajectories if t.village_id == 2)
        assert receiver.high == 0.0, "the sender has nothing to send, so nothing may arrive"
        assert receiver.daily_net == 0.0

    def test_a_destination_with_no_store_gets_no_trajectory(self):
        """Foreign tributes enter the optimizer as negative-id pseudo-villages
        with no production, no capacity and no consumption, so a route to one
        would grow an unbounded phantom store. It never settles, and `settled`
        is one global flag -- so a single tribute makes every village in the
        response report as still drifting."""
        day = ProfileSegment(
            name="All day",
            start_minute=0,
            end_minute=1439,
            routes=(self._route(1, -1, 500.0, 6, 30.0, 60),),
        )
        trajectories, _ = simulate_profile_cycle(
            [day],
            own_rates={1: {Resource.CROP: 500.0}},
            stocks={1: {Resource.CROP: 100_000}},
            capacities={1: {Resource.CROP: 800_000}},
        )
        assert [t.village_id for t in trajectories] == [1], (
            "the sink is not a village and must not get a row"
        )
        assert all(t.settled for t in trajectories), (
            "an untracked sink must not keep the whole account from settling"
        )


class TestTheReportedDayIsOneDay:
    """low, high and daily_net must all describe the SAME day.

    A dispatch delivers only what the origin could fund, so a figure taken on
    day 0 depends on the snapshot's opening stock -- while low and high are read
    off the settled day. Review measured the same account reporting 48,000/day
    or 46,000/day purely from the sender's opening stock, with an identical
    steady state and `settled=True` either way.
    """

    @staticmethod
    def _segment(cycle_hours=12):
        return ProfileSegment(
            name="All day",
            start_minute=0,
            end_minute=1439,
            routes=(
                ScheduledRoute(
                    route=Route(
                        origin=2,
                        destination=1,
                        cargo_per_hour={Resource.CROP: 2_000.0},
                        cycle_hours=cycle_hours,
                        merchants_per_send=1,
                        sets_in_flight=1,
                        one_way_minutes=30.0,
                    ),
                    dispatch_minute=60,
                ),
            ),
        )

    def _run(self, sender_opening):
        return simulate_profile_cycle(
            [self._segment()],
            own_rates={1: {Resource.CROP: 0.0}, 2: {Resource.CROP: 2_000.0}},
            stocks={1: {Resource.CROP: 0}, 2: {Resource.CROP: sender_opening}},
            capacities={1: {Resource.CROP: 800_000}, 2: {Resource.CROP: 800_000}},
        )

    def test_the_opening_stock_does_not_change_the_reported_daily_net(self):
        full, _ = self._run(200_000)
        empty, _ = self._run(0)

        def net(trajectories, vid):
            return next(t.daily_net for t in trajectories if t.village_id == vid)

        assert net(full, 1) == pytest.approx(net(empty, 1)), (
            "the receiver's daily net must describe the settled day, "
            "not whatever the sender happened to be holding at snapshot time"
        )
        assert net(full, 2) == pytest.approx(net(empty, 2))
