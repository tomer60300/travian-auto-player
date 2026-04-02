"""Reports service for fetching and parsing Travian reports."""

from __future__ import annotations

from typing import List, Optional, Dict, Any

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

        for page in range(1, max_pages + 1):
            try:
                html = await self.client.get_html(f"/report/all?page={page}")
                page_reports = parse_report_list(html)

                if not page_reports:
                    logger.debug(f"No reports on page {page}, stopping")
                    break

                all_reports.extend(page_reports)

                # If fewer than 30 reports, no more pages
                if len(page_reports) < 30:
                    break

            except Exception as e:
                logger.warning(f"Failed to fetch reports page {page}: {e}")
                break

        logger.info(f"Fetched {len(all_reports)} reports from {page} pages")
        return all_reports

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

