"""A farm-list send whose body cannot be read must not be reported as
"troops exhausted", and must not silently skip the rest of the list.

`_send_batch` returned zero `FarmListSendTargetResult`s for an empty 200, a
missing `lists` key, an HTML soft-block (which `post_json` hands back as
`{"response_text": ...}`) or a non-object body. The exhaustion test then read
`batch_sent == 0 and batch_troop_errors == len(batch_results)` as `0 == 0` and
returned immediately, advancing the cursor PAST the batch: eight of twelve
farms were never raided and the response was byte-identical to "the list was
empty".
"""

import asyncio

import pytest

from travian_api.models.farm_list import FarmList
from travian_api.services.farm_list_service import FarmListService

SLOT_IDS = list(range(100, 112))  # 12 active slots
BATCH_SIZE = 4  # pinned; the code draws from randint(BATCH-1, BATCH+2)


def _clean_body(slot_ids):
    return {"lists": [{"targets": [{"id": s, "status": "sent"} for s in slot_ids]}]}


class _Client:
    """Records every send POST; answers batch 1 with `first_body`."""

    def __init__(self, first_body):
        self._first_body = first_body
        self.sent_batches: list[list[int]] = []
        self.human_delay = _Delay()
        self.activity_scheduler = _Scheduler()

    async def post_json(self, url, data=None, **kwargs):
        slot_ids = list(data["lists"][0]["targets"])
        self.sent_batches.append(slot_ids)
        if len(self.sent_batches) == 1:
            return self._first_body
        return _clean_body(slot_ids)


class _Delay:
    async def wait_range(self, lo, hi, reason=""):
        return None


class _Scheduler:
    def __init__(self):
        self.bills: list[float] = []

    def log_activity(self, seconds):
        self.bills.append(seconds)


@pytest.fixture(autouse=True)
def _pin_batch_size(monkeypatch):
    monkeypatch.setattr("random.randint", lambda lo, hi: BATCH_SIZE)


def _run(first_body):
    client = _Client(first_body)
    svc = FarmListService(client)
    fl = FarmList.model_validate(
        {"id": 7, "name": "raids", "slots": [{"id": s, "isActive": True} for s in SLOT_IDS]}
    )
    result = asyncio.run(svc.send_farm_list(7, known_list=fl))
    return client, svc, result


def test_the_clean_baseline_fires_every_batch():
    client, svc, result = _run(_clean_body(SLOT_IDS[:BATCH_SIZE]))
    assert len(client.sent_batches) == 3
    assert len(result.targets) == 12
    assert result.success_count == 12
    assert result.fail_count == 0


@pytest.mark.parametrize(
    "first_body",
    [
        {},  # empty 200
        {"lists": []},  # no list entry
        {"lists": [{}]},  # list entry with no targets array
        {"lists": [{"targets": []}]},  # targets array naming nobody
        {"response_text": "<html>soft block</html>"},  # HTML body
        None,  # non-object body
        [],  # non-object body
    ],
)
def test_an_unreadable_batch_does_not_stop_the_list(first_body):
    client, svc, result = _run(first_body)

    # The remaining two batches still go out.
    assert len(client.sent_batches) == 3
    assert [s for b in client.sent_batches for s in b] == SLOT_IDS

    # Every slot in the unreadable batch is accounted for, as unverified.
    assert len(result.targets) == 12
    unverified = [t for t in result.targets if t.status == "unverified"]
    assert [t.id for t in unverified] == SLOT_IDS[:BATCH_SIZE]
    assert all(t.error for t in unverified)

    # "Could not check" is not success.
    assert result.success_count == 8
    assert result.fail_count == 4


@pytest.mark.parametrize(
    "first_body",
    [{}, {"lists": []}, {"response_text": "<html>soft block</html>"}, None],
)
def test_an_unreadable_batch_is_not_reported_as_troops_exhausted(first_body, caplog):
    with caplog.at_level("INFO"):
        _run(first_body)
    assert "troops exhausted" not in caplog.text


def test_a_genuine_troop_exhaustion_still_stops_the_list():
    exhausted = {
        "lists": [
            {
                "targets": [
                    {"id": s, "status": "error", "error": "Not enough troops"}
                    for s in SLOT_IDS[:BATCH_SIZE]
                ]
            }
        ]
    }
    client, svc, result = _run(exhausted)
    assert len(client.sent_batches) == 1
    assert result.success_count == 0
    assert result.fail_count == 4
    assert svc._cursors[7] == BATCH_SIZE


# ── F2: a non-troop refusal must stop the loop too ───────────────────────


@pytest.mark.parametrize(
    "batch_error",
    ["captcha required", "plus.error_something", "rate limited"],
)
def test_a_non_troop_refusal_stops_the_list(batch_error):
    """The one condition that stopped the loop was the BENIGN one.

    A batch whose body is `{"error": ...}` marks all N slots errored, but
    `"troops" not in error` so `batch_troop_errors == 0 != len(...)` and the
    loop proceeded. Measured `batches_fired=3/3, fail=12`: twelve dispatch
    attempts into a server that refused the first four.
    """
    client, svc, result = _run({"error": batch_error})

    assert len(client.sent_batches) == 1, "the server refused batch 1; stop asking"
    assert result.success_count == 0
    assert result.fail_count == BATCH_SIZE
    assert all(batch_error in t.error for t in result.targets)


def test_an_unreadable_batch_does_not_stop_the_list_but_a_refusal_does():
    """The two shapes are different answers and must not be confused.

    An unreadable body means "I could not check", so the rest of the list
    still goes out (F1). A named refusal means "the server said no", so it
    does not.
    """
    unreadable, _svc, _r = _run({})
    refused, _svc2, _r2 = _run({"error": "captcha required"})
    assert len(unreadable.sent_batches) == 3
    assert len(refused.sent_batches) == 1
