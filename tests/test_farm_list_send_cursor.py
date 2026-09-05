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
from travian_api.models.farm_list import FarmList, FarmListSendResult
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

    # F8: the cursor must not forget where it started. Only observable on a
    # second cycle that begins somewhere other than 0.
    assert svc._cursors[7] == 8, "run 2 sent all twelve from 8, so it lands back on 8"

    third = _Client()
    svc.http_client = third
    _send(svc, third)
    assert third.sent_batches[0] == [108, 109, 110, 111], "run 3 opens where run 2 did"


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


def test_a_half_failed_batch_advances_the_cursor_by_what_landed():
    """Four asked, two sent: the cursor moves two, not four."""

    class _HalfFails(_Client):
        async def post_json(self, url, data=None, **kwargs):
            slot_ids = list(data["lists"][0]["targets"])
            self.sent_batches.append(slot_ids)
            if len(self.sent_batches) == 1:
                return {
                    "lists": [
                        {
                            "targets": [
                                {"id": slot_ids[0], "status": "sent"},
                                {"id": slot_ids[1], "status": "sent"},
                                {"id": slot_ids[2], "status": "error", "error": "Not enough troops"},
                                {"id": slot_ids[3], "status": "error", "error": "Not enough troops"},
                            ]
                        }
                    ]
                }
            return _clean_body(slot_ids)

    client = _HalfFails()
    svc = FarmListService(client)
    _send(svc, client)

    assert len(client.sent_batches) == 3, "a half-sent batch is not exhaustion"
    assert svc._cursors[7] == 10, "2 + 4 + 4 landed, not 12"

    second = _Client()
    svc.http_client = second
    _send(svc, second)
    assert second.sent_batches[0] == [110, 111, 100, 101]


def test_a_batch_refused_for_mixed_reasons_is_not_troop_exhaustion():
    """One "not enough troops" among three captchas is a REFUSAL.

    Exhaustion advances the cursor past the batch on purpose; a refusal must
    not, because those slots were never raided.
    """

    class _MixedRefusal(_Client):
        async def post_json(self, url, data=None, **kwargs):
            slot_ids = list(data["lists"][0]["targets"])
            self.sent_batches.append(slot_ids)
            errors = ["Not enough troops", "captcha required",
                      "captcha required", "captcha required"]
            return {
                "lists": [
                    {
                        "targets": [
                            {"id": s, "status": "error", "error": e}
                            for s, e in zip(slot_ids, errors)
                        ]
                    }
                ]
            }

    client = _MixedRefusal()
    svc = FarmListService(client)
    _send(svc, client)

    assert len(client.sent_batches) == 1, "the game refused; stop asking"
    assert svc._cursors[7] == 0, "nothing was raided, so nothing is stepped over"

    second = _Client()
    svc.http_client = second
    _send(svc, second)
    assert second.sent_batches[0] == [100, 101, 102, 103], "the refused four are retried"


def test_send_all_stops_at_the_activity_ceiling():
    """`send_all_farm_lists` has no test of its own, and the activity ceiling
    is its only guard against firing an entire account's lists past the
    stealth budget."""

    class _CeilingAfter:
        def __init__(self, n):
            self.n, self.calls = n, 0

        def can_continue(self):
            self.calls += 1
            return self.calls <= self.n

        def log_activity(self, seconds):
            return None

    class _DelayWithWait(_Delay):
        async def wait(self, action_type, reason=""):
            return None

    class _Noise:
        async def maybe_inject_noise(self, village_id=None):
            return None

    client = _Client()
    client.human_delay = _DelayWithWait()
    client.activity_scheduler = _CeilingAfter(2)
    client.noise_injector = _Noise()
    svc = FarmListService(client)

    lists = [
        FarmList.model_validate({"id": i, "name": f"l{i}", "slots": []})
        for i in (1, 2, 3, 4, 5)
    ]
    sent: list[int] = []

    async def _all():
        return lists

    async def _one(list_id, known_list=None):
        sent.append(list_id)
        return FarmListSendResult(targets=[])

    svc.get_all_farm_lists = _all
    svc.send_farm_list = _one

    results = asyncio.run(svc.send_all_farm_lists())

    assert sent == [1, 2], "the ceiling stopped the account after two lists"
    assert sorted(results) == [1, 2], "and only those two are reported"


def test_a_two_slot_list_is_still_round_robined(monkeypatch):
    """The bulk (non-round-robin) path never writes `_cursors`, so a two-slot
    list wrongly routed there loses its rotation entirely."""
    monkeypatch.setattr("random.randint", lambda lo, hi: 1)  # one target a batch
    client = _Client()
    svc = FarmListService(client)
    fl = FarmList.model_validate(
        {
            "id": 7,
            "name": "raids",
            "slots": [{"id": 100, "isActive": True}, {"id": 101, "isActive": True}],
        }
    )
    asyncio.run(svc.send_farm_list(7, known_list=fl))

    assert client.sent_batches == [[100], [101]], "two batches, not one bulk call"
    assert svc._cursors[7] == 0, "and the list has a cursor at all"
