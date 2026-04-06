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
# Analyze
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    village_id: int | None = None
    min_resources: int = 200
    max_report_age_hours: int = 24
    max_pages: int = 20
    exclude_alliances: list[str] = Field(default_factory=list)
    exclude_players: list[str] = Field(default_factory=list)
    smithy_level: int = 0
    hero_offense: int = 0
    hero_strength: int = 0
    radius: float | None = None


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
        smithy_level=body.smithy_level,
        hero_offense=body.hero_offense,
        hero_strength=body.hero_strength,
        radius=body.radius,
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
