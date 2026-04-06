"""Military routes — scout and raid operations."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from travian_api.exceptions import TravianError, MilitaryError, InvalidTargetError
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/military", tags=["military"])

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ScoutRequest(BaseModel):
    x: int
    y: int
    amount: int = 1
    type: str = "resources"  # "resources" or "defenses"
    village_id: int | None = None


class RaidRequest(BaseModel):
    x: int
    y: int
    troops: dict[str, int]  # e.g. {"t1": 50, "t2": 30}
    village_id: int | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/scout")
async def send_scouts(
    body: ScoutRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Send scouts to a target location."""
    village_id = body.village_id or session.active_village_id
    try:
        result = await session.military_service.send_scouts(
            x=body.x,
            y=body.y,
            amount=body.amount,
            scout_type=body.type,
            village_id=village_id,
        )
        return result.model_dump()
    except InvalidTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc
    except MilitaryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post("/raid")
async def send_raid(
    body: RaidRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Send a raid to a target location."""
    village_id = body.village_id or session.active_village_id
    try:
        result = await session.military_service.send_raid(
            x=body.x,
            y=body.y,
            troops=body.troops,
            village_id=village_id,
        )
        return result.model_dump()
    except InvalidTargetError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=exc.message,
        ) from exc
    except MilitaryError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
