"""Farm-list mutations must read their response body.

`create_farm_list`, `delete_farm_list`, `add_slot` and `delete_slots`
discarded what the game answered, so every 2xx was a success: a body carrying
`{"error": "errorRaidListSlotLimit"}`, a per-slot `slots[].error`, or an HTML
soft-block (which `post_json` hands back as `{"response_text": ...}`) all came
back as "ok", the routes turned that into 201/204, and the UI toasted green.
A `create_farm_list` whose body carried no `id` returned 0 and was logged as
`Created farm list 'X' (id=0)`.

This is wave-1's trade-route finding on the same `/api/v1/*` API: fixed there
with `_rejected_routes` + `ToggleResponseUnreadable`, untouched here.
"""

import asyncio

import pytest

from travian_api.services.farm_list_service import (
    FarmListMutationRefused,
    FarmListMutationUnverified,
    FarmListService,
)


class _Client:
    def __init__(self, body):
        self._body = body
        self.calls: list[str] = []

    async def post_json(self, url, data=None, **kwargs):
        self.calls.append(url)
        return self._body

    async def delete_json(self, url, data=None, **kwargs):
        self.calls.append(url)
        return self._body


def _svc(body):
    client = _Client(body)
    return client, FarmListService(client)


MUTATIONS = {
    "add_slot": lambda svc: svc.add_slot(list_id=1, x=10, y=20),
    "delete_slots": lambda svc: svc.delete_slots(slot_ids=[7]),
    "delete_farm_list": lambda svc: svc.delete_farm_list(list_id=1),
}


REFUSALS = [
    {"error": "errorRaidListSlotLimit"},
    {"error": "raidList.targetExists"},
    {"slots": [{"id": 7, "error": "nope"}]},
]

UNREADABLE = [
    {"response_text": "<html>soft block</html>"},
    {"response_text": "[]"},
    None,
    [],
]

CLEAN = [
    {},
    {"response_text": ""},  # a genuinely empty 200/204 body names no refusal
    {"slots": [{"id": 7}]},
]


@pytest.mark.parametrize("body", REFUSALS)
@pytest.mark.parametrize("name", list(MUTATIONS))
def test_a_refusal_in_the_body_is_a_refusal(name, body):
    client, svc = _svc(body)
    with pytest.raises(FarmListMutationRefused):
        asyncio.run(MUTATIONS[name](svc))
    assert client.calls, "the request itself must still have been made"


@pytest.mark.parametrize("body", UNREADABLE)
@pytest.mark.parametrize("name", list(MUTATIONS))
def test_an_unreadable_body_is_unverified_not_success(name, body):
    client, svc = _svc(body)
    with pytest.raises(FarmListMutationUnverified) as exc:
        asyncio.run(MUTATIONS[name](svc))
    # The farm builder classifies a failure by this wording; see
    # farm_builder_service.answer_was_lost.
    assert "may already have taken effect" in str(exc.value)


@pytest.mark.parametrize("body", CLEAN)
@pytest.mark.parametrize("name", list(MUTATIONS))
def test_a_clean_body_still_succeeds(name, body):
    _client, svc = _svc(body)
    assert asyncio.run(MUTATIONS[name](svc)) is None


# ── create_farm_list ─────────────────────────────────────────────────────


def test_create_returns_the_id_the_game_gave():
    _client, svc = _svc({"id": 123})
    assert asyncio.run(svc.create_farm_list(village_id=1, name="raids")) == 123


@pytest.mark.parametrize("body", [{}, {"id": 0}, {"id": None}, {"response_text": ""}])
def test_create_never_answers_with_id_zero(body):
    _client, svc = _svc(body)
    with pytest.raises(FarmListMutationUnverified):
        asyncio.run(svc.create_farm_list(village_id=1, name="raids"))


@pytest.mark.parametrize("body", REFUSALS[:2])
def test_a_refused_create_raises(body):
    _client, svc = _svc(body)
    with pytest.raises(FarmListMutationRefused):
        asyncio.run(svc.create_farm_list(village_id=1, name="raids"))
