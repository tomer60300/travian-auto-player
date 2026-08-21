"""GET /api/reports may parse up to 100 report pages in one request.

``max_pages`` is bounded at 100 and every page is a BeautifulSoup parse over a
full HTML document -- tens of milliseconds each. Run inline, that is seconds of
event loop the stealth layer cannot use to time its own game requests, growing
linearly with ``max_pages``. The parse now happens in a worker thread; these
tests pin that, and that the pagination it drives is otherwise unchanged.
"""

import asyncio
import threading
import time

from travian_api.services import reports_service as reports_module
from travian_api.services.reports_service import ReportsService


def _page(rows: int, page: int = 1) -> str:
    body = "".join(
        f'<tr><td class="sel"><input name="ids[]" value="{page}{i:03d}" /></td>'
        f'<td><img class="iReport iReport4" />'
        f'<img class="messageStatus messageStatusUnread" /></td>'
        f'<td class="sub"><div><a href="/report?id={page}{i:03d}">Raid on V{i}</a></div></td>'
        f'<td class="dat">today, 11:15</td></tr>'
        for i in range(rows)
    )
    return f"<html><body><table><tbody>{body}</tbody></table></body></html>"


class _Client:
    """Serves canned /report/all pages; no network, no throttle."""

    def __init__(self, sizes: list[int]) -> None:
        self.sizes = sizes
        self.requested: list[int] = []

    async def get_html(self, path: str) -> str:
        page = int(path.rsplit("=", 1)[1])
        self.requested.append(page)
        rows = self.sizes[page - 1] if page <= len(self.sizes) else 0
        return _page(rows, page)


class _Ticker:
    """Counts how many times the event loop got to run."""

    def __init__(self) -> None:
        self.ticks = 0
        self._task: asyncio.Task | None = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(0)
            self.ticks += 1

    async def __aenter__(self) -> "_Ticker":
        self._task = asyncio.ensure_future(self._run())
        await asyncio.sleep(0)
        self.ticks = 0
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task is not None:
            self._task.cancel()


class TestParsingLeavesTheLoopFree:
    async def test_the_loop_runs_while_a_page_is_parsed(self, monkeypatch):
        def slow_parse(html: str) -> list:
            time.sleep(0.05)  # a page parse, exaggerated to be unmistakable
            return []

        monkeypatch.setattr(reports_module, "parse_report_list", slow_parse)
        service = ReportsService(_Client([30]))

        async with _Ticker() as ticker:
            await service.fetch_reports(max_pages=1)

        assert ticker.ticks > 1, "the page parse blocked the event loop"

    async def test_every_page_is_parsed_off_the_loop(self, monkeypatch):
        threads: set[int] = set()
        real = reports_module.parse_report_list

        def record(html: str) -> list:
            threads.add(threading.get_ident())
            return real(html)

        monkeypatch.setattr(reports_module, "parse_report_list", record)
        service = ReportsService(_Client([30, 30, 12]))

        await service.fetch_reports(max_pages=10)

        assert threads, "parse_report_list was never called"
        assert threading.main_thread().ident not in threads


class TestPaginationIsUnchanged:
    async def test_a_short_page_ends_the_walk(self):
        client = _Client([30, 30, 12, 30])
        service = ReportsService(client)

        reports = await service.fetch_reports(max_pages=10)

        assert client.requested == [1, 2, 3]
        assert len(reports) == 72
        assert reports[0].report_type == "battle"
        assert reports[0].is_read is False

    async def test_an_empty_page_ends_the_walk(self):
        client = _Client([30, 30])
        service = ReportsService(client)

        reports = await service.fetch_reports(max_pages=10)

        assert client.requested == [1, 2, 3]
        assert len(reports) == 60

    async def test_max_pages_caps_the_walk(self):
        client = _Client([30] * 10)
        service = ReportsService(client)

        reports = await service.fetch_reports(max_pages=3)

        assert client.requested == [1, 2, 3]
        assert len(reports) == 90

    async def test_a_failing_page_keeps_what_was_already_parsed(self):
        class Flaky(_Client):
            async def get_html(self, path: str) -> str:
                page = int(path.rsplit("=", 1)[1])
                if page == 2:
                    raise RuntimeError("boom")
                return await super().get_html(path)

        service = ReportsService(Flaky([30, 30, 30]))

        reports = await service.fetch_reports(max_pages=10)

        assert len(reports) == 30

    async def test_max_age_hours_does_not_change_the_result(self):
        """It is accepted for call-site compatibility and deliberately unused:
        an age-based early exit would drop reports this method returns today."""
        service = ReportsService(_Client([30, 30, 5]))
        everything = await service.fetch_reports(max_pages=10)

        service = ReportsService(_Client([30, 30, 5]))
        filtered = await service.fetch_reports(max_age_hours=1, max_pages=10)

        assert [r.report_id for r in filtered] == [r.report_id for r in everything]
