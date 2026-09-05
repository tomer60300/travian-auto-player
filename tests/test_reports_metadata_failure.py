"""A metadata read that FAILED must not look like a village with no metadata.

`fetch_report_batch_metadata` caught every exception and returned `{}`, so its
caller could not tell "the game named no metadata for these reports" from "the
request never landed" — and a village whose metadata could not be read was
filtered as though it had none. The sibling village-metadata batch in
`raid_analyzer_service` gets this right: it logs AND appends to the rendered
`warnings` list.
"""

import asyncio
from types import SimpleNamespace

import pytest

from travian_api.exceptions import ReportError
from travian_api.services.reports_service import ReportsService

IDS = ["r1", "r2"]


def _service(answer):
    async def post_json(url, payload, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    client = SimpleNamespace(
        post_json=post_json,
        base_url="https://example.invalid",
        settings=SimpleNamespace(base_url="https://example.invalid"),
    )
    return ReportsService(client)


def test_no_metadata_is_an_empty_result():
    svc = _service({"data": {}})
    assert asyncio.run(svc.fetch_report_batch_metadata(IDS)) == {}


def test_metadata_that_arrives_is_returned():
    svc = _service({"data": {"r0": {"title": "raid"}, "r1": {"title": "scout"}}})
    out = asyncio.run(svc.fetch_report_batch_metadata(IDS))
    assert out == {"r1": {"title": "raid"}, "r2": {"title": "scout"}}


def test_an_empty_request_is_still_an_empty_result():
    svc = _service({"data": {}})
    assert asyncio.run(svc.fetch_report_batch_metadata([])) == {}


@pytest.mark.parametrize(
    "boom",
    [RuntimeError("connection reset"), ValueError("not json")],
)
def test_a_failed_read_raises_instead_of_reporting_no_metadata(boom):
    svc = _service(boom)
    with pytest.raises(ReportError) as exc:
        asyncio.run(svc.fetch_report_batch_metadata(IDS))
    assert "metadata" in str(exc.value)
    assert str(boom) in str(exc.value)
