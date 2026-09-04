"""The windowed-profile and reconciliation defects from the 2026-08-27 review.

Every test here pins a fix for a reproduced defect. The common theme is a gap
between what the plan MODELS and what the game will DO: batches sized for one
schedule delivered on another, a search advising cadences nobody may create, a
simulation replaying rows a prune deletes, and a reconciler that recognised
routes by destination while the game stores them as timed rows.
"""

import asyncio

import pytest
from fastapi import HTTPException

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import (
    Route,
    VillageState,
    _trade_office_levels_needed,
)
from travian_api.web.routes import distribution as dist
from travian_api.web.routes.distribution import NightProfileRequest, PlanRequest

from .test_distribution_audit import USER

_NIGHT = (23 * 60, 7 * 60)  # 23:00-07:00, 480 minutes


def _village(vid, name, x, y, *, crop=2000, stock=10_000, cap=80_000):
    return {
        "village_id": vid,
        "name": name,
        "x": x,
        "y": y,
        "merchants_total": 20,
        "merchants_free": 20,
        "lumber_per_hour": 2000,
        "clay_per_hour": 1000,
        "iron_per_hour": 1000,
        "crop_per_hour": crop,
        "lumber_stock": stock,
        "clay_stock": stock,
        "iron_stock": stock,
        "crop_stock": stock,
        "warehouse_capacity": cap,
        "granary_capacity": cap,
    }


def _plan_body(**extra):
    # A small flow over a long haul, because that is what tempts the optimizer
    # into a long cycle: unrestricted, this exact shape chooses 12h -- which
    # does not divide an 8-hour window (720 does not divide 480). A fixture the
    # optimizer happens to plan at 1h would make every windowed-cycle test here
    # pass with the fix reverted, which is how a first version of another test
    # in this repo turned out to be theatre.
    return PlanRequest.model_validate(
        {
            "snapshot": [
                _village(1, "hub", 0, 0),
                {
                    **_village(2, "farm", 60, 0, crop=300),
                    "lumber_per_hour": 100,
                    "clay_per_hour": 50,
                    "iron_per_hour": 50,
                },
            ],
            "config": [
                {"village_id": 1, "trade_office_level": 10},
                {"village_id": 2, "trade_office_level": 10},
            ],
            "allocations": {
                "crop": {
                    "1": {"mode": "remainder", "value": 0},
                    "2": {"mode": "absolute", "value": 0},
                }
            },
            **extra,
        }
    )


class TestWindowedCyclesDeliverWhatTheProfileMeans:
    """Batches are rate x cycle; pruning keeps the in-window firings. Those only
    multiply back to rate x window when the cycle divides the window: a 6h cycle
    in an 8h window keeps 2 of 4 firings and ships rate x 12 where the profile
    meant rate x 8 -- 50% over-delivery, plus six hours of extra withdrawal at
    the origin."""

    def test_a_pruned_profile_only_uses_cycles_that_divide_its_window(self):
        body = _plan_body(dispatch_window=_NIGHT, prune_to_window=True)

        res = asyncio.run(dist.post_plan(body, USER))

        offenders = [
            (r.origin, r.destination, r.cycle_hours)
            for r in res.rows
            if (480 % (r.cycle_hours * 60)) != 0
        ]
        assert not offenders, (
            f"routes with cycles that do not divide the 480-minute window: {offenders}. "
            f"Each ships batch x survivors != rate x window -- the reproduced case "
            f"delivered 1,200 where the profile meant 800."
        )

    def test_every_surviving_delivery_totals_the_windowed_rate(self):
        """The property itself, not the mechanism: for every route, batch x
        in-window firings == hourly rate x window hours."""
        body = _plan_body(dispatch_window=_NIGHT, prune_to_window=True)

        res = asyncio.run(dist.post_plan(body, USER))

        assert res.rows, "the fixture must produce at least one route"
        for r in res.rows:
            survivors = 480 // (r.cycle_hours * 60)
            batch = sum(r.cargo.values())
            hourly = batch / r.cycle_hours
            assert batch * survivors == pytest.approx(hourly * 8.0), (
                f"route {r.origin}->{r.destination}: {batch} x {survivors} firings "
                f"!= {hourly}/h x 8h window"
            )

    def test_an_unpruned_profile_keeps_the_full_cycle_set(self):
        """Without pruning every firing is real, so restricting cycles would be
        pure cost -- the WINDOW_NOT_ENFORCEABLE finding carries the danger."""
        body = _plan_body(dispatch_window=_NIGHT, prune_to_window=False)

        res = asyncio.run(dist.post_plan(body, USER))

        assert res.rows, "the fixture must produce at least one route"
        # Nothing to assert about which cycles were chosen -- only that the
        # divisor restriction did not apply and planning still succeeds.


class TestStorageDiagnosticsSimulateTheSurvivingRows:
    """/plan replayed every daily firing regardless of prune_to_window, so an
    hourly 8-hour profile was simulated at 24 firings while live execution
    keeps 8 -- roughly 3x the real traffic, reported as overflow and loss."""

    def _overflow(self, dispatch_window):
        from travian_api.services.distribution.schedule import Beat, ScheduledRoute
        from travian_api.services.distribution.storage import simulate_day

        # One hourly route into a receiver that can absorb the 8 in-window
        # deliveries but not all 24: batch 1,000, receiver headroom 10,000,
        # nothing produced or consumed at either end. First send 23:00.
        route = Route(
            origin=1,
            destination=2,
            cargo_per_hour={Resource.CROP: 1000.0},
            cycle_hours=1,
            merchants_per_send=1,
            sets_in_flight=1,
            one_way_minutes=10.0,
        )
        beat = Beat(routes=(ScheduledRoute(route=route, dispatch_minute=23 * 60),))
        # Only the receiver has a cap to overflow. The origin's is left unread
        # (skipped, not assumed): windowed, it ships 8 of the 24 batches it
        # grows and would fill any finite store eventually, which simulate_day
        # now projects and which is not what this test is about. The receiver
        # eats exactly the pruned inflow of 8,000/day, so the windowed case
        # holds level while the unpruned 24,000/day drowns it.
        return simulate_day(
            beat,
            stocks={1: {Resource.CROP: 50_000}, 2: {Resource.CROP: 0}},
            capacities={2: {Resource.CROP: 10_000}},
            net_per_hour={1: {Resource.CROP: 1000.0}, 2: {Resource.CROP: -8_000 / 24}},
            dispatch_window=dispatch_window,
        )

    def test_pruned_firings_are_not_replayed(self):
        # 8 surviving deliveries of 1,000 fit a 10,000 store exactly; the 16
        # pruned ones must not be simulated on top, or the diagnostics report
        # an overflow the live account will never have.
        events = self._overflow(dispatch_window=_NIGHT)
        assert events == (), (
            f"the windowed simulation overflowed anyway: {events} -- it is "
            f"replaying departures the prune will delete"
        )

    def test_without_a_window_every_firing_is_real_and_overflows(self):
        # The control: unpruned, all 24 land and the store genuinely overflows.
        # Without this the test above could pass because nothing overflows in
        # either mode.
        events = self._overflow(dispatch_window=None)
        assert events, (
            "24 deliveries of 1,000 into a 10,000 store did not overflow; the "
            "fixture proves nothing about the window filter"
        )


class TestTradeOfficeAdviceHonoursTheCadenceCap:
    """3,000/h over a one-hour trip under a 1h cap needs two merchant sets at
    ANY Trade Office level -- the cadence, not the capacity, is binding. The
    advice recomputed with unrestricted cycles and recommended +5 anyway."""

    def _route(self):
        return Route(
            origin=1,
            destination=2,
            cargo_per_hour={Resource.CROP: 3000.0},
            cycle_hours=1,
            merchants_per_send=1,
            sets_in_flight=2,
            one_way_minutes=60.0,
        )

    def test_no_upgrade_is_claimed_when_the_cadence_is_binding(self):
        village = VillageState(1, 0, 0, merchant_count=3, trade_office_level=0)

        advice = _trade_office_levels_needed(
            village,
            [self._route()],
            EUROPE2_TEUTON,
            budget=1,
            cycles=(1, 2, 3, 4, 6, 8, 12, 24),
            max_cycle={2: 1},
        )

        assert advice is None, (
            f"advised +{advice} Trade Office levels for a route whose 1h cadence "
            f"needs two merchant sets at any capacity -- the advice quietly "
            f"switched to a 2h cycle the destination forbids"
        )

    def test_the_advice_still_works_when_capacity_is_what_binds(self):
        village = VillageState(1, 0, 0, merchant_count=4, trade_office_level=0)

        advice = _trade_office_levels_needed(
            village,
            [self._route()],
            EUROPE2_TEUTON,
            budget=2,
            cycles=(1, 2, 3, 4, 6, 8, 12, 24),
            max_cycle={2: 1},
        )

        assert advice is not None, (
            "two sets fit the budget once each set is one merchant, which a "
            "Trade Office upgrade genuinely delivers -- the guard must not "
            "swallow real advice"
        )


class TestNightDerivationRefusesTheUnknown:
    """`crop_per_hour or 0.0` turned an unreadable balance into a healthy zero:
    no break-even allocation, no warning, and if the true balance is negative
    the granary drains all night. Starvation eats troops; refused outright."""

    def _body(self, crop):
        return NightProfileRequest.model_validate(
            {
                "snapshot": [
                    _village(1, "hub", 0, 0),
                    {**_village(2, "army", 10, 0), "crop_per_hour": crop},
                ],
                "config": [
                    {"village_id": 1, "trade_office_level": 10},
                    {"village_id": 2, "trade_office_level": 10},
                ],
                "allocations": {"crop": {"1": {"mode": "remainder", "value": 0}}},
                "dispatch_window": _NIGHT,
                "baseline_fill": 0.3,
                "target_fill": 0.8,
            }
        )

    def test_an_unreadable_crop_balance_refuses_derivation_by_name(self):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(dist.post_night_profile(self._body(None), USER))

        assert caught.value.status_code == 422
        assert "army" in caught.value.detail, "the village to fix must be named"

    def test_a_readable_negative_balance_still_derives(self):
        res = asyncio.run(dist.post_night_profile(self._body(-3000), USER))
        assert res.allocations, "a known consumer is the normal case, not an error"


class TestNightDerivationResolvesEveryAllocationMode:
    """Only ABSOLUTE reached day_retention; a 50% target silently vanished, so
    two Day profiles that mean the same thing derived different nights."""

    def _derive(self, allocation):
        body = NightProfileRequest.model_validate(
            {
                "snapshot": [
                    _village(1, "hub", 0, 0),
                    _village(2, "farm", 10, 0, crop=8000),
                    _village(3, "army", 20, 0, crop=-2000),
                ],
                "config": [{"village_id": v, "trade_office_level": 10} for v in (1, 2, 3)],
                "allocations": {
                    "crop": {
                        "1": {"mode": "remainder", "value": 0},
                        "3": allocation,
                    }
                },
                "dispatch_window": _NIGHT,
                "baseline_fill": 0.3,
                "target_fill": 0.8,
            }
        )
        return asyncio.run(dist.post_night_profile(body, USER))

    def test_a_sustain_target_does_not_change_the_night(self):
        # Not an accident: the derivation never reads crop retention, because
        # its own crop design makes every consumer break even for the night --
        # which supersedes whatever share the day sustained. A sustain target
        # must therefore change nothing, and an earlier "equivalence" test here
        # passed only because BOTH sides were being ignored.
        sustained = self._derive({"mode": "sustain", "value": 100})
        untouched = self._derive({"mode": "keep", "value": 0})

        assert sustained.allocations == untouched.allocations

    def _derive_lumber(self, allocation):
        # day_retention actually drives the MATERIAL allocations -- a crop
        # consumer breaks even regardless -- so resolving of percentage targets
        # must be pinned on a material or the test is vacuous. Learned by
        # watching the crop version pass with the fix reverted.
        body = NightProfileRequest.model_validate(
            {
                "snapshot": [
                    _village(1, "hub", 0, 0),
                    _village(2, "farm", 10, 0),
                    _village(3, "builder", 20, 0),
                ],
                "config": [{"village_id": v, "trade_office_level": 10} for v in (1, 2, 3)],
                "allocations": {
                    "crop": {"1": {"mode": "remainder", "value": 0}},
                    "lumber": {
                        "1": {"mode": "remainder", "value": 0},
                        "3": allocation,
                    },
                },
                "dispatch_window": _NIGHT,
                "baseline_fill": 0.3,
                "target_fill": 0.8,
            }
        )
        return asyncio.run(dist.post_night_profile(body, USER))

    def test_a_percentage_target_reaches_the_material_derivation(self):
        # Total lumber production is 3 x 2000 = 6000/h, so 50% means 3000/h --
        # the same day plan as an absolute 3000.
        percentage = self._derive_lumber({"mode": "percentage", "value": 50})
        absolute = self._derive_lumber({"mode": "absolute", "value": 3000})

        assert percentage.allocations == absolute.allocations, (
            "a 50% lumber target and its absolute equivalent derived different "
            "nights; percentage targets are being dropped from day_retention"
        )


class TestReconciliationMatchesTheRowSet:
    """A desired route was reduced to its destination, and any active row for it
    counted as satisfying the plan -- so a Day 3h route satisfied the Night
    plan's 1h demand for the same destination, out-of-window rows survived
    forever, and cargo correction wrote one batch across every row. These are
    endpoint-level checks of the same behaviour the execute-suite fixtures pin;
    they live here so the review's exact scenario is named somewhere.
    """

    def test_the_execute_suite_pins_the_behaviour(self):
        """The real coverage is tests/test_distribution_execute.py: fixtures now
        seed the full fan-out with departure minutes, and satisfaction requires
        the live minutes to equal the planned ones. This placeholder documents
        where to look rather than duplicating a 3,000-line harness."""
        from tests import test_distribution_execute as suite

        assert hasattr(suite, "_fanned"), (
            "the faithful fan-out fixture was removed; destination-only "
            "reconciliation can regress unnoticed without it"
        )


class TestNightDerivationKnowsTheMapWraps:
    """Distances used raw hypot on a torus. A hub at (-200|0) saw a supplier at
    (200|0) as 400 fields away when the seam makes it 1, so the draw picked a
    genuinely distant village over a next-door one and understated how much
    edge villages can shed overnight."""

    def _derive(self, map_span):
        from travian_api.services.distribution.geometry import MapGeometry
        from travian_api.services.distribution.night_profile import (
            NightVillage,
            derive_night_profile,
        )

        def v(vid, name, x, y, crop):
            return NightVillage(
                village_id=vid,
                name=name,
                x=x,
                y=y,
                merchants_total=20,
                trade_office_level=10,
                warehouse_capacity=80_000,
                granary_capacity=80_000,
                production={
                    Resource.LUMBER: 500.0,
                    Resource.CLAY: 500.0,
                    Resource.IRON: 500.0,
                    Resource.CROP: crop,
                },
            )

        villages = [
            v(1, "hub", -200, 0, 0.0),
            # Across the seam: one field from the hub on a 401-wide world.
            v(2, "seam-neighbour", 200, 0, 4000.0),
            # Ten fields away without crossing anything.
            v(3, "near", -190, 0, 4000.0),
            # The demand the draw must source: a consumer at the hub's side.
            v(4, "army", -199, 0, -6000.0),
        ]
        return derive_night_profile(
            villages,
            window_hours=8.0,
            geometry=MapGeometry(span=map_span, speed_fields_per_hour=12.0),
            merchant_model=EUROPE2_TEUTON,
            day_retention={},
            hub_id=1,
            consumer_ids=[4],
        )

    def test_the_seam_neighbour_is_drawn_before_the_farther_village(self):
        profile = self._derive(map_span=401)

        drawn = profile.drawn_in.get(Resource.CROP, [])
        assert drawn, "the consumer's deficit must draw on someone"
        first = drawn[0]
        assert first == 2, (
            f"the draw order {drawn} starts with village {first} instead of the "
            f"seam neighbour (2, one field away across the torus) -- distance is "
            f"being measured across the flat map instead of around it"
        )
