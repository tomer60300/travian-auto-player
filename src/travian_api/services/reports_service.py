"""Reports service for fetching and parsing Travian reports."""

from __future__ import annotations

from typing import List, Optional, Dict, Any, Tuple

from ..clients.http_client import HttpClient
from ..exceptions import ReportError
from ..logging_config import get_logger
from ..models.reports import ReportListItem, Report
from ..parsers.report_parser import (
    parse_report_list,
    parse_individual_report,
    parse_scout_report,
    parse_battle_report,
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
            max_age_hours: Only fetch reports newer than this (basic date filter)
            max_pages: Maximum pages to scrape (30 reports/page)

        Returns:
            List of ReportListItem objects
        """
        all_reports: List[ReportListItem] = []
        first_page_size = None

        for page in range(1, max_pages + 1):
            try:
                html = await self.client.get_html(f"/report/all?page={page}")
                page_reports = parse_report_list(html)

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

        logger.info(f"Fetched {len(all_reports)} reports from {page} pages")
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
        from ..services.raid_analyzer_service import parse_report_date
        from datetime import datetime, timedelta

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
                            f"Page {page}: oldest report ({oldest_date}) "
                            f"exceeds max age, stopping"
                        )
                        break

            except Exception as e:
                logger.error(f"Failed to fetch reports page {page}: {e}")
                pages_failed += 1
                failed_pages.append(page)
                continue  # Continue to next page, do NOT break

        logger.info(
            f"Fetched {len(all_reports)} reports from {pages_fetched} pages "
            f"({pages_failed} failed)"
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
            result['report_id'] = report_id
            return result
        except Exception as e:
            raise ReportError(f"Failed to fetch report {report_id}: {e}") from e

    async def fetch_report_batch_metadata(
        self, report_ids: List[str]
    ) -> Dict[str, Dict[str, Any]]:
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
            logger.warning(f"Failed to fetch report metadata: {e}")
            return {}

    async def fetch_alliance_reports(
        self,
        max_age_hours: Optional[int] = None,
        max_pages: int = 100,
    ) -> Tuple[List[ReportListItem], bool]:
        """
        Fetch alliance shared reports using /report/all?allianceReports=1.

        Uses the same robust pagination as fetch_reports_robust():
        dynamic page size detection and age-based cutoff.

        Returns:
            Tuple of (reports_list, success_bool)
        """
        from ..services.raid_analyzer_service import parse_report_date
        from datetime import datetime, timedelta

        # Test if the alliance report route works at all
        try:
            html = await self.client.get_html("/report/all?allianceReports=1&page=1")
            first_page = parse_report_list(html)
            if not first_page:
                logger.warning("Alliance reports: route returned 0 reports")
                return [], False
        except Exception as e:
            logger.warning(f"Alliance reports: route failed — {e}")
            return [], False

        # Route works — fetch all pages with same robust logic as personal reports
        all_reports: List[ReportListItem] = list(first_page)
        first_page_size = len(first_page)
        pages_fetched = 1
        pages_failed = 0

        cutoff = None
        if max_age_hours is not None:
            cutoff = datetime.now() - timedelta(hours=max_age_hours)

        # Check age cutoff on first page
        if cutoff is not None:
            oldest_date = parse_report_date(first_page[-1].date_str)
            if oldest_date and oldest_date < cutoff:
                logger.info(
                    f"Alliance reports: {len(all_reports)} from 1 page (age cutoff hit)"
                )
                return all_reports, True

        for page in range(2, max_pages + 1):
            try:
                page_html = await self.client.get_html(
                    f"/report/all?allianceReports=1&page={page}"
                )
                page_reports = parse_report_list(page_html)
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
                            f"Alliance page {page}: oldest report ({oldest_date}) "
                            f"exceeds max age, stopping"
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
