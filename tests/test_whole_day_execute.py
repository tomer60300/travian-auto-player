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

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from travian_api.services.distribution.allocation import Resource
from travian_api.web.routes import distribution as dist_module
from travian_api.web.routes.distribution import ExecuteRequest

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

    async def _plan(_body, dispatch_window=None):
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

        async def _plan(_body, dispatch_window=None):
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
