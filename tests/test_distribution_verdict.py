"""One word was answering two questions.

``is_feasible`` weighs merchant budgets, unroutable demand and over-claimed
allocations. It does not weigh -- and must not -- whether the plan is a good
idea: stores overflowing, a granary running dry, a tribute going unpaid. Those
are facts about the account, and a plan that leaves them in place is still
perfectly executable. Vetoing on overflow would break a deliberate stockpile.

But the page rendered that single boolean as a green "Feasible" badge, so a plan
losing 2.4M resources a day looked approved, and it is the only thing standing
between a bad plan and 53 real routes. The fix is not to block: it is to make the
answer say what it weighed and what it did not.
"""

import pytest

from travian_api.services.distribution.allocation import Resource
from travian_api.services.distribution.findings import Category, Finding, Severity
from travian_api.services.distribution.optimizer import OverBudget, Plan, Shortfall
from travian_api.services.distribution.planner import DistributionPlan, assess, blockers

NAMES = {3: "V03", 7: "Capital"}


def _plan(**routing) -> DistributionPlan:
    return DistributionPlan(routing=Plan(**routing))


def _finding(category: Category, *, loss: float = 0.0) -> Finding:
    return Finding(category=category, message="something", loss_per_day=loss)


class TestWhatItWeighed:
    def test_it_names_its_own_criteria_even_when_everything_passes(self):
        verdict = assess(_plan(), [], NAMES)

        assert verdict.executable
        assert verdict.covers, "a gate that will not say what it checked is not a gate"
        assert verdict.blockers == ()

    def test_an_over_budget_village_is_a_named_blocker(self):
        verdict = assess(
            _plan(over_budget=(OverBudget(village_id=3, committed=9, available=4),)),
            [],
            NAMES,
        )

        assert not verdict.executable
        assert len(verdict.blockers) == 1
        assert "V03" in verdict.blockers[0]
        assert "9" in verdict.blockers[0] and "4" in verdict.blockers[0]

    def test_unroutable_demand_is_a_named_blocker(self):
        verdict = assess(
            _plan(
                shortfalls=(
                    Shortfall(
                        village_id=7,
                        resource=Resource.CROP,
                        per_hour=1200.0,
                        reason="no village has surplus left to cover this demand",
                    ),
                )
            ),
            [],
            NAMES,
        )

        assert not verdict.executable
        assert "Capital" in verdict.blockers[0]
        assert "crop" in verdict.blockers[0]

    def test_the_blocker_repeats_the_reason_the_shortfall_gave(self):
        """`blockers` ignored `short.reason` and hardcoded "no village has
        spare", so an operator whose own exclusion list caused the shortfall
        was sent looking for production. /execute renders exactly this tuple."""
        reasons = blockers(
            _plan(
                shortfalls=(
                    Shortfall(
                        village_id=7,
                        resource=Resource.CROP,
                        per_hour=1200.0,
                        reason="every village with surplus left is excluded from this "
                        "destination: V03",
                    ),
                )
            ),
            NAMES,
        )

        assert len(reasons) == 1
        assert "excluded" in reasons[0], reasons
        assert "V03" in reasons[0], "the excluded origin must survive into the blocker"
        assert "no village has spare" not in reasons[0]

    def test_a_cap_is_named_as_the_operators_own_ceiling(self):
        """ "its budget allows 8" of a village fielding 19 is nobody's figure.

        `blockers` renders the /plan verdict AND /execute's 422 body, so this
        was the one surface that refuses to write while blaming a number the
        operator cannot find anywhere in the game. Same clause as the budget
        explanation, from the same helper.
        """
        reasons = blockers(
            _plan(
                over_budget=(
                    OverBudget(village_id=7, committed=12, available=8, max_busy=8, fleet_spare=17),
                )
            ),
            NAMES,
        )

        assert "you capped it at 8 busy at once" in reasons[0], reasons
        assert "17" in reasons[0], "the fleet the ceiling is holding back"

    def test_an_uncapped_village_reads_as_the_budget_it_always_did(self):
        reasons = blockers(
            _plan(over_budget=(OverBudget(village_id=7, committed=12, available=8),)),
            NAMES,
        )

        assert reasons[0] == "Capital commits 12 merchants but its budget allows 8"

    def test_a_village_with_no_name_is_still_identified(self):
        verdict = assess(
            _plan(over_budget=(OverBudget(village_id=99, committed=9, available=4),)),
            [],
            {},
        )
        assert "99" in verdict.blockers[0]


class TestWhatItDidNotWeigh:
    def test_a_plan_that_destroys_resources_is_still_executable(self):
        # Deliberate: the operator stockpiles crop to 700K before NPCing it, so
        # an overflow veto would refuse a plan they meant to run.
        verdict = assess(
            _plan(),
            [_finding(Category.OVERFLOW_STRUCTURAL, loss=2_400_000)],
            NAMES,
        )
        assert verdict.executable

    def test_but_it_is_not_reported_as_clean(self):
        """The actual defect: the badge. Executable and clean are not the same
        answer, and only one of them earns a green light."""
        verdict = assess(
            _plan(),
            [_finding(Category.OVERFLOW_STRUCTURAL, loss=2_400_000)],
            NAMES,
        )

        assert not verdict.clean
        assert verdict.critical_findings == 1
        assert Category.OVERFLOW_STRUCTURAL in verdict.unweighed

    def test_a_clean_plan_is_clean(self):
        assert assess(_plan(), [], NAMES).clean

    def test_notes_and_warnings_do_not_cost_the_green_light(self):
        # Every real account produces warnings -- missed latency targets are a
        # soft target by design. If those blocked the green light nothing would
        # ever be green and the badge would mean nothing again.
        findings = [_finding(Category.LATENCY), _finding(Category.RELAY)]
        verdict = assess(_plan(), findings, NAMES)

        assert all(f.severity is not Severity.CRITICAL for f in findings)
        assert verdict.clean
        assert verdict.unweighed == ()

    def test_the_criterion_it_does_weigh_is_not_listed_as_unweighed(self):
        # over_allocated IS part of is_feasible, so its finding must not also be
        # reported as something the check ignored.
        verdict = assess(_plan(), [_finding(Category.OVER_ALLOCATED)], NAMES)

        assert Category.OVER_ALLOCATED not in verdict.unweighed

    def test_repeated_criticals_are_named_once(self):
        verdict = assess(
            _plan(),
            [_finding(Category.STARVATION), _finding(Category.STARVATION)],
            NAMES,
        )

        assert verdict.unweighed == (Category.STARVATION,)
        assert verdict.critical_findings == 2, "the count is not deduplicated"

    def test_an_unexecutable_plan_is_never_clean(self):
        verdict = assess(
            _plan(over_budget=(OverBudget(village_id=3, committed=9, available=4),)),
            [],
            NAMES,
        )
        assert not verdict.clean


class TestItRefusesToAnswerFromHalfTheEvidence:
    """The one mistake that makes ``assess`` assert the opposite of the truth.

    Overflow, starvation and busy merchants are computed in the endpoint, not in
    ``craft_plan``, and they are exactly what ``unweighed`` reports. Handed only
    ``plan.findings``, ``assess`` would return ``clean=True`` for a plan that
    destroys 2.4M resources a day -- the original defect, restored by a plausible
    call. So the contract is checked rather than documented.
    """

    def test_a_partial_list_raises_instead_of_reporting_clean(self):
        plan = DistributionPlan(findings=(_finding(Category.STARVATION),))

        with pytest.raises(ValueError, match="complete finding list"):
            assess(plan, [], NAMES)

    def test_the_plans_own_findings_are_enough_when_that_is_all_there_is(self):
        finding = _finding(Category.STARVATION)
        plan = DistributionPlan(findings=(finding,))

        verdict = assess(plan, [finding], NAMES)

        assert verdict.unweighed == (Category.STARVATION,)

    def test_extra_findings_on_top_are_the_normal_case(self):
        own = _finding(Category.OVER_ALLOCATED)
        plan = DistributionPlan(findings=(own,))

        verdict = assess(plan, [own, _finding(Category.OVERFLOW_STRUCTURAL)], NAMES)

        assert verdict.critical_findings == 2
        assert verdict.unweighed == (Category.OVERFLOW_STRUCTURAL,)


class TestTheRefusalAlwaysSaysWhy:
    """``/execute`` gates on ``is_feasible`` and explains itself with
    ``blockers``. If a criterion is ever added to the first without the second,
    the refusal becomes "refusing to write to the account." with no reason -- so
    the two are pinned to each other here rather than left to vigilance."""

    @pytest.mark.parametrize(
        "routing",
        [
            {"over_budget": (OverBudget(village_id=3, committed=9, available=4),)},
            {
                "shortfalls": (
                    Shortfall(
                        village_id=7, resource=Resource.IRON, per_hour=10.0, reason="none left"
                    ),
                )
            },
            {
                "over_budget": (OverBudget(village_id=3, committed=9, available=4),),
                "shortfalls": (
                    Shortfall(
                        village_id=7, resource=Resource.IRON, per_hour=10.0, reason="none left"
                    ),
                ),
            },
        ],
    )
    def test_every_way_a_plan_can_fail_produces_a_reason(self, routing):
        plan = _plan(**routing)

        assert not plan.is_feasible
        assert blockers(plan, NAMES), "refused with nothing to tell the operator"

    def test_a_feasible_plan_has_nothing_to_explain(self):
        plan = _plan()

        assert plan.is_feasible
        assert blockers(plan, NAMES) == ()
