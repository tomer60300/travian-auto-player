"""A windowed profile has to be modelled the way the game will run it.

``dispatch_window`` exists so one allocation profile can own part of the day --
a night set that ships between 23:00 and 07:00 while a day set owns the rest.
The beat implements that by counting only the firings that land inside the
window, on the stated reasoning that "a firing outside the profile's hours is
not dispatched".

**The game does not work that way.** A Gold Club route carries `repeatEvery: N`
and nothing else; Travian fans that into 24/N daily departures across the whole
day. There is no "only repeat during these hours" setting to send. So a route
created for an 8-hour profile keeps firing for the other sixteen hours too.

The direction of the error is what makes it dangerous. The beat counts 8 of the
24 hourly firings and sizes the cargo so those 8 deliver the profile's
allocation; the game then performs all 24. The village receives three times what
the plan modelled -- and overflow, not shortfall, is what the operator wakes up
to.

The existing CYCLE_VS_WINDOW finding catches only the opposite, milder case: a
cycle LONGER than the window, which really does fire at most once inside it. The
common case -- a cycle shorter than the window -- was silent.

These tests pin the honest version: any route whose repeats spill outside the
profile's hours says so, loudly enough that the plan cannot read as clean.
"""

import math

import pytest

from travian_api.services.distribution.allocation import (
    Allocation,
    AllocationMode,
    Resource,
)
from travian_api.services.distribution.findings import Category, Severity
from travian_api.services.distribution.geometry import MapGeometry
from travian_api.services.distribution.merchants import EUROPE2_TEUTON
from travian_api.services.distribution.optimizer import VillageState
from travian_api.services.distribution.planner import PlannerConfig, assess, craft_plan

NIGHT = (23 * 60, 7 * 60)  # 23:00 -> 07:00, the operator's real night profile
GEOMETRY = MapGeometry(span=401, speed_fields_per_hour=12.0)


def _plan(dispatch_window, *, supply=5000.0, cycles=None):
    """One surplus village feeding one neighbour, planned into a window."""
    villages = {
        1: VillageState(village_id=1, x=0, y=0, merchant_count=20, name="src"),
        2: VillageState(village_id=2, x=2, y=0, merchant_count=20, name="dst"),
    }
    productions = {Resource.LUMBER: {1: supply, 2: 0.0}}
    allocations = {
        Resource.LUMBER: {
            1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
            2: Allocation(mode=AllocationMode.ABSOLUTE, value=supply),
        }
    }
    config = PlannerConfig(
        geometry=GEOMETRY,
        merchant_model=EUROPE2_TEUTON,
        dispatch_window=dispatch_window,
        **({"cycles": cycles} if cycles else {}),
    )
    return craft_plan(villages, productions, allocations, config)


def _window_findings(plan):
    return [f for f in plan.findings if f.category is Category.WINDOW_NOT_ENFORCEABLE]


class TestTheGameIgnoresTheProfilesHours:
    def test_a_cycle_shorter_than_the_window_is_reported(self):
        # The silent case, and the common one. Nothing warned about this before:
        # the beat counted the in-window firings and treated the rest as not
        # happening, which is the assumption the game contradicts.
        plan = _plan(NIGHT, cycles=(1,))
        assert plan.rows, "the plan must still be produced, just not called clean"
        assert _window_findings(plan), (
            "a 1h cycle in an 8h profile fires 24 times, not 8 — that must be said"
        )

    def test_the_finding_is_critical_not_advisory(self):
        # Three times the modelled cargo arriving at a village is not a note.
        finding = _window_findings(_plan(NIGHT, cycles=(1,)))[0]
        assert finding.severity is Severity.CRITICAL

    def test_it_names_how_many_firings_escape_the_window(self):
        message = _window_findings(_plan(NIGHT, cycles=(1,)))[0].message
        # 24 firings, 8 of which are inside a 480-minute window.
        assert "24" in message and "16" in message, message

    def test_a_daily_cycle_is_the_one_that_does_fit(self):
        # repeatEvery: 24 fires once. Placed inside the window, nothing escapes,
        # so this is the only cycle a windowed profile can honestly use today.
        assert _window_findings(_plan(NIGHT, cycles=(24,))) == []

    def test_a_round_the_clock_profile_is_unaffected(self):
        # No window means every firing is wanted; this must not warn about plans
        # that were never scoped to part of the day.
        assert _window_findings(_plan(None, cycles=(1,))) == []


class TestAPlanTheGameWillNotHonourIsNotClean:
    def test_the_verdict_refuses_to_call_it_clean(self):
        plan = _plan(NIGHT, cycles=(1,))
        verdict = assess(plan, plan.findings)
        assert not verdict.clean, (
            "a windowed plan the game runs round the clock must never show green"
        )

    def test_and_the_reason_is_stated_rather_than_left_to_be_inferred(self):
        plan = _plan(NIGHT, cycles=(1,))
        verdict = assess(plan, plan.findings)
        blob = " ".join(verdict.blockers) + " ".join(c.value for c in verdict.unweighed)
        assert Category.WINDOW_NOT_ENFORCEABLE.value in blob or "window" in blob.lower()


class TestWhatTheOperatorsOwnNightProfileWouldHaveDone:
    """The concrete size of the error on the account this was designed for."""

    def test_an_eight_hour_profile_on_hourly_cycles_ships_three_times_over(self):
        window_hours = 8
        for cycle in (1, 2, 4):
            firings_per_day = 24 // cycle
            inside = math.floor(window_hours / cycle)
            assert firings_per_day > inside, "the premise: the game fires more than the model"
            overshoot = firings_per_day / inside
            assert overshoot == pytest.approx(3.0), (
                f"a {cycle}h cycle in an {window_hours}h window ships {overshoot:.0f}x"
            )


class TestDeclaringThePruneMakesThePlanHonestAgain:
    """The same fact, weighed differently once it is being dealt with.

    Without pruning, the escaping firings are a critical over-delivery: the game
    ships roughly a day of cargo through an eight-hour window. With pruning those
    rows are deleted after each route is created, so the window really is enforced
    and the plan can read as clean.

    It stays reported, as a NOTE, because the plan's correctness now DEPENDS on a
    later step. Suppressing it entirely would hide that dependency, and a prune
    that fails is exactly when someone needs to know the plan assumed one.
    """

    def _plan_pruned(self):
        villages = {
            1: VillageState(village_id=1, x=0, y=0, merchant_count=20, name="src"),
            2: VillageState(village_id=2, x=2, y=0, merchant_count=20, name="dst"),
        }
        productions = {Resource.LUMBER: {1: 5000.0, 2: 0.0}}
        allocations = {
            Resource.LUMBER: {
                1: Allocation(mode=AllocationMode.ABSOLUTE, value=0.0),
                2: Allocation(mode=AllocationMode.ABSOLUTE, value=5000.0),
            }
        }
        config = PlannerConfig(
            geometry=GEOMETRY,
            merchant_model=EUROPE2_TEUTON,
            dispatch_window=NIGHT,
            cycles=(1,),
            prune_to_window=True,
        )
        return craft_plan(villages, productions, allocations, config)

    def test_the_critical_finding_is_gone(self):
        assert _window_findings(self._plan_pruned()) == []

    def test_it_is_still_reported_as_a_note(self):
        plan = self._plan_pruned()
        pruned = [f for f in plan.findings if f.category is Category.WINDOW_PRUNED]
        assert pruned, "the dependency must stay visible"
        assert pruned[0].severity is Severity.NOTE

    def test_the_verdict_can_now_be_clean(self):
        plan = self._plan_pruned()
        verdict = assess(plan, plan.findings)
        assert Category.WINDOW_NOT_ENFORCEABLE not in verdict.unweighed
        assert Category.WINDOW_PRUNED not in verdict.unweighed, (
            "a note is not an unweighed critical"
        )

    def test_without_the_declaration_it_is_critical_again(self):
        # The control: the severity must follow the intent, not drift.
        plan = _plan(NIGHT, cycles=(1,))
        assert _window_findings(plan), "not declaring the prune leaves it critical"

    def test_the_note_describes_the_prune_rather_than_the_breach(self):
        """The two findings shared one sentence and differed only by Category,
        so the note told the operator that "the other 16 ship the same cargo
        outside it" and that his destination "receives about 3.0x what was
        modelled". With the prune on, those rows are deleted: nothing ships
        outside the hours and the destination gets what was sized for it. The
        sentence was the critical case's, and it was false here."""
        (note,) = [f for f in self._plan_pruned().findings if f.category is Category.WINDOW_PRUNED]

        assert "ship the same cargo outside it" not in note.message, note.message
        assert "what was modelled" not in note.message, note.message
        assert "the 16 that depart outside this profile's 480 min are deleted" in note.message
        assert "leaving the 8 the plan sized the cargo for" in note.message

    def test_the_breach_keeps_its_own_words(self):
        """The control: correcting the note must not soften the critical one,
        which describes a real over-delivery and is what the operator reads
        when nothing is pruning anything."""
        (breach,) = _window_findings(_plan(NIGHT, cycles=(1,)))

        assert "the other 16 ship the same cargo outside it" in breach.message
        assert "receives about 3.0x what was modelled" in breach.message

    def test_both_findings_quote_the_windows_real_length(self):
        """23:00-07:00 is eight hours. Measured as ``abs(end - start)`` it reads
        as 960 minutes -- the sixteen hours the profile is NOT running -- and
        the finding renders that figure straight to the operator."""
        (note,) = [f for f in self._plan_pruned().findings if f.category is Category.WINDOW_PRUNED]
        (breach,) = _window_findings(_plan(NIGHT, cycles=(1,)))

        for finding in (note, breach):
            assert "480 min" in finding.message, finding.message
            assert "960" not in finding.message, finding.message
