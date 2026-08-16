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
            ship_rates={1: {Resource.CROP: 10_000.0}},
        )
        night = ProfileSegment(
            name="Night",
            start_minute=23 * 60,
            end_minute=7 * 60,
            ship_rates={1: {Resource.CROP: 500.0}},
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
            ship_rates={1: {Resource.CROP: -6_000.0}},
        )
        night = ProfileSegment(
            name="Night",
            start_minute=20 * 60,
            end_minute=8 * 60,  # 12h at +2,000/h net = +24,000
            ship_rates={1: {Resource.CROP: -2_000.0}},
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
            ship_rates={1: {Resource.CROP: -5_000.0}},
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
            ship_rates={},
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
        segment = ProfileSegment(name="Day", start_minute=0, end_minute=720, ship_rates={})

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
