"""Does running the planner against the game repeatedly ever STOP writing?

The whole-day design is sold on one economic claim: provision once, then the
account runs itself for ~0 requests a day. That is a convergence property, and
nothing tested it -- every existing test checks a single run against a
hand-built marketplace. A reconciler can pass all of those and still churn
forever, disabling and recreating the same rows on every pass, and the operator
would only discover it by watching requests bleed daily.

So this drives the real endpoint against the fake game repeatedly, letting the
fake's marketplace carry forward between runs exactly as a real one would, and
asserts two things:

  * it reaches a fixed point -- a run that writes nothing at all;
  * once there, further runs stay silent (idempotence), which is the property
    the "~0 requests a day" claim actually rests on.

The fake service is a real state machine, not a stub: create_route fans a route
into its 24/N daily rows with the departure minutes the game stamps,
disable_routes flips rows inactive, delete_routes removes them. That is what
makes iterating it meaningful.
"""

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.web.routes import distribution as dist_module

from .test_distribution_execute import _execute, _FakeLiveSvc, _patch
from .test_whole_day_execute import (
    ACCOUNTS,
    DAY,
    NIGHT,
    _account,
    _segments_body,
    _sheet_row,
)

# Enough passes to converge and then prove silence, but bounded: a
# non-converging reconciler must fail the test rather than loop forever.
MAX_PASSES = 8


def _writes(svc):
    """Every mutating call the run made, as one comparable count.

    `updated` belongs here and was missed in the first version of this file:
    without it, a cargo correction that rewrites the same routes every single
    run reads as CONVERGED -- the exact churn the drift test below exists to
    catch, passing vacuously. Any list the fake appends a mutation to has to be
    in this sum.
    """
    return (
        len(svc.created)
        + len(svc.disabled)
        + len(svc.enabled)
        + len(svc.deleted)
        + len(svc.updated)
    )


def _clear_writes(svc):
    svc.created.clear()
    svc.disabled.clear()
    svc.enabled.clear()
    svc.deleted.clear()
    svc.updated.clear()
    svc.listed.clear()


def _drive_to_fixed_point(svc, body_factory, accounts, max_passes=MAX_PASSES):
    """Run execute against `svc` until a pass writes nothing.

    Returns (passes_until_quiet, per_pass_write_counts). The marketplace inside
    `svc` persists across passes, so pass N sees exactly what pass N-1 left --
    which is the situation a daily run is actually in.
    """

    async def _plan(_body, dispatch_window=None):
        return accounts[tuple(dispatch_window)]

    counts = []
    for attempt in range(1, max_passes + 1):
        _clear_writes(svc)
        with (
            _patch(dist_module, "_plan_account", _plan),
            _patch(dist_module.random, "shuffle", lambda seq: None),
        ):
            _execute(body_factory(), svc=svc)
        counts.append(_writes(svc))
        if counts[-1] == 0:
            return attempt, counts
    return None, counts


class TestItStopsWritingOnceTheGameMatchesThePlan:
    def test_a_whole_day_run_reaches_a_fixed_point_from_an_empty_account(self):
        """The headline claim: from nothing, provisioning settles and then goes
        quiet. If this fails the account pays requests every single day forever,
        which is the opposite of what the whole-day design is for."""
        svc = _FakeLiveSvc()

        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, (
            f"never stopped writing in {MAX_PASSES} passes; writes per pass were "
            f"{counts} -- the reconciler is churning, so a daily run never becomes free"
        )
        assert passes <= 3, (
            f"took {passes} passes to settle (writes per pass {counts}); one pass to "
            f"create and one to confirm silence is the design, so anything more means "
            f"a route is being rebuilt for a reason worth naming"
        )

    def test_further_runs_after_convergence_write_nothing_at_all(self):
        """Idempotence, stated separately: reaching quiet once could be luck if
        the next pass starts churning again."""
        svc = _FakeLiveSvc()
        passes, _ = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)
        assert passes is not None

        for extra in range(3):
            _clear_writes(svc)
            _drive_to_fixed_point(svc, _segments_body, ACCOUNTS, max_passes=1)
            assert _writes(svc) == 0, (
                f"pass {extra + 1} after convergence wrote again: created="
                f"{len(svc.created)} disabled={len(svc.disabled)} "
                f"enabled={len(svc.enabled)} deleted={len(svc.deleted)} "
                f"updated={len(svc.updated)}"
            )

    def test_a_converged_account_still_costs_reads(self):
        """The honest other half. Convergence means no WRITES, not no requests:
        the run must still read each village to know nothing is stale. If this
        ever showed zero reads the reconciler would be trusting instead of
        checking, which is the failure the whole design refuses."""
        svc = _FakeLiveSvc()
        _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        _clear_writes(svc)
        _drive_to_fixed_point(svc, _segments_body, ACCOUNTS, max_passes=1)

        assert svc.listed, "a quiet run must still verify by reading the marketplace"


class TestItConvergesFromAMessyStartingPoint:
    """An account is never empty in practice -- it holds whatever last week's
    plan left, plus routes made by hand. Convergence from a clean slate is the
    easy case."""

    def test_it_converges_when_the_account_holds_the_wrong_schedule(self):
        from .test_whole_day_execute import _fanned

        # A route to the right destination on a cycle no profile wants: the
        # reconciler must replace it and then settle, not oscillate between
        # "mismatched, recreate" and "mismatched again".
        svc = _FakeLiveSvc(
            existing={20003: _fanned(20011, 10, 0, cycle_hours=3, dispatch_minute=90, start_id=500)}
        )

        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, (
            f"never settled from a wrong-schedule start; writes per pass {counts}"
        )

    def test_it_converges_when_the_account_holds_routes_no_profile_wants(self):
        from travian_api.services.trade_route_service import ExistingRoute

        svc = _FakeLiveSvc(
            existing={20003: [ExistingRoute(700 + i, 77777, 99, 98, active=True) for i in range(4)]}
        )

        passes, counts = _drive_to_fixed_point(svc, _segments_body, ACCOUNTS)

        assert passes is not None, f"never settled with stale routes present; {counts}"

    def test_it_converges_with_a_protected_route_it_must_never_touch(self):
        """A protected route is permanently 'wrong' from the plan's point of
        view. Convergence must not depend on being allowed to fix everything --
        otherwise one hand-made route makes every future run write forever."""
        from travian_api.services.trade_route_service import ExistingRoute

        svc = _FakeLiveSvc(existing={20003: [ExistingRoute(800, 77777, 99, 98, active=True)]})

        passes, counts = _drive_to_fixed_point(
            svc,
            lambda: _segments_body(protect_destinations=["99|98"]),
            ACCOUNTS,
        )

        assert passes is not None, (
            f"a protected route prevented convergence; writes per pass {counts} -- "
            f"every daily run would keep writing because of one route it may not fix"
        )
        assert not any((99, 98) in coords for _vid, coords in svc.disabled), (
            "the protected route was disabled anyway"
        )


class TestConvergenceIsNotSpecificToOneShape:
    """Two profiles sharing a destination is the case the union exists for, but
    convergence has to hold for the ordinary shapes too."""

    @pytest.mark.parametrize(
        "day_cycle,night_cycle",
        [(4, 1), (8, 2), (1, 1), (2, 4)],
    )
    def test_it_converges_across_cycle_combinations(self, day_cycle, night_cycle):
        day = _account([_sheet_row(20003, 20011, cycle=day_cycle, minute=8 * 60, crop=4000)])
        night = _account(
            [
                _sheet_row(20003, 20011, cycle=night_cycle, minute=23 * 60, crop=1500),
                _sheet_row(20003, 20012, cycle=2, minute=23 * 60, crop=2000),
            ]
        )
        accounts = {tuple(DAY): day, tuple(NIGHT): night}
        svc = _FakeLiveSvc()

        passes, counts = _drive_to_fixed_point(svc, _segments_body, accounts)

        assert passes is not None, (
            f"Day {day_cycle}h + Night {night_cycle}h never settled; writes per pass {counts}"
        )

    def test_a_single_profile_run_also_converges(self):
        """The union is the general case, but plenty of runs are one profile.
        Regression cover for the pre-union path, which is what most existing
        tests exercise a single pass of."""
        night = ACCOUNTS[tuple(NIGHT)]
        svc = _FakeLiveSvc()

        def _single():
            return _segments_body(
                segments=[{"name": "Night", "window": list(NIGHT), "allocations": {}}]
            )

        passes, counts = _drive_to_fixed_point(svc, _single, {tuple(NIGHT): night})

        assert passes is not None, f"a single-profile run never settled; {counts}"


class TestTheCargoCorrectionAlsoSettles:
    def test_drift_correction_does_not_rewrite_forever(self):
        """update_drifted rewrites cargo on live routes. If the comparison and
        the write disagree even slightly -- rounding, per-resource zeroes, the
        wrong rows attributed -- it rewrites the same routes every run, which is
        a paced PUT per route per day for nothing."""
        from .test_whole_day_execute import _day_rows, _night_other_rows, _night_rows

        svc = _FakeLiveSvc(
            existing={
                20003: [
                    *_day_rows(cargo={Resource.CROP: 9999}),
                    *_night_rows(cargo={Resource.CROP: 9999}),
                    *_night_other_rows(),
                ]
            }
        )

        passes, counts = _drive_to_fixed_point(
            svc,
            lambda: _segments_body(update_drifted=True),
            ACCOUNTS,
        )

        assert passes is not None, (
            f"cargo correction never settled; writes per pass {counts} -- the same "
            f"routes are being rewritten every run"
        )
