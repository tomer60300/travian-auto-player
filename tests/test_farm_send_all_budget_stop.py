"""`send-all` must say when the activity ceiling stopped it part-way.

`send_all_farm_lists` returns `results` early when `can_continue()` goes
false, and the route rendered `[... for lid, result in result_map.items()]` —
so a 5-list request that ran 2 came back as a 2-element array with no field
that could say the other three never ran. `SendResultResponse` had no such
field, and `farm_ws` folds the dict into `cycle_end sent/failed` totals, so a
5-list cycle that sent 2 was indistinguishable from a 2-list account.

Measured before the fix, 5 lists requested, `can_continue()` false from the
3rd call:
    result_map has 2: [1, 2]
    route body = [{list_id:1, ...}, {list_id:2, ...}]
    response model fields = ['list_id','success_count','fail_count','targets']
"""

import asyncio
from types import SimpleNamespace

from travian_api.models.farm_list import FarmList, FarmListSendResult, FarmListSendTargetResult
from travian_api.web.routes.farm import SendAllRequest, send_all_farm_lists

RAN = [1, 2]
REQUESTED = [1, 2, 3, 4, 5]


def _list(lid):
    return FarmList.model_validate(
        {"id": lid, "name": f"list{lid}", "slots": [{"id": lid * 10, "isActive": True}]}
    )


def _session():
    async def _all():
        return [_list(lid) for lid in REQUESTED]

    async def _send_all(list_ids=None):
        # The ceiling stopped it after two lists.
        return {
            lid: FarmListSendResult(targets=[FarmListSendTargetResult(id=lid * 10, status="sent")])
            for lid in RAN
        }

    return SimpleNamespace(
        farm_service=SimpleNamespace(get_all_farm_lists=_all, send_all_farm_lists=_send_all)
    )


def _run():
    return asyncio.run(send_all_farm_lists(SendAllRequest(list_ids=REQUESTED), _session()))


def test_every_requested_list_appears_in_the_answer():
    body = _run()
    assert [r.list_id for r in body] == REQUESTED


def test_the_lists_that_never_ran_are_marked_as_such():
    body = _run()
    by_id = {r.list_id: r for r in body}
    assert [by_id[lid].status for lid in RAN] == ["sent", "sent"]
    for lid in (3, 4, 5):
        assert by_id[lid].status == "not_attempted"
        assert by_id[lid].success_count == 0
        assert by_id[lid].targets == []


def test_a_complete_run_marks_nothing_as_skipped():
    session = _session()

    async def _send_all(list_ids=None):
        return {
            lid: FarmListSendResult(targets=[FarmListSendTargetResult(id=lid * 10, status="sent")])
            for lid in REQUESTED
        }

    session.farm_service.send_all_farm_lists = _send_all
    body = asyncio.run(send_all_farm_lists(SendAllRequest(list_ids=REQUESTED), session))
    assert {r.status for r in body} == {"sent"}
