"""Village management routes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/villages", tags=["villages"])

# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class VillageInfo(BaseModel):
    """Single village summary."""
    id: int
    name: str
    x: int
    y: int
    is_main_village: bool
    is_active: bool = False


class VillageListResponse(BaseModel):
    """Response for the village list endpoint."""
    active_village_id: int | None
    villages: list[VillageInfo]


class SwitchVillageRequest(BaseModel):
    village_id: int = Field(..., description="Village ID to switch to")


class SwitchVillageResponse(BaseModel):
    """Response confirming the village switch (client-side only)."""
    active_village_id: int
    village: VillageInfo


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=VillageListResponse)
async def list_villages(
    session: TravianSession = Depends(get_travian_session),
):
    """Return all villages for the connected player."""
    if session.auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no auth state. Try reconnecting.",
        )

    villages = [
        VillageInfo(
            id=v.id,
            name=v.name,
            x=v.x,
            y=v.y,
            is_main_village=v.is_main_village,
            is_active=(v.id == session.active_village_id),
        )
        for v in session.auth_state.villages
    ]

    return VillageListResponse(
        active_village_id=session.active_village_id,
        villages=villages,
    )


@router.post("/switch", response_model=SwitchVillageResponse)
async def switch_village(
    body: SwitchVillageRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Switch the active village context.

    NOTE: This is now a client-side-only operation. The backend validates that the
    village belongs to the player but does NOT mutate session.active_village_id.
    Each browser tab tracks its own active village via sessionStorage. This prevents
    one tab/browser from affecting another's village context.
    """
    # Validate that the village belongs to this player (don't mutate session state)
    village = None
    if session.auth_state:
        for v in session.auth_state.villages:
            if v.id == body.village_id:
                village = VillageInfo(
                    id=v.id,
                    name=v.name,
                    x=v.x,
                    y=v.y,
                    is_main_village=v.is_main_village,
                    is_active=True,
                )
                break

    if village is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Village {body.village_id} not found for this player.",
        )

    return SwitchVillageResponse(
        active_village_id=body.village_id,
        village=village,
    )
