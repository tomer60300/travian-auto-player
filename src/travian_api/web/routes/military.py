"""Military routes — scout and raid operations with tribe-aware scout safety."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travian_api.constants import SCOUT_UNITS, BuildingType, TribeType
from travian_api.exceptions import InvalidTargetError, MilitaryError, TravianError
from travian_api.web.rate_limit import action_limiter
from travian_api.web.sessions import TravianSession, get_travian_session, require_village_id

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
        session.tribe_id,
        scout_unit,
        amount,
        x,
        y,
        scout_type,
    )

    return await session.military_service._send_troops(
        x=x,
        y=y,
        troops=troops,
        event_type=_RAID_EVENT_TYPE,
        scout_target=scout_target_value,
        village_id=village_id,
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ScoutRequest(BaseModel):
    x: int = Field(..., description="Target X coordinate")
    y: int = Field(..., description="Target Y coordinate")
    amount: int = Field(1, description="Number of scouts to send")
    type: str = Field("resources", description="Scout type: 'resources' or 'defenses'")
    village_id: int | None = Field(None, description="Source village (default: active village)")


class RaidRequest(BaseModel):
    x: int = Field(..., description="Target X coordinate")
    y: int = Field(..., description="Target Y coordinate")
    troops: dict[str, int] = Field(..., description="Troops to send, e.g. {'t1': 50, 't2': 30}")
    village_id: int | None = Field(None, description="Source village (default: active village)")


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
    village_id = require_village_id(body.village_id)
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
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


@router.get("/troops")
async def get_available_troops(
    village_id: int | None = None,
    session: TravianSession = Depends(get_travian_session),
):
    """Get available (idle) troops at the rally point for a village."""
    vid = require_village_id(village_id)
    try:
        troops = await session.military_service.get_available_troops(vid)
        return troops
    except Exception as exc:
        logger.warning("Failed to get troops: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/smithy")
async def get_smithy_levels(
    village_id: int | None = None,
    session: TravianSession = Depends(get_travian_session),
):
    """Per-unit smithy research levels for the village.

    Returns ``{"smithy_level": int, "research": {t1..t10: level}, "found": bool}``.
    ``found=False`` means the village has no smithy built yet — research dict
    stays all zeros.
    """
    vid = require_village_id(village_id)
    try:
        smithy = await session.building_service.find_building(
            int(BuildingType.BLACKSMITH), village_id=vid
        )
        if not smithy:
            return {
                "smithy_level": 0,
                "research": {f"t{i}": 0 for i in range(1, 11)},
                "found": False,
            }
        research = await session.military_service.get_smithy_research_levels(
            smithy_slot=smithy.slot_id,
            village_id=vid,
            tribe_id=session.tribe_id or 0,
        )
        return {
            "smithy_level": smithy.level,
            "research": research,
            "found": True,
        }
    except TravianError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    except Exception as exc:
        logger.warning("Failed to get smithy levels: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/raid")
async def send_raid(
    body: RaidRequest,
    session: TravianSession = Depends(get_travian_session),
    _=Depends(action_limiter),
):
    """Send a raid to a target location."""
    village_id = require_village_id(body.village_id)
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
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=exc.message,
        ) from exc
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc
