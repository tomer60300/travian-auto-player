"""`BuildPlan.pending_items`/`next_priority` must work highest priority first.

Every existing build-queue test uses a plan with exactly one item, so this
ordering was invisible to the whole suite: `pending_items()` could sort
ascending or descending and nothing would notice.
"""

from travian_api.services.build_queue_service import BuildPlan, BuildPlanItem


def test_the_plan_is_worked_highest_priority_first():
    plan = BuildPlan(
        village_id=1,
        items=[
            BuildPlanItem(building="Warehouse", target=5, priority=3),
            BuildPlanItem(building="Granary", target=4, priority=1),
            BuildPlanItem(building="Sawmill", target=2, priority=2),
        ],
    )
    assert [i.building for i in plan.pending_items()] == ["Granary", "Sawmill", "Warehouse"]
    assert plan.next_priority() == 1


def test_a_finished_item_no_longer_sets_the_priority():
    plan = BuildPlan(
        village_id=1,
        items=[
            BuildPlanItem(building="Granary", target=4, priority=1, status="done"),
            BuildPlanItem(building="Sawmill", target=2, priority=2),
        ],
    )
    assert [i.building for i in plan.pending_items()] == ["Sawmill"]
    assert plan.next_priority() == 2
