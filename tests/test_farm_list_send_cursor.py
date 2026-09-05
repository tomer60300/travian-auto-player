"""The round-robin cursor must survive an exception mid-send.

The cursor was written only on the two clean exits, so a
`TravianError`/`NetworkError` on batch 3 propagated past the assignment and
left it untouched. The batches that HAD landed were then re-raided by the next
run — measured 12 of 12 slots duplicated across two runs, 8 of them definitely
dispatched, with troops leaving twice. There is no fixed point on the failing
shape: the same farms are re-raided every time the failure recurs.
"""

import asyncio

import pytest

from travian_api.exceptions import NetworkError
from travian_api.models.farm_list import FarmList
from travian_api.services.farm_list_service import FarmListService

SLOT_IDS = list(range(100, 112))  # 12 active slots
BATCH_SIZE = 4  # pinned; the code draws from randint(BATCH-1, BATCH+2)


def _clean_body(slot_ids):
    return {"lists": [{"targets": [{"id": s, "status": "sent"} for s in slot_ids]}]}


class _Client:
    """Answers every batch cleanly except the `raise_on_batch`-th (1-based)."""

    def __init__(self, raise_on_batch=None):
        self._raise_on_batch = raise_on_batch
        self.sent_batches: list[list[int]] = []
        self.human_delay = _Delay()
        self.activity_scheduler = _Scheduler()

    async def post_json(self, url, data=None, **kwargs):
        slot_ids = list(data["lists"][0]["targets"])
        self.sent_batches.append(slot_ids)
        if len(self.sent_batches) == self._raise_on_batch:
            raise NetworkError("HTTP 500: server exploded", 500)
        return _clean_body(slot_ids)

    @property
    def requested(self) -> list[int]:
        return [s for b in self.sent_batches for s in b]


class _Delay:
    async def wait_range(self, lo, hi, reason=""):
        return None


class _Scheduler:
    def log_activity(self, seconds):
        return None


@pytest.fixture(autouse=True)
def _pin_batch_size(monkeypatch):
    monkeypatch.setattr("random.randint", lambda lo, hi: BATCH_SIZE)


def _farm_list():
    return FarmList.model_validate(
        {"id": 7, "name": "raids", "slots": [{"id": s, "isActive": True} for s in SLOT_IDS]}
    )


def _send(svc, client):
    return asyncio.run(svc.send_farm_list(7, known_list=_farm_list()))


def test_a_clean_run_advances_the_cursor_by_what_it_sent():
    client = _Client()
    svc = FarmListService(client)
    _send(svc, client)
    assert client.requested == SLOT_IDS
    assert svc._cursors[7] == 0, "12 sent of 12 wraps to 0"


def test_an_exception_on_batch_three_still_commits_the_first_two_batches():
    client = _Client(raise_on_batch=3)
    svc = FarmListService(client)
    with pytest.raises(NetworkError):
        _send(svc, client)

    # All three batches were REQUESTED; batch 3's answer is what failed.
    assert client.requested == SLOT_IDS
    assert svc._cursors[7] == 8, "the cursor must not forget the 8 slots that went out"


def test_the_next_run_spends_its_troops_on_what_did_not_go_out():
    """A truncated cycle must not re-raid the batches that landed.

    The cursor decides the ORDER, which is what matters when troops run out
    part-way down the list: with it forgotten, run 2 restarts at slot 100 and
    spends the troops on the four farms run 1 had just raided.
    """
    client = _Client(raise_on_batch=3)
    svc = FarmListService(client)
    with pytest.raises(NetworkError):
        _send(svc, client)
    landed = set(SLOT_IDS[:8])

    second = _Client()
    svc.http_client = second
    _send(svc, second)

    assert second.sent_batches[0] == [108, 109, 110, 111], "run 2 resumes where run 1 stopped"
    re_raided_first = sorted(landed & set(second.sent_batches[0]))
    assert re_raided_first == [], f"re-raided first: {re_raided_first}"


def test_a_troop_exhausted_batch_still_advances_past_itself():
    """The deliberate exception to "advance by what was sent"."""

    class _Exhausted(_Client):
        async def post_json(self, url, data=None, **kwargs):
            slot_ids = list(data["lists"][0]["targets"])
            self.sent_batches.append(slot_ids)
            return {
                "lists": [
                    {
                        "targets": [
                            {"id": s, "status": "error", "error": "Not enough troops"}
                            for s in slot_ids
                        ]
                    }
                ]
            }

    client = _Exhausted()
    svc = FarmListService(client)
    _send(svc, client)
    assert len(client.sent_batches) == 1
    assert svc._cursors[7] == BATCH_SIZE
