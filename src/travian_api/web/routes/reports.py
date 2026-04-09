"""Reports routes — list, view, and analyze Travian reports."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from travian_api.exceptions import TravianError, ReportError, ReportNotFoundError
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"])

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_reports(
    max_age_hours: Optional[int] = Query(default=None, description="Only fetch reports newer than this many hours"),
    max_pages: int = Query(default=10, ge=1, le=100, description="Maximum pages to fetch"),
    session: TravianSession = Depends(get_travian_session),
):
    """List recent reports."""
    try:
        reports = await session.reports_service.fetch_reports(
            max_age_hours=max_age_hours,
            max_pages=max_pages,
        )
        return [r.model_dump() for r in reports]
    except ReportError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    except TravianError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc


@router.get("/{report_id}")
async def get_report(
    report_id: str,
    session: TravianSession = Depends(get_travian_session),
):
    """Fetch a detailed report by ID."""
    try:
        detail = await session.reports_service.fetch_report_detail(report_id)
        return detail
    except ReportNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.message) from exc
    except ReportError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc
    except TravianError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.message) from exc


# ---------------------------------------------------------------------------
# Village reports from map tile
# ---------------------------------------------------------------------------


class VillageReportsRequest(BaseModel):
    x: int
    y: int
    fetch_details: bool = False
    max_detail_count: int | None = None


@router.post("/village-reports")
async def village_reports(
    body: VillageReportsRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Fetch all visible reports for a village from its map tile page.

    Returns own + alliance reports from ``/karte.php?x=X&y=Y``.
    """
    try:
        result = await session.reports_service.fetch_village_reports(
            x=body.x, y=body.y,
            fetch_details=body.fetch_details,
            max_detail_count=body.max_detail_count,
        )
        return result
    except Exception as exc:
        logger.exception("Village reports failed for (%s, %s)", body.x, body.y)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch village reports: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Analyze
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    village_id: int | None = None
    min_resources: int = 200
    max_report_age_hours: int = 24
    max_pages: int = 20
    exclude_alliances: list[str] = Field(default_factory=list)
    exclude_players: list[str] = Field(default_factory=list)
    nap_alliances: list[str] = Field(default_factory=list)
    max_population: int | None = None
    smithy_level: int = 0
    hero_offense: int = 0
    hero_strength: int = 0
    radius: float | None = None
    stale_hours: float = 12.0
    cache_ttl_minutes: int = 30


@router.post("/analyze")
async def analyze_reports(
    body: AnalyzeRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Analyze raid reports and return prioritized target recommendations."""
    from travian_api.models.raid_analyzer import AnalyzerSettings

    village_id = body.village_id or session.active_village_id

    settings = AnalyzerSettings(
        village_id=village_id,
        min_resources=body.min_resources,
        max_report_age_hours=body.max_report_age_hours,
        max_pages=body.max_pages,
        exclude_alliances=body.exclude_alliances,
        exclude_players=body.exclude_players,
        nap_alliances=body.nap_alliances,
        max_population=body.max_population,
        smithy_level=body.smithy_level,
        hero_offense=body.hero_offense,
        hero_strength=body.hero_strength,
        radius=body.radius,
        stale_hours=body.stale_hours,
        cache_ttl_minutes=body.cache_ttl_minutes,
        output_json=True,
    )

    try:
        result = await session.raid_analyzer.analyze(settings)

        targets = []
        for target_state, rec in result.targets:
            targets.append({
                "state": target_state.model_dump(mode="json"),
                "recommendation": rec.model_dump(mode="json"),
            })

        return {
            "source_village": result.source_village_name,
            "source_coords": f"({result.source_x}, {result.source_y})",
            "total_targets": len(targets),
            "filters": {
                "min_resources": body.min_resources,
                "max_report_age_hours": body.max_report_age_hours,
                "radius": body.radius,
            },
            "targets": targets,
            "re_scout_targets": [t.model_dump(mode="json") for t in result.re_scout_targets],
            "diagnostics": {
                "pipeline_version": result.pipeline_version,
                "total_reports_listed": result.total_reports_listed,
                "unique_coords_discovered": result.unique_coords_discovered,
                "coords_after_gql_filter": result.coords_after_gql_filter,
                "village_reports_fetched": result.village_reports_fetched,
                "village_reports_cached": result.village_reports_cached,
                "village_reports_failed": result.village_reports_failed,
                "reports_fetched_ok": result.reports_fetched_ok,
                "pages_fetched": result.pages_fetched,
                "pages_failed": result.pages_failed,
                "analysis_duration_seconds": round(result.analysis_duration_seconds, 1),
                "skipped_needs_scout": result.skipped_needs_scout,
                "skipped_low_resources": result.skipped_low_resources,
                "skipped_out_of_range": result.skipped_out_of_range,
                "skipped_alliance": result.skipped_alliance,
                "warnings": result.warnings,
            },
        }

    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Analysis failed: {exc.message}",
        ) from exc
    except Exception as exc:
        logger.exception("Report analysis failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Analysis failed: {exc}",
        ) from exc
