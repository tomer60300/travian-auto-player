"""Reports service for fetching and parsing Travian reports."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from ..clients.http_client import HttpClient
from ..exceptions import ReportError
from ..logging_config import get_logger
from ..models.reports import ReportListItem
from ..parsers.report_parser import (
    parse_individual_report,
    parse_map_tile_reports,
    parse_report_list,
)

logger = get_logger(__name__)


class ReportsService:
    """Handles report-related operations."""

    def __init__(self, client: HttpClient) -> None:
        self.client = client

    async def fetch_reports(
        self,
        max_age_hours: Optional[int] = None,
        max_pages: int = 10,
    ) -> List[ReportListItem]:
        """
        Fetch reports from /report/all pages.

        Args:
            max_age_hours: Ignored here -- kept for call-site compatibility.
                Age-based early exit lives in :meth:`fetch_reports_robust`;
                adding it here would drop reports this method returns today.
            max_pages: Maximum pages to scrape (30 reports/page)

        Returns:
            List of ReportListItem objects
        """
        all_reports: List[ReportListItem] = []
        first_page_size = None
        pages_fetched = 0

        for page in range(1, max_pages + 1):
            try:
                html = await self.client.get_html(f"/report/all?page={page}")
                # BeautifulSoup over a full page is tens of milliseconds, and
                # max_pages runs to 100. Off the loop, so a long list of pages
                # cannot stall stealth-timed requests or WebSocket frames.
                page_reports = await asyncio.to_thread(parse_report_list, html)
                pages_fetched += 1

                if not page_reports:
                    logger.debug(f"No reports on page {page}, stopping")
                    break

                all_reports.extend(page_reports)

                if first_page_size is None:
                    first_page_size = len(page_reports)
                elif len(page_reports) < first_page_size:
                    break

            except Exception as e:
                logger.warning(f"Failed to fetch reports page {page}: {e}")
                break

        logger.info(f"Fetched {len(all_reports)} reports from {pages_fetched} pages")
        return all_reports

    async def fetch_reports_robust(
        self,
        max_age_hours: Optional[int] = None,
        max_pages: int = 100,
    ) -> Tuple[List[ReportListItem], int, int, List[int]]:
        """
        Fetch reports from /report/all pages with robust error handling.

        Keeps fetching pages until:
        - All reports on a page are older than *max_age_hours*, OR
        - No more pages remain, OR
        - *max_pages* safety cap is reached.

        Args:
            max_age_hours: Stop fetching when the oldest report on a page
                exceeds this age.  None = no age limit (use max_pages only).
            max_pages: Hard safety cap on pages (default 100 = 3 000 reports).

        Returns:
            Tuple of (reports, pages_fetched, pages_failed, failed_page_numbers)
        """
        from datetime import datetime, timedelta

        from ..services.raid_analyzer_service import parse_report_date

        all_reports: List[ReportListItem] = []
        pages_fetched = 0
        pages_failed = 0
        failed_pages: List[int] = []
        first_page_size: Optional[int] = None
        cutoff = None
        if max_age_hours is not None:
            cutoff = datetime.now() - timedelta(hours=max_age_hours)

        for page in range(1, max_pages + 1):
            try:
                html = await self.client.get_html(f"/report/all?page={page}")
                page_reports = parse_report_list(html)
                pages_fetched += 1

                if not page_reports:
                    logger.debug(f"No reports on page {page}, stopping")
                    break

                all_reports.extend(page_reports)

                # Detect page size from the first full page
                if first_page_size is None:
                    first_page_size = len(page_reports)
                elif len(page_reports) < first_page_size:
                    # Fewer reports than a full page → last page
                    break

                # Stop when the last (oldest) report on the page is beyond cutoff
                if cutoff is not None:
                    oldest_date = parse_report_date(page_reports[-1].date_str)
                    if oldest_date and oldest_date < cutoff:
                        logger.debug(
                            f"Page {page}: oldest report ({oldest_date}) exceeds max age, stopping"
                        )
                        break

            except Exception as e:
                logger.error(f"Failed to fetch reports page {page}: {e}")
                pages_failed += 1
                failed_pages.append(page)
                continue  # Continue to next page, do NOT break

        logger.info(
            f"Fetched {len(all_reports)} reports from {pages_fetched} pages ({pages_failed} failed)"
        )
        return all_reports, pages_fetched, pages_failed, failed_pages

    async def fetch_report_detail(self, report_id: str) -> Dict[str, Any]:
        """
        Fetch and parse an individual report.

        Args:
            report_id: Report ID

        Returns:
            Parsed report data dict
        """
        try:
            html = await self.client.get_html(f"/report?id={report_id}")
            result = parse_individual_report(html)
            result["report_id"] = report_id
            return result
        except Exception as e:
            raise ReportError(f"Failed to fetch report {report_id}: {e}") from e

    async def fetch_report_batch_metadata(self, report_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        Fetch metadata for multiple reports using GraphQL batch.

        Args:
            report_ids: List of report IDs (up to 250)

        Returns:
            Dict mapping report_id -> {time, title, defender{playerName, village{id,name,x,y}}}
        """
        if not report_ids:
            return {}

        # Build batched GraphQL query
        aliases = []
        for i, rid in enumerate(report_ids[:250]):
            aliases.append(
                f'r{i}:report(objectId:"{rid}"){{time title defender{{playerName village{{id name x y}}}}}}'
            )

        query = "{" + " ".join(aliases) + "}"

        try:
            response = await self.client.post_json(
                "/api/v1/graphql", {"query": query, "variables": {}}
            )

            data = response.get("data", {})
            metadata: Dict[str, Dict[str, Any]] = {}

            for i, rid in enumerate(report_ids[:250]):
                alias = f"r{i}"
                if alias in data and data[alias]:
                    metadata[rid] = data[alias]

            return metadata

        except Exception as e:
            # NOT `return {}`: the caller cannot tell that apart from "the game
            # named no metadata for these reports", so a village whose metadata
            # could not be read was filtered as though it had none. Raising
            # lets the analyzer count it and put it in the `warnings` list it
            # already renders, the way its sibling village-metadata batch does.
            raise ReportError(
                f"Failed to fetch metadata for {len(report_ids[:250])} report(s): {e}"
            ) from e

    async def fetch_alliance_reports(
        self,
        max_age_hours: Optional[int] = None,
        max_pages: int = 100,
    ) -> Tuple[List[ReportListItem], bool]:
        """
        Fetch alliance shared reports via /alliance/reports?filter=...

        Uses battle+scout report type filters and robust pagination
        with dynamic page size detection and age-based cutoff.

        Returns:
            Tuple of (reports_list, success_bool)
        """
        from datetime import datetime, timedelta

        from ..parsers.report_parser import parse_alliance_report_list
        from ..services.raid_analyzer_service import parse_report_date

        ALLIANCE_FILTER = "1,2,3,4,5,6,7,15,16,17,18,19"
        base_url = f"/alliance/reports?filter={ALLIANCE_FILTER}"

        # Test if the route works
        try:
            html = await self.client.get_html(base_url)
            first_page = parse_alliance_report_list(html)
            if not first_page:
                logger.warning("Alliance reports: /alliance/reports returned 0 reports")
                return [], False
        except Exception as e:
            logger.warning(f"Alliance reports: /alliance/reports failed -- {e}")
            return [], False

        # Route works -- fetch all pages
        all_reports: List[ReportListItem] = list(first_page)
        first_page_size = len(first_page)
        pages_fetched = 1
        pages_failed = 0

        cutoff = None
        if max_age_hours is not None:
            cutoff = datetime.now() - timedelta(hours=max_age_hours)

        # Check age cutoff on first page
        if cutoff is not None and first_page:
            oldest_date = parse_report_date(first_page[-1].date_str)
            if oldest_date and oldest_date < cutoff:
                logger.info(f"Alliance reports: {len(all_reports)} from 1 page (age cutoff hit)")
                return all_reports, True

        for page in range(2, max_pages + 1):
            try:
                page_html = await self.client.get_html(f"{base_url}&page={page}")
                page_reports = parse_alliance_report_list(page_html)
                pages_fetched += 1

                if not page_reports:
                    break

                all_reports.extend(page_reports)

                # Dynamic page size: stop when page has fewer reports than first page
                if len(page_reports) < first_page_size:
                    break

                # Age-based cutoff
                if cutoff is not None:
                    oldest_date = parse_report_date(page_reports[-1].date_str)
                    if oldest_date and oldest_date < cutoff:
                        logger.debug(
                            f"Alliance page {page}: oldest report exceeds max age, stopping"
                        )
                        break

            except Exception as e:
                logger.error(f"Alliance reports page {page} failed: {e}")
                pages_failed += 1
                continue

        logger.info(
            f"Alliance reports: {len(all_reports)} from {pages_fetched} pages "
            f"({pages_failed} failed)"
        )
        return all_reports, True

    # ------------------------------------------------------------------
    # Village reports from map tile
    # ------------------------------------------------------------------

    async def fetch_village_reports(
        self,
        x: int,
        y: int,
        fetch_details: bool = False,
        max_detail_count: Optional[int] = None,
        detail_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch all visible reports for a village from its map tile page.

        Uses ``/karte.php?x=X&y=Y`` which shows **both personal and alliance
        reports** in the tile popup — even for villages you never attacked.

        Stealth:
        - Navigates to the map page first (human wouldn't jump straight to a tile)
        - Human reading delay between report detail fetches
        - All requests go through the global throttler via ``get_html``

        Args:
            x: Target village X coordinate.
            y: Target village Y coordinate.
            fetch_details: Also fetch full HTML detail for each report.
            max_detail_count: Cap on how many details to fetch (``None`` = all).

        Returns:
            Dict with ``village`` (metadata dict) and ``reports`` (list of
            summary dicts). If *fetch_details*, each report entry gains a
            ``detail`` key with the full parsed report data.
        """
        from ..stealth.human_delay import ActionType

        try:
            # Stealth: navigate to the map first (like clicking "Map" in the menu)
            navigator = self.client.navigator
            if navigator.enabled:
                if not navigator.current_page or "karte" not in navigator.current_page:
                    await navigator._visit("/karte.php", "opening map")
                await self.client.human_delay.wait(ActionType.CLICK, "clicking map tile")

            # The tile popup is loaded via the tile-details API (not karte.php HTML)
            resp = await self.client.post_json("/api/v1/map/tile-details", {"x": x, "y": y})
            html = resp.get("html", "")
            if not html:
                raise ReportError(f"Empty tile-details response for ({x}, {y})")
        except ReportError:
            raise
        except Exception as e:
            raise ReportError(f"Failed to fetch map tile for ({x}, {y}): {e}") from e

        parsed = parse_map_tile_reports(html)
        reports = parsed.get("reports", [])
        logger.info("Found %d reports on map tile (%d, %d)", len(reports), x, y)

        if fetch_details and reports:
            limit = max_detail_count if max_detail_count is not None else len(reports)
            fetched = 0
            for entry in reports[:limit]:
                # Optional: only fetch details for specific icon types
                # detail_filter="battle_only" skips scouts, "scout_priority" fetches scouts first
                if detail_filter == "battle_only" and entry.get("icon_type", 0) not in (
                    1,
                    2,
                    3,
                    4,
                    5,
                    6,
                    7,
                    8,
                ):
                    continue
                rid = entry["report_id"]
                aid = entry.get("aid", "")
                try:
                    # Stealth: human reading delay before clicking next report
                    if fetched > 0 and navigator.enabled:
                        await self.client.human_delay.wait(ActionType.PAGE_LOAD, "reading report")

                    url = f"/report?id={rid}"
                    if aid:
                        url += f"&aid={aid}"
                    detail_html = await self.client.get_html(url)
                    detail = parse_individual_report(detail_html)
                    detail["report_id"] = rid
                    data = detail.get("data")
                    if data and hasattr(data, "model_dump"):
                        detail["data"] = data.model_dump()
                    entry["detail"] = detail
                    fetched += 1
                except Exception as e:
                    logger.warning("Failed to fetch detail for report %s: %s", rid, e)
            parsed["details_fetched"] = fetched

        return parsed
