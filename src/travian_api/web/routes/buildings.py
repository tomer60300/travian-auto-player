"""Building management routes."""

import logging

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from travian_api.web.rate_limit import action_limiter
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/buildings", tags=["buildings"])

# ---------------------------------------------------------------------------
# Pydantic request/response schemas
# ---------------------------------------------------------------------------


class UpgradeRequest(BaseModel):
    slot_id: int = Field(..., ge=1, le=40, description="Building slot to upgrade")
    allow_gold: bool = Field(default=False, description="Allow spending gold on master builder")


class ConstructRequest(BaseModel):
    slot_id: int = Field(..., ge=19, le=40, description="Empty building slot (19-40)")
    building_name: str = Field(..., description="Building name (used to resolve GID)")
    building_gid: int | None = Field(
        default=None, description="Building GID (takes precedence over building_name)"
    )
    allow_gold: bool = Field(default=False, description="Allow spending gold on master builder")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
async def list_buildings(
    village_id: Optional[int] = Query(None, description="Village ID (default: active village)"),
    session: TravianSession = Depends(get_travian_session),
):
    """List all buildings for a village."""
    vid = village_id or session.active_village_id
    try:
        buildings = await session.building_service.get_village_buildings(village_id=vid)
    except Exception as exc:
        logger.exception("Failed to get buildings for village %s", vid)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch buildings: {exc}",
        )

    return {
        "village_id": vid,
        "buildings": [b.model_dump() for b in buildings],
    }


@router.get("/resources")
async def get_resources(
    village_id: Optional[int] = Query(None, description="Village ID (default: active village)"),
    session: TravianSession = Depends(get_travian_session),
):
    """Get current resources, production rates, and storage capacity."""
    vid = village_id or session.active_village_id
    try:
        resources = await session.building_service.get_resources(village_id=vid)
    except Exception as exc:
        logger.exception("Failed to get resources for village %s", vid)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch resources: {exc}",
        )

    return {
        "village_id": vid,
        **resources.model_dump(),
    }


@router.get("/queue")
async def get_construction_queue(
    village_id: Optional[int] = Query(None, description="Village ID (default: active village)"),
    session: TravianSession = Depends(get_travian_session),
):
    """Get the active construction queue."""
    vid = village_id or session.active_village_id
    try:
        queue = await session.building_service.get_construction_queue(village_id=vid)
    except Exception as exc:
        logger.exception("Failed to get construction queue for village %s", vid)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch construction queue: {exc}",
        )

    return {
        "village_id": vid,
        "queue": [item.model_dump() for item in queue],
    }


@router.post("/upgrade")
async def upgrade_building(
    body: UpgradeRequest,
    session: TravianSession = Depends(get_travian_session),
    _=Depends(action_limiter),
):
    """Upgrade an existing building to the next level."""
    try:
        result = await session.building_service.upgrade_building(
            slot_id=body.slot_id,
            allow_gold=body.allow_gold,
            village_id=session.active_village_id,
        )
    except Exception as exc:
        logger.exception("Failed to upgrade building slot %s", body.slot_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to upgrade building: {exc}",
        )

    data = result.model_dump()
    # Strip raw_response from the API output — it can be very large and is only
    # useful for internal debugging.
    data.pop("raw_response", None)
    return data


@router.post("/construct")
async def construct_building(
    body: ConstructRequest,
    session: TravianSession = Depends(get_travian_session),
    _=Depends(action_limiter),
):
    """Construct a new building on an empty slot.

    Provide either ``building_gid`` (preferred) or ``building_name``.  When only
    ``building_name`` is given, the route looks up available buildings for the
    slot and matches by name (case-insensitive).
    """
    gid = body.building_gid

    # Resolve building_name -> gid if gid not provided
    if gid is None:
        try:
            available = await session.building_service.get_available_buildings(
                slot_id=body.slot_id,
                village_id=session.active_village_id,
            )
        except Exception as exc:
            logger.exception("Failed to list available buildings for slot %s", body.slot_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to list available buildings: {exc}",
            )

        search = body.building_name.lower()
        for b in available:
            if b.get("name", "").lower() == search:
                gid = b["gid"]
                break

        if gid is None:
            available_names = [b.get("name", "?") for b in available if b.get("can_build")]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Building '{body.building_name}' not found for slot {body.slot_id}. "
                    f"Available: {', '.join(available_names)}"
                ),
            )

    try:
        result = await session.building_service.construct_building(
            slot_id=body.slot_id,
            building_gid=gid,
            allow_gold=body.allow_gold,
            village_id=session.active_village_id,
        )
    except Exception as exc:
        logger.exception("Failed to construct building on slot %s", body.slot_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to construct building: {exc}",
        )

    data = result.model_dump()
    data.pop("raw_response", None)
    return data


@router.get("/{slot_id}")
async def get_building_detail(
    slot_id: int,
    session: TravianSession = Depends(get_travian_session),
):
    """Get detailed information for a specific building slot."""
    if not (1 <= slot_id <= 40):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="slot_id must be between 1 and 40",
        )

    try:
        detail = await session.building_service.get_building_detail(
            slot_id=slot_id,
            village_id=session.active_village_id,
        )
    except Exception as exc:
        logger.exception("Failed to get building detail for slot %s", slot_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch building detail: {exc}",
        )

    return {
        "village_id": session.active_village_id,
        **detail.model_dump(),
    }
