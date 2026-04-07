"""Military routes — scout and raid operations with tribe-aware scout safety."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from travian_api.constants import SCOUT_UNITS, TribeType
from travian_api.exceptions import TravianError, MilitaryError, InvalidTargetError
from travian_api.web.rate_limit import action_limiter
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/military", tags=["military"])

# ---------------------------------------------------------------------------
# Scout troop safety
# ---------------------------------------------------------------------------

# Event type 4 = raid (used for scout dispatch)
_RAID_EVENT_TYPE = 4


def _resolve_scout_unit(tribe_id: int | None) -> str:
    """Return the correct scout troop slot for the player's tribe.

    Romans: t4 (Equites Legati)
    Teutons: t4 (Scout)
    Gauls: t3 (Pathfinder)

    Falls back to t4 if tribe is unknown.
    """
    if tribe_id is None:
        raise ValueError("Cannot send scouts: tribe_id is not set on session")
    return SCOUT_UNITS.get(tribe_id, SCOUT_UNITS.get(TribeType.TEUTONS, "t4"))


async def _send_scouts_safe(
    session: TravianSession,
    x: int,
    y: int,
    amount: int,
    scout_type: str = "resources",
    village_id: int | None = None,
):
    """Tribe-aware scout send — bypasses the hardcoded t4 in MilitaryService.send_scouts().

    Resolves the correct troop slot from session.tribe_id, then calls
    _send_troops() directly with the right troops dict.
    """
    scout_unit = _resolve_scout_unit(session.tribe_id)
    troops = {scout_unit: amount}
    scout_target_value = "1" if scout_type == "resources" else "2"

    logger.info(
        "Tribe-safe scout: tribe=%s unit=%s amount=%d target=(%d,%d) type=%s",
        session.tribe_id, scout_unit, amount, x, y, scout_type,
    )

    return await session.military_service._send_troops(
        x=x, y=y,
        troops=troops,
        event_type=_RAID_EVENT_TYPE,
        scout_target=scout_target_value,
        village_id=village_id,
    )


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
    _=Depends(action_limiter),
):
    """Send scouts to a target location (tribe-aware — correct troop slot)."""
    village_id = body.village_id or session.active_village_id
    try:
        result = await _send_scouts_safe(
            session=session,
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
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("/raid")
async def send_raid(
    body: RaidRequest,
    session: TravianSession = Depends(get_travian_session),
    _=Depends(action_limiter),
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
