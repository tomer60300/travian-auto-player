"""A refused upgrade must come back with the reason, not just `success: false`.

`buildings.py` did `data.pop("raw_response", None)` — "can be very large and
is only useful for internal debugging". It is also the ONLY diagnostic the
result carries, so a gold guard that fired, a missing upgrade link, a lost
connection and a rejected build were all this one payload:

    200 {'success': False, 'village_id': 0, 'building_id': 19,
         'building_name': 'Unknown', 'old_level': 0, 'new_level': 0,
         'construction_time': '', 'reward_used': False}

The text dropped in the measured run was
`BLOCKED: Construction queue already has [Warehouse Lv6]. Upgrading now would
cost gold`.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.models.buildings import UpgradeResult
from travian_api.web.routes.buildings import (
    ConstructRequest,
    UpgradeRequest,
    construct_building,
    upgrade_building,
)

BLOCKED = (
    "BLOCKED: Construction queue already has [Warehouse Lv6]. "
    "Upgrading now would cost gold. Use allow_gold=True to override."
)


def _result(*, success, raw_response):
    return UpgradeResult(
        success=success,
        village_id=0,
        building_id=19,
        building_name="Warehouse" if success else "Unknown",
        old_level=5 if success else 0,
        new_level=6 if success else 0,
        construction_time="0:30:00" if success else "",
        reward_used=False,
        raw_response=raw_response,
    )


def _session(result):
    async def _upgrade(slot_id, allow_gold=False, village_id=None):
        return result

    async def _construct(slot_id, building_gid, allow_gold=False, village_id=None):
        return result

    return SimpleNamespace(
        building_service=SimpleNamespace(
            upgrade_building=_upgrade,
            construct_building=_construct,
        )
    )


def _upgrade(result):
    return asyncio.run(upgrade_building(UpgradeRequest(slot_id=19, village_id=1), _session(result)))


def test_a_refused_upgrade_carries_the_reason():
    data = _upgrade(_result(success=False, raw_response=BLOCKED))
    assert data["success"] is False
    assert "error" in data, "the only diagnostic the result carries was dropped"
    assert "Construction queue already has" in data["error"]


def test_a_successful_upgrade_carries_no_error():
    data = _upgrade(_result(success=True, raw_response=""))
    assert data["success"] is True
    assert data.get("error") in (None, "")


def test_the_reason_is_truncated_not_dumped():
    """`raw_response` can be a whole HTML page; the payload is not the place."""
    data = _upgrade(_result(success=False, raw_response="x" * 5000))
    assert "raw_response" not in data
    assert 0 < len(data["error"]) <= 200


def test_a_refused_construct_carries_the_reason_too():
    data = asyncio.run(
        construct_building(
            ConstructRequest(slot_id=19, building_gid=10, village_id=1),
            _session(_result(success=False, raw_response=BLOCKED)),
        )
    )
    assert "Construction queue already has" in data["error"]


@pytest.mark.parametrize("raw", ["", None])
def test_a_failure_with_no_text_still_says_something(raw):
    result = _result(success=False, raw_response=raw or "")
    data = _upgrade(result)
    assert data["success"] is False
    assert data["error"], "a refusal with no text must not report an empty reason"
