"""Reports routes — list and view Travian reports."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

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
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.get("/{report_id}")
async def get_report(
    report_id: str,
    session: TravianSession = Depends(get_travian_session),
):
    """Fetch a detailed report by ID."""
    try:
        detail = await session.reports_service.fetch_report_detail(report_id)
        # fetch_report_detail already returns a dict
        return detail
    except ReportNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message,
        ) from exc
    except ReportError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
