"""Whole-day execution: every profile's routes in one pass, reconciled as a union.

The operator runs a Day profile (07:00-23:00) and a Night one (23:00-07:00).
Executing them separately means recreating route sets twice a day forever;
executed as a union, both row sets live in the game at once -- disjoint by
departure minute, which the per-profile fan-out prune guarantees -- and the
account runs the whole day with no switching and near-zero daily requests.

What the union must get right, each pinned here because each was a real way to
get it wrong (three were called out by peer review of the design before a line
was written):

* expected rows per destination are the MERGED minute multisets -- a dict
  comprehension keyed by destination silently kept only the last profile's
  minutes, so any destination served by both profiles would churn
  disable+recreate on every run, forever;
* cargo drift is attributed by minute -- game rows carry no profile tag, and
  Day's batch must never be stamped onto Night's rows;
* each route is trimmed and budgeted against ITS OWN window, and a trim must
  keep its hands off rows the other profile's create just made.
"""

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.optimizer import VillageState
from travian_api.web.routes import distribution as dist_module
from travian_api.web.routes.distribution import DayCheckRequest, ExecuteRequest, post_day_check

from .test_distribution_execute import (
    _MINUTES_PER_DAY,
    ExistingRoute,
    SheetRow,
    _execute,
    _FakeLiveSvc,
    _fanned,
    _patch,
)

NIGHT = (23 * 60, 7 * 60)
DAY = (7 * 60, 23 * 60)


def _in_window(minute, window):
    start, end = window
    return (start <= minute < end) if start <= end else (minute >= start or minute < end)


def _pruned(rows, window):
    """The rows the game holds AFTER the trim: only in-window departures."""
    return [r for r in rows if _in_window((r.departure_at % 86400) // 60, window)]


def _sheet_row(origin, destination, *, cycle, minute, crop=2400):
    return SheetRow(
        origin=origin,
        destination=destination,
        cargo={Resource.CROP: crop},
        cycle_hours=cycle,
        dispatch_minute=minute,
        arrival_minute=0,
        merchants=1,
    )


def _account(rows):
    plan = SimpleNamespace(
        is_feasible=True, warnings=(), rows=tuple(rows), merchants_committed={20003: len(rows)}
    )
    return SimpleNamespace(
        plan=plan,
        # `_PlannedAccount` carries the village models, and the whole-day
        # merchant boundary reads each village's budget off them -- the one
        # place the fleet, the reserve and the operator's cap are composed.
        villages={
            20003: VillageState(
                village_id=20003, x=0, y=0, merchant_count=20, trade_office_level=10
            ),
            20011: VillageState(
                village_id=20011, x=10, y=0, merchant_count=20, trade_office_level=0
            ),
            20012: VillageState(
                village_id=20012, x=0, y=10, merchant_count=20, trade_office_level=0
            ),
        },
        names={20003: "03", 20011: "11", 20012: "12"},
        coords={20003: (0, 0), 20011: (10, 0), 20012: (0, 10)},
        warnings=[],
        dropped_allocations=[],
    )


# Day ships to 11 on a 4h cycle from 08:00; Night ships hourly to the SAME
# destination from 23:00 -- the shared-destination case is the hard one.
DAY_ROUTE = _sheet_row(20003, 20011, cycle=4, minute=8 * 60, crop=4000)
NIGHT_ROUTE = _sheet_row(20003, 20011, cycle=1, minute=23 * 60, crop=1500)
NIGHT_OTHER = _sheet_row(20003, 20012, cycle=2, minute=23 * 60, crop=2000)

ACCOUNTS = {tuple(DAY): _account([DAY_ROUTE]), tuple(NIGHT): _account([NIGHT_ROUTE, NIGHT_OTHER])}


def _segments_body(**extra):
    payload = {
        "snapshot": [
            {
                "village_id": 20011,
                "name": "11",
                "x": 10,
                "y": 0,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 2000,
                "clay_per_hour": 1000,
                "iron_per_hour": 1000,
                "crop_per_hour": 2000,
                "lumber_stock": 10000,
                "clay_stock": 10000,
                "iron_stock": 10000,
                "crop_stock": 10000,
                "warehouse_capacity": 80000,
                "granary_capacity": 80000,
            },
            {
                "village_id": 20003,
                "name": "03",
                "x": 0,
                "y": 0,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 2000,
                "clay_per_hour": 1000,
                "iron_per_hour": 1000,
                "crop_per_hour": 9000,
                "lumber_stock": 10000,
                "clay_stock": 10000,
                "iron_stock": 10000,
                "crop_stock": 10000,
                "warehouse_capacity": 80000,
                "granary_capacity": 80000,
            },
        ],
        "config": [{"village_id": 20003, "trade_office_level": 10}],
        "allocations": {},
        "prune_to_window": True,
        "dry_run": False,
        "disable_existing": True,
        "max_routes_per_run": 50,
        "segments": [
            {"name": "Day", "window": list(DAY), "allocations": {}},
            {"name": "Night", "window": list(NIGHT), "allocations": {}},
        ],
        **extra,
    }
    return ExecuteRequest.model_validate(payload)


def _run_union(svc, body=None, accounts=ACCOUNTS):
    """Execute with segments, each segment planned by its window's account."""

    async def _plan(_body, dispatch_window=None, overnight=None, night_end=None):
        return accounts[tuple(dispatch_window)]

    with (
        _patch(dist_module, "_plan_account", _plan),
        _patch(dist_module.random, "shuffle", lambda seq: None),
    ):
        return _execute(body or _segments_body(), svc=svc)


def _day_rows(start_id=100, cargo=None, active=True):
    return _pruned(
        _fanned(
            20011,
            10,
            0,
            cycle_hours=4,
            dispatch_minute=8 * 60,
            start_id=start_id,
            active=active,
            cargo=cargo or {Resource.CROP: 4000},
        ),
        DAY,
    )


def _night_rows(start_id=200, cargo=None, active=True):
    return _pruned(
        _fanned(
            20011,
            10,
            0,
            cycle_hours=1,
            dispatch_minute=23 * 60,
            start_id=start_id,
            active=active,
            cargo=cargo or {Resource.CROP: 1500},
        ),
        NIGHT,
    )


def _night_other_rows(start_id=300):
    return _pruned(
        _fanned(
            20012,
            0,
            10,
            cycle_hours=2,
            dispatch_minute=23 * 60,
            start_id=start_id,
            cargo={Resource.CROP: 2000},
        ),
        NIGHT,
    )


class TestTheUnionIsOneTruthPerVillage:
    def test_a_fully_provisioned_day_and_night_is_left_untouched(self):
        """THE test. Both profiles' rows live -- including two routes to the
        same destination -- and a whole-day run changes nothing. This is what
        fails when expected rows are keyed by destination and overwritten: the
        shared destination mismatches forever and churns on every run."""
        svc = _FakeLiveSvc(existing={20003: [*_day_rows(), *_night_rows(), *_night_other_rows()]})

        res = _run_union(svc)

        assert svc.disabled == [], "a union-complete village must not be churned"
        assert svc.created == [], "nor recreated"
        assert {a.status for a in res.actions} == {"skipped"}

    def test_one_pass_creates_both_profiles_routes(self):
        svc = _FakeLiveSvc()  # empty marketplace

        res = _run_union(svc)

        created = {(r.dest_village_id, r.cycle_hours) for r in svc.created}
        assert created == {(20011, 4), (20011, 1), (20012, 2)}, (
            "one visit must create the Day route AND both Night routes"
        )
        assert len(svc.listed) == 1, "one marketplace read serves every profile"
        segments = {a.segment for a in res.actions}
        assert segments == {"Day", "Night"}, "actions carry their profile label"

    def test_each_create_is_trimmed_to_its_own_window(self):
        """The Day route loses its night-departing rows and vice versa -- and
        neither trim touches the rows the OTHER profile's create just made."""
        svc = _FakeLiveSvc()

        _run_union(svc)

        deleted = {rid for _vid, ids in svc.deleted for rid in ids}
        surviving = [
            r for rows in svc._existing.values() for r in rows if r.route_id not in deleted
        ]
        for row in surviving:
            minute = (row.departure_at % 86400) // 60
            if row.dest_village_id == 20012:
                assert _in_window(minute, NIGHT), (
                    f"row at minute {minute} to 20012 survived outside the Night window"
                )
        # The shared destination holds BOTH profiles' surviving rows: 3 Day
        # (08:00, 12:00, 16:00 -- 20:00 wait: 4h from 480 = 480,720.. compute
        # in assert) + 8 Night hourly rows.
        shared = [r for r in surviving if r.dest_village_id == 20011]
        minutes = sorted((r.departure_at % 86400) // 60 for r in shared)
        day_expected = sorted(
            m
            for m in ((8 * 60 + k * 240) % _MINUTES_PER_DAY for k in range(6))
            if _in_window(m, DAY)
        )
        night_expected = sorted(
            m
            for m in ((23 * 60 + k * 60) % _MINUTES_PER_DAY for k in range(24))
            if _in_window(m, NIGHT)
        )
        assert minutes == sorted([*day_expected, *night_expected]), (
            f"survivors {minutes} != Day {day_expected} + Night {night_expected}: "
            f"a trim deleted the other profile's rows or kept its own strays"
        )

    def test_a_fully_disabled_shared_destination_is_re_enabled_not_rebuilt(self):
        """Both profiles' rows to the shared destination exist but are OFF.
        One re-enable restores the whole union. This is where the minute
        multisets must be MERGED per destination: keyed-and-overwritten,
        the dormant set compares against only the last profile's minutes,
        never matches, and the key churns disable+recreate instead."""
        svc = _FakeLiveSvc(
            existing={
                20003: [
                    *_day_rows(active=False),
                    *_night_rows(active=False),
                    *_night_other_rows(),
                ]
            }
        )

        _run_union(svc)

        assert any(vid == 20003 for vid, _ in svc.enabled), "the dormant union must be re-enabled"
        assert svc.created == [], "never rebuilt on top of rows one PUT restores"
        assert svc.disabled == []

    def test_a_row_in_neither_profiles_schedule_is_stale(self):
        stray = _fanned(20011, 10, 0, cycle_hours=8, dispatch_minute=100, start_id=900)
        svc = _FakeLiveSvc(
            existing={20003: [*_day_rows(), *_night_rows(), *_night_other_rows(), *stray]}
        )

        _run_union(svc)

        assert svc.disabled, "rows on a schedule no profile wants must be disabled"


class TestDriftIsAttributedByMinute:
    def test_only_the_drifted_profiles_rows_are_rewritten_with_its_own_cargo(self):
        """Night's rows drifted; Day's are correct. Only the Night rows may be
        rewritten, and with NIGHT's batch -- stamping Day's 4,000 across the
        shared destination is exactly the corruption minute-attribution
        prevents."""
        svc = _FakeLiveSvc(
            existing={
                20003: [
                    *_day_rows(cargo={Resource.CROP: 4000}),  # correct
                    *_night_rows(cargo={Resource.CROP: 9999}),  # drifted
                    *_night_other_rows(),
                ]
            }
        )

        res = _run_union(svc, body=_segments_body(update_drifted=True))

        assert len(svc.updated) == 1, f"exactly one update expected, got {svc.updated}"
        _vid, row_ids, cargo = svc.updated[0]
        night_ids = {r.route_id for r in _night_rows()}
        assert set(row_ids) == night_ids, (
            f"updated rows {sorted(row_ids)} are not exactly the Night rows "
            f"{sorted(night_ids)} -- drift leaked across profiles"
        )
        assert cargo[Resource.CROP] == 1500, "and the batch written is Night's, not Day's"
        assert not any("Day" in u for u in res.updates), "Day must not be reported as updated"


class TestTheUnionRefusesWhatItCannotKeepStraight:
    def test_overlapping_windows_are_refused(self):
        with pytest.raises(ValidationError, match="overlap"):
            _segments_body(
                segments=[
                    {"name": "Day", "window": [420, 1400], "allocations": {}},
                    {"name": "Night", "window": [1380, 420], "allocations": {}},
                ]
            )

    def test_segments_without_pruning_are_refused(self):
        with pytest.raises(ValidationError, match="prune_to_window"):
            _segments_body(prune_to_window=False)

    def test_a_top_level_window_is_refused(self):
        with pytest.raises(ValidationError, match="dispatch_window"):
            _segments_body(dispatch_window=[1380, 420])

    def test_an_infeasible_segment_is_named_in_the_refusal(self):
        bad_night = _account([NIGHT_ROUTE])
        bad_night.plan.is_feasible = False
        bad_night.plan.over_budget = ()
        bad_night.plan.shortfalls = ()
        bad_night.plan.over_allocated = ()
        # Section 7's NPC refusal is one of the reasons `blockers()` reports, so
        # a stub plan has to answer it like the real one does.
        bad_night.plan.npc_short = ()
        accounts = {tuple(DAY): ACCOUNTS[tuple(DAY)], tuple(NIGHT): bad_night}

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            _run_union(_FakeLiveSvc(), accounts=accounts)

        assert caught.value.status_code == 422
        assert "Night" in caught.value.detail, "the blocking profile must be named"


class TestThePreviewBillsTheRun:
    def test_a_dry_run_forecasts_its_requests(self):
        body = _segments_body(dry_run=True)

        async def _plan(_body, dispatch_window=None, overnight=None, night_end=None):
            return ACCOUNTS[tuple(dispatch_window)]

        with _patch(dist_module, "_plan_account", _plan):
            res = _execute(body, connected=False)

        forecast = res.requests_forecast
        assert forecast["creates"] == 3
        assert forecast["marketplace_reads"] >= 1
        assert forecast["trim_deletes"] == 1, "one origin, one batched trim"
        assert forecast["estimated_total"] <= forecast["estimated_total_max"]


class TestOneVisitReportsItselfHonestly:
    def test_shared_destination_actions_each_count_their_own_rows(self):
        """The verify counts rows per (destination, minute), not per key: a
        key-level count handed Day's and Night's action the SAME twelve rows,
        so the response claimed 24 rows where 12 exist."""
        svc = _FakeLiveSvc(existing={20003: _night_other_rows()})

        res = _run_union(svc)

        by_route = {
            (a.destination, a.cycle_hours): a.observed_game_rows
            for a in res.actions
            if a.status == "created"
        }
        # Day 4h -> 6 pre-trim rows; Night 1h -> 24 pre-trim rows, same key.
        assert by_route[(20011, 4)] == 6, by_route
        assert by_route[(20011, 1)] == 24, by_route

    def test_the_provisioning_sweep_equals_an_ordinary_run_plus_the_sweep(self):
        """Whole-day sweep-create parity: reconcile_all_origins with a create
        budget must build exactly what an ordinary capped run builds, while
        still sweeping villages the plan does not ship from."""
        ordinary = _FakeLiveSvc()
        _run_union(ordinary)

        sweeping = _FakeLiveSvc(
            existing={
                # A village the plan does not use, holding a stale route the
                # sweep must switch off while it creates everywhere else.
                20011: [ExistingRoute(9000, 77777, 99, 98, active=True)]
            }
        )
        body = _segments_body(reconcile_all_origins=True, max_origins_per_run=0)
        res = _run_union(sweeping, body=body)

        assert {(r.dest_village_id, r.cycle_hours) for r in sweeping.created} == {
            (r.dest_village_id, r.cycle_hours) for r in ordinary.created
        }, "the sweep must create the same blueprint an ordinary run does"
        assert any(vid == 20011 for vid, _ in sweeping.disabled), (
            "and still disable the stale route on the village the plan skips"
        )
        assert sorted(res.swept_origins) == [20003, 20011], res.swept_origins


# -- One latency target per segment, whichever endpoint plans it --------------


def _planned_latency(run):
    """The ``max_latency_hours`` each handler actually plans each window with.

    Spied on the ``PlannerConfig`` the handler builds rather than inferred from
    the response, because that object IS the answer: /day-check returns
    trajectories and /execute returns route actions, so no shared field of the
    two responses carries the target either of them planned against.
    """
    seen: dict[tuple[int, int] | None, float | None] = {}
    real = dist_module.PlannerConfig

    def spy(**kwargs):
        seen[kwargs.get("dispatch_window")] = kwargs.get("max_latency_hours")
        return real(**kwargs)

    with _patch(dist_module, "PlannerConfig", spy):
        run()
    return seen


def _crop_to_11():
    return {
        "crop": {
            "20003": {"mode": "absolute", "value": 0},
            "20011": {"mode": "remainder"},
        }
    }


def _shared_payload():
    """One body both endpoints accept, with a real route in it to plan.

    03 makes 9,000 crop an hour and keeps none of it; 11 is the remainder and
    takes the lot, ten fields away. Day runs 07:00-23:00 (16h) and Night
    23:00-07:00 (8h), which is the operator's own pair.
    """
    return {
        "snapshot": [
            {
                "village_id": 20003,
                "name": "03",
                "x": 0,
                "y": 0,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 0,
                "clay_per_hour": 0,
                "iron_per_hour": 0,
                "crop_per_hour": 9000,
                "crop_stock": 10000,
                "granary_capacity": 400000,
                "warehouse_capacity": 400000,
            },
            {
                "village_id": 20011,
                "name": "11",
                "x": 10,
                "y": 0,
                "merchants_total": 20,
                "merchants_free": 20,
                "lumber_per_hour": 0,
                "clay_per_hour": 0,
                "iron_per_hour": 0,
                "crop_per_hour": 0,
                "crop_stock": 10000,
                "granary_capacity": 400000,
                "warehouse_capacity": 400000,
            },
        ],
        "config": [{"village_id": 20003, "trade_office_level": 10}],
        "prune_to_window": True,
        "segments": [
            {"name": "Day", "window": list(DAY), "allocations": _crop_to_11()},
            {"name": "Night", "window": list(NIGHT), "allocations": _crop_to_11()},
        ],
    }


def _executed_latency(payload):
    return _planned_latency(
        lambda: _execute(
            ExecuteRequest.model_validate({**payload, "dry_run": True}), connected=False
        )
    )


class TestOneLatencyTargetPerSegment:
    """/execute writes the plan /day-check was reviewed as, latency included.

    Both handlers plan every segment through `_plan_account`, and the latency
    pass buys shorter cycles (smaller batches, more merchants in flight) to meet
    a target. Give the two endpoints different targets for one window and they
    build different route sets from one body: the operator reads a clean day
    picture off short cycles and small batches, and the write lands long cycles
    and big batches into the same stores -- the burst overflow nothing
    simulated.
    """

    def test_execute_and_day_check_plan_a_segment_against_the_same_target(self):
        payload = _shared_payload()

        checked = _planned_latency(
            lambda: asyncio.run(
                post_day_check(DayCheckRequest.model_validate(payload), SimpleNamespace(id=1))
            )
        )
        executed = _executed_latency(payload)

        assert executed == checked, (
            "the endpoint that WRITES must plan each profile against the target "
            "the endpoint the operator REVIEWS with used"
        )

    def test_a_long_window_keeps_the_standing_target_rather_than_loosening_to_it(self):
        """A 16h day window taken as the target disables the latency objective
        for the whole day: no route can miss it, so the pass never fires."""
        assert _executed_latency(_shared_payload())[DAY] == 2.0

    def test_a_window_shorter_than_the_standing_target_tightens_it(self):
        """The other direction is real: a delivery cannot be two hours late
        inside a one-hour profile, so the window binds where it is tighter."""
        payload = _shared_payload()
        payload["segments"] = [
            {"name": "Hour", "window": [420, 480], "allocations": _crop_to_11()},
            {"name": "Rest", "window": [480, 420], "allocations": _crop_to_11()},
        ]

        assert _executed_latency(payload)[(420, 480)] == 1.0


# -- The profiles together honour the per-village merchant reserve -----------


def _with_fleet(payload, merchants, reserve):
    """The same body, at a fleet small enough for the boundary sum to bind."""
    for village in payload["snapshot"]:
        village["merchants_total"] = merchants
        village["merchants_free"] = merchants
    payload["merchant_reserve"] = reserve
    return payload


class TestTheProfilesTogetherHonourTheMerchantReserve:
    """Section I.3.5: `sum(pool) <= merchants_total - reserve`, per village.

    Each profile fits `merchant_budget(reserve)` on its own -- the optimizer
    refuses otherwise -- but a round trip started late in one window is still in
    the air when the next begins, so the sum across profiles is what a village
    actually needs. The reserve is precisely what a boundary overlap eats, and
    section VII.6 asks for it to be generous rather than tight: defensive calls
    come at random hours, and a village with zero idle merchants cannot respond
    to anything by hand.

    At 7 merchants and a reserve of 2 the pair commits 4 + 3 = 7: the whole
    fleet, with nothing left for 01:00. Measured against the FLEET that is
    `7 > 7`, which is false, so the composite passed in silence.
    """

    def _payload(self):
        return _with_fleet(_shared_payload(), 7, 2)

    def _boundary(self, warnings):
        return [w for w in warnings if "together commit" in w]

    def test_a_composite_that_eats_the_reserve_is_reported(self):
        res = _execute(
            ExecuteRequest.model_validate({**self._payload(), "dry_run": True}), connected=False
        )

        (warning,) = self._boundary(res.warnings)
        assert "03:" in warning, warning
        # The budget, not the fleet: 7 in the fleet less the 2 held back.
        assert "7 merchants" in warning and "5" in warning, warning

    def test_the_endpoint_the_operator_reviews_with_reports_it_too(self):
        """The check lived only in /execute, so the day picture the operator
        signs off was silent about it -- and /execute builds the same
        per-segment plans, so there was never a reason for the two to differ."""
        res = asyncio.run(
            post_day_check(DayCheckRequest.model_validate(self._payload()), SimpleNamespace(id=1))
        )

        assert self._boundary(res.warnings), res.warnings

    def test_both_endpoints_word_it_identically(self):
        executed = _execute(
            ExecuteRequest.model_validate({**self._payload(), "dry_run": True}), connected=False
        )
        checked = asyncio.run(
            post_day_check(DayCheckRequest.model_validate(self._payload()), SimpleNamespace(id=1))
        )

        assert self._boundary(executed.warnings) == self._boundary(checked.warnings)

    def test_a_fleet_with_room_for_the_reserve_says_nothing(self):
        res = _execute(
            ExecuteRequest.model_validate(
                {**_with_fleet(_shared_payload(), 20, 2), "dry_run": True}
            ),
            connected=False,
        )

        assert not self._boundary(res.warnings), res.warnings


# -- A profile name is a key, so it has to be unique -------------------------


class TestTwoProfilesCannotShareAName:
    """The section 6 state rules look a stock up by profile NAME.

    `simulate_profile_cycle` records the hand-over stock as
    `openings[starts[minute]]`, keyed by name, and `morning_floor_shortfalls`
    and `pre_night_overfills` read it back the same way. Two halves of a split
    night both called "Night" collapse to one entry -- right by accident for a
    23:00/00:00 split, where the second write is the opening minute anyway, and
    wrong for a 22:00/23:00 one, where the 25% baseline is then read at 23:00
    for a half that opened at 22:00. The breach lines say "during Night" for
    both of them too, so a duplicate name is unreadable for the operator as
    well as ambiguous for the lookup.

    Refused at the schema rather than keyed around, because a name the operator
    cannot tell apart in the output is not a name.
    """

    def test_the_day_check_refuses_two_profiles_with_one_name(self):
        with pytest.raises(ValidationError, match="name"):
            DayCheckRequest.model_validate(
                {
                    "prune_to_window": True,
                    **_shared_payload(),
                    "segments": [
                        {"name": "Night", "window": [22 * 60, 23 * 60], "allocations": {}},
                        {"name": "Night", "window": [23 * 60, 7 * 60], "allocations": {}},
                        {"name": "Day", "window": [7 * 60, 22 * 60], "allocations": {}},
                    ],
                }
            )

    def test_execute_refuses_them_too(self):
        with pytest.raises(ValidationError, match="name"):
            _segments_body(
                segments=[
                    {"name": "Night", "window": [22 * 60, 23 * 60], "allocations": {}},
                    {"name": "Night", "window": [23 * 60, 7 * 60], "allocations": {}},
                    {"name": "Day", "window": [7 * 60, 22 * 60], "allocations": {}},
                ]
            )

    def test_distinct_names_are_untouched(self):
        body = _segments_body(
            segments=[
                {"name": "Night before midnight", "window": [23 * 60, 0], "allocations": {}},
                {"name": "Night after midnight", "window": [0, 7 * 60], "allocations": {}},
                {"name": "Day", "window": [7 * 60, 23 * 60], "allocations": {}},
            ]
        )

        assert [s.name for s in body.segments] == [
            "Night before midnight",
            "Night after midnight",
            "Day",
        ]


class TestATwoRouteDestinationCannotBeRebuiltAtACapOfOne:
    """The reserve funds a mismatched destination's rebuild whole or not at all.

    A whole-day union routinely wants TWO routes to one destination -- Day's and
    Night's -- so at `max_routes_per_run=1`, the documented safe first live
    test, `len(pairs) == 2 > cap == 1` for ever: six consecutive passes created
    nothing and the destination kept its wrong rows, while the refusal offered
    "raise the per-run budget or re-run". Re-running can never help.
    """

    def _mismatched(self):
        # Eight 3h rows where the union wants a 4h Day route and an hourly Night
        # one -- one destination, two routes, diverging.
        return {
            20003: _fanned(
                20011,
                10,
                0,
                cycle_hours=3,
                dispatch_minute=100,
                start_id=800000,
                cargo={Resource.CROP: 4000},
            )
        }

    def test_the_refusal_names_the_cap_that_would_work(self):
        svc = _FakeLiveSvc(existing=self._mismatched())
        res = _run_union(svc, body=_segments_body(max_routes_per_run=1))

        line = next(p for p in res.problems if "diverging" in p)
        assert "2 route(s) created together" in line, line
        assert "re-running will not help" in line, line
        assert "raise it to at least 2" in line, line

    def test_the_blocked_action_gives_the_same_remedy(self):
        svc = _FakeLiveSvc(existing=self._mismatched())
        res = _run_union(svc, body=_segments_body(max_routes_per_run=1))

        blocked = [a for a in res.actions if a.status == "blocked"]
        assert len(blocked) == 2, [(a.destination, a.status) for a in res.actions]
        assert all("re-running will not help" in a.detail for a in blocked), [
            a.detail for a in blocked
        ]

    def test_the_cap_it_names_actually_reconciles_the_destination(self):
        # The control: at 2, the destination this run switched off is rebuilt.
        svc = _FakeLiveSvc(existing=self._mismatched())
        _run_union(svc, body=_segments_body(max_routes_per_run=2))

        assert {(r.dest_village_id, r.cycle_hours) for r in svc.created} == {
            (20011, 4),
            (20011, 1),
        }
