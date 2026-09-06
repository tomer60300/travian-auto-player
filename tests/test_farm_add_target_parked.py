"""A target can be put on a list without being raided from it.

`FarmListService.add_slot` has taken `active: bool = True` since before this
change — it is in the JSON body it posts to `/api/v1/farm-list/slot`. The HTTP
route simply never exposed it, so every slot any HTTP client added came out
raiding. `scripts/one_shot_raid_lists.py` does not hit this because it drives
the service directly.

That matters because parking is how the operator stops raiding a target
WITHOUT losing it: a deleted slot takes its loot history with it, and the
lifetime `total_booty`/`total_raids` counters the game keeps per slot are what
every loot estimate in this repo is built on. Deactivating keeps the record and
stops the sends; deleting throws away the evidence.

The route handler is called directly with a recording fake, the way
`test_farm_send_all_budget_stop.py` does — the assertion is about which
keyword reaches `add_slot`, not about the wire.
"""

import asyncio
from types import SimpleNamespace

from travian_api.web.routes.farm import AddTargetRequest, add_target


def _session(calls):
    async def add_slot(list_id, x, y, units=None, active=True, force=False):
        calls.append(
            {
                "list_id": list_id,
                "x": x,
                "y": y,
                "units": units,
                "active": active,
                "force": force,
            }
        )

    return SimpleNamespace(farm_service=SimpleNamespace(add_slot=add_slot))


def _add(**body):
    calls: list[dict] = []
    result = asyncio.run(add_target(7, AddTargetRequest(x=-12, y=34, **body), _session(calls)))
    return result, calls


def test_a_target_is_added_active_by_default():
    """Every caller on main today posts no `active` field. Widening the model
    must not quietly park the slots they have always created raiding."""
    result, calls = _add()

    assert calls[0]["active"] is True
    assert result == {"list_id": 7, "x": -12, "y": 34}


def test_a_target_can_be_added_parked():
    """`active: false` in the body has to arrive at `add_slot` as given — the
    service already knows what to do with it."""
    _result, calls = _add(active=False)

    assert calls[0]["active"] is False
