"""What may refuse a live run, and what may not.

`/execute` gates on feasibility before it writes anything, and both halves of
that gate were wrong in a way that only showed on a real account.

**Too small to name.** Every shortfall blocked, at any size above float noise.
Village 15 was short **0.20 crop/h** -- 4.8 crop a day, against a merchant that
carries 12,500 -- and that alone refused every write across all 27 villages. The
refusal printed itself as "15 needs 0 crop/h": a quantity the message could not
name was stopping the account.

**Somewhere else entirely.** The gate asked whether the WHOLE plan was feasible,
even when the request narrowed the run with `only_origins`. Creating one route
out of village 27 was refused because village 19 and a foreign target wanted
crop nobody had -- neither of which that run goes anywhere near, and neither of
which it could have made better or worse.

Both are about the same thing: a refusal has to be about something this run can
actually do.
"""

from types import SimpleNamespace

import pytest

from travian_api.services.distribution.allocation import NEGLIGIBLE_PER_HOUR, Resource
from travian_api.services.distribution.optimizer import Shortfall
from travian_api.services.distribution.planner import blockers, is_feasible_for

HUB = 2
FAR = 19
TEST_VILLAGE = 27
NAMES = {HUB: "02", FAR: "19", TEST_VILLAGE: "27"}


def _short(village_id: int, per_hour: float) -> Shortfall:
    return Shortfall(
        village_id=village_id,
        resource=Resource.CROP,
        per_hour=per_hour,
        reason="no village has surplus left to cover this demand",
    )


def _plan(*, shortfalls=(), over_budget=(), over_allocated=()) -> SimpleNamespace:
    """Only what the gate reads. A real plan is exercised end to end elsewhere."""
    plan = SimpleNamespace(
        shortfalls=tuple(shortfalls),
        over_budget=tuple(over_budget),
        over_allocated=tuple(over_allocated),
        npc_short=(),
    )
    # Mirrors DistributionPlan.is_feasible: the NAMEABLE shortfalls, the budget
    # breaches, and the two global refusals. A stub that computed this more
    # simply than the real thing would pass for the wrong reason.
    plan.is_feasible = (
        not [s for s in plan.shortfalls if s.per_hour > NEGLIGIBLE_PER_HOUR]
        and not plan.over_budget
        and not plan.over_allocated
        and not plan.npc_short
    )
    return plan


class TestAShortfallTooSmallToNameDoesNotRefuseTheAccount:
    def test_the_exact_case_from_the_account(self):
        """0.20 crop/h at village 15 stopped all 27 villages."""
        plan = _plan(shortfalls=[_short(15, 0.20096352374457638)])

        assert blockers(plan, NAMES) == ()
        assert is_feasible_for(plan, None) is True

    @pytest.mark.parametrize("rate", [0.0, 0.2, NEGLIGIBLE_PER_HOUR])
    def test_anything_that_rounds_to_zero_is_ignored(self, rate):
        assert blockers(_plan(shortfalls=[_short(15, rate)]), NAMES) == ()

    @pytest.mark.parametrize("rate", [0.51, 7.8, 4714.178356713427])
    def test_anything_that_can_be_NAMED_still_refuses(self, rate):
        """The threshold is where the printed figure stops being zero.

        7.8/h reads as "8/h" and remains a refusal -- this narrows the gate, it
        does not open it.
        """
        reasons = blockers(_plan(shortfalls=[_short(FAR, rate)]), NAMES)

        assert len(reasons) == 1
        assert "19 needs" in reasons[0]
        assert "needs 0 crop/h" not in reasons[0]

    def test_a_refusal_never_names_a_quantity_it_rounded_away(self):
        """The bug's signature, pinned: no blocker may read "needs 0"."""
        plan = _plan(shortfalls=[_short(15, 0.2), _short(FAR, 4714.2)])

        assert not any("needs 0 " in reason for reason in blockers(plan, NAMES))


class TestARunIsOnlyRefusedForWhereItGoes:
    """`only_origins` narrows the run, so it must narrow the veto with it."""

    SOMEWHERE_ELSE = _plan(shortfalls=[_short(FAR, 4714.2)])

    def test_unscoped_still_refuses_exactly_as_before(self):
        """`None` is the old behaviour, unchanged -- this is the safety net."""
        assert is_feasible_for(self.SOMEWHERE_ELSE, None) is False
        assert len(blockers(self.SOMEWHERE_ELSE, NAMES)) == 1

    def test_a_run_that_never_touches_the_blocked_village_may_proceed(self):
        scope = {TEST_VILLAGE, 11}

        assert is_feasible_for(self.SOMEWHERE_ELSE, scope) is True
        assert blockers(self.SOMEWHERE_ELSE, NAMES, only_villages=scope) == ()

    def test_a_run_that_DOES_touch_it_is_still_refused(self):
        """The point of the narrowing is that it narrows, not that it excuses."""
        scope = {TEST_VILLAGE, FAR}

        assert is_feasible_for(self.SOMEWHERE_ELSE, scope) is False
        reasons = blockers(self.SOMEWHERE_ELSE, NAMES, only_villages=scope)
        assert len(reasons) == 1 and "19 needs" in reasons[0]

    def test_an_over_budget_origin_outside_the_scope_does_not_refuse_either(self):
        over = SimpleNamespace(
            village_id=HUB, committed=14, available=12, max_busy=12, fleet_spare=18
        )
        plan = _plan(over_budget=[over])

        assert is_feasible_for(plan, {TEST_VILLAGE, 11}) is True
        assert is_feasible_for(plan, {HUB}) is False

    def test_an_over_claimed_allocation_refuses_whatever_the_scope(self):
        """Global on purpose: it says the SHEET asks for more than exists.

        The remainder village would ship what it does not have, and no choice of
        origins makes that untrue.
        """
        plan = _plan(over_allocated=[Resource.CROP])

        assert is_feasible_for(plan, {TEST_VILLAGE}) is False
        assert is_feasible_for(plan, None) is False

    def test_the_predicate_and_the_reasons_agree(self):
        """`/execute` refuses on the predicate and explains with the list.

        When they disagree the operator gets a refusal with no reason, or a
        reason for something that was not refused.
        """
        plan = _plan(shortfalls=[_short(15, 0.2), _short(FAR, 4714.2)])
        for scope in (None, {FAR}, {TEST_VILLAGE}, {TEST_VILLAGE, FAR}, set()):
            refused = not is_feasible_for(plan, scope)
            named = bool(blockers(plan, NAMES, only_villages=scope))
            assert refused == named, f"scope={scope}: refused={refused} named={named}"
