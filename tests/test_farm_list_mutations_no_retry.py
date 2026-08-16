"""Farm-list mutations must not be transport-retried.

post_json now retries transient failures by default (the round-2 fix). For
non-idempotent calls that is dangerous: if Travian commits the create/add and
the response connection then drops, a retry makes a duplicate list or slot.
These tests pin that every farm-list mutation opts out with safe_to_retry=False.
"""

import asyncio

import pytest

from travian_api.services.farm_list_service import FarmListService


class _RecordingClient:
    """Captures the kwargs of each write call; returns benign payloads."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def post_json(self, url, data=None, **kwargs):
        self.calls.append(("post_json", kwargs))
        return {"id": 1}

    async def delete_json(self, url, **kwargs):
        self.calls.append(("delete_json", kwargs))
        return {}


@pytest.fixture
def client_and_service():
    client = _RecordingClient()
    return client, FarmListService(client)


def test_create_farm_list_opts_out_of_retries(client_and_service):
    client, svc = client_and_service
    asyncio.run(svc.create_farm_list(village_id=1, name="raids"))
    assert client.calls[-1][1].get("safe_to_retry") is False


def test_add_slot_opts_out_of_retries(client_and_service):
    client, svc = client_and_service
    asyncio.run(svc.add_slot(list_id=1, x=10, y=20))
    assert client.calls[-1][1].get("safe_to_retry") is False


def test_delete_farm_list_opts_out_of_retries(client_and_service):
    client, svc = client_and_service
    asyncio.run(svc.delete_farm_list(list_id=1))
    assert client.calls[-1][1].get("safe_to_retry") is False


def test_delete_slots_opts_out_of_retries(client_and_service):
    client, svc = client_and_service
    asyncio.run(svc.delete_slots(slot_ids=[1, 2, 3]))
    assert client.calls[-1][1].get("safe_to_retry") is False
