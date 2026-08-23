"""Undoing a live run has to be a tested operation, not a promise.

The game returns no id when it creates a route, so "what did this run add?" can
only be answered by diffing a fresh read against the inventory the trace recorded
before the run started. And because this app has no verified delete request, a
revert is genuinely two things: the part it can perform, and the part a person
has to perform in the UI. Conflating those would let a "reverted" run leave live
routes shipping resources.
"""

from travian_api.services.distribution.route_revert import describe, plan_revert


def _row(route_id: int, dest: int = 500, active: bool = True) -> dict:
    return {"route_id": route_id, "dest": dest, "active": active}


class TestNothingHappened:
    def test_an_identical_read_is_clean(self):
        rows = [_row(1), _row(2, active=False)]
        plan = plan_revert(20003, rows, rows)

        assert plan.is_clean
        assert plan.disable_ids == []
        assert plan.manual_delete_ids == []
        assert describe(plan) == ["village 20003: unchanged, nothing to revert"]

    def test_an_empty_village_that_is_still_empty_is_clean(self):
        assert plan_revert(20003, [], []).is_clean


class TestRoutesTheRunCreated:
    def test_a_new_row_is_reported_as_created(self):
        plan = plan_revert(20003, [_row(1)], [_row(1), _row(99)])

        assert [r.route_id for r in plan.created] == [99]
        assert plan.manual_delete_ids == [99]
        assert not plan.is_clean

    def test_a_created_route_that_is_live_must_be_disabled_first(self):
        # This is the ordering that matters: a created route left enabled while
        # someone gets round to deleting it keeps shipping resources.
        plan = plan_revert(20003, [], [_row(99, active=True)])

        assert plan.disable_ids == [99]
        lines = describe(plan)
        assert "FIRST disable" in lines[0]
        assert any("DELETE" in line for line in lines)
        assert lines.index(next(x for x in lines if "FIRST disable" in x)) < lines.index(
            next(x for x in lines if "DELETE" in x)
        ), "disabling must be described before deleting"

    def test_a_created_route_already_disabled_needs_no_disable(self):
        plan = plan_revert(20003, [], [_row(99, active=False)])

        assert plan.disable_ids == [], "already inert"
        assert plan.manual_delete_ids == [99], "but still needs removing"

    def test_the_whole_fan_out_is_reported_not_just_one_row(self):
        # One create request becomes 24/N rows. A revert that named only one of
        # them would leave the rest behind.
        after = [_row(600 + i) for i in range(24)]
        plan = plan_revert(20003, [], after)

        assert len(plan.manual_delete_ids) == 24
        assert plan.disable_ids == sorted(600 + i for i in range(24))

    def test_the_delete_instructions_name_the_actual_ui_path(self):
        # An operator reading this is about to click things; vagueness costs them
        # a wrong click on a real account.
        plan = plan_revert(20003, [], [_row(99)])
        text = " ".join(describe(plan))

        assert "Edit selected" in text
        assert "trash" in text
        assert "no verified delete" in text, "and it must not imply the app can do it"


class TestStateTheRunChanged:
    def test_a_route_the_run_disabled_is_restored_to_enabled(self):
        plan = plan_revert(20003, [_row(1, active=True)], [_row(1, active=False)])

        assert plan.to_restore == [(1, True)]
        assert plan.created == []
        assert "restore route 1 to enabled" in " ".join(describe(plan))

    def test_a_route_the_run_re_enabled_is_restored_to_disabled(self):
        plan = plan_revert(20003, [_row(1, active=False)], [_row(1, active=True)])

        assert plan.to_restore == [(1, False)]
        assert "restore route 1 to disabled" in " ".join(describe(plan))

    def test_an_untouched_route_is_not_restored(self):
        plan = plan_revert(20003, [_row(1), _row(2)], [_row(1), _row(2)])
        assert plan.to_restore == []


class TestTheDiffAdmitsWhenItCannotBeTrusted:
    def test_a_route_that_vanished_is_flagged_loudly(self):
        # The app never deletes. So a row disappearing means something else
        # changed this village, and the rest of the comparison is suspect --
        # reporting a tidy revert plan here would be false confidence.
        plan = plan_revert(20003, [_row(1), _row(2)], [_row(1)])

        assert [r.route_id for r in plan.vanished] == [2]
        assert not plan.is_clean
        text = " ".join(describe(plan))
        assert "WARNING" in text
        assert "may not reflect what the run did" in text

    def test_a_row_with_no_usable_id_is_dropped_not_half_handled(self):
        # Without an id it cannot be disabled or deleted, so counting it as
        # revertible would overstate what the plan can achieve.
        after = [_row(99), {"dest": 500, "active": True}, {"route_id": "nonsense"}]
        plan = plan_revert(20003, [], after)

        assert plan.manual_delete_ids == [99]

    def test_a_missing_active_flag_is_assumed_live(self):
        # Erring toward "this is running" makes the plan disable something
        # harmless; erring the other way leaves a live route behind.
        plan = plan_revert(20003, [], [{"route_id": 99, "dest": 500}])
        assert plan.disable_ids == [99]


class TestOrderingIsStable:
    def test_ids_are_reported_sorted_regardless_of_input_order(self):
        plan = plan_revert(20003, [], [_row(300), _row(100), _row(200)])

        assert plan.manual_delete_ids == [100, 200, 300]
        assert plan.disable_ids == [100, 200, 300]
