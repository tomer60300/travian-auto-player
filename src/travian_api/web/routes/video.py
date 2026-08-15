"""Video reward routes — claim ad-based rewards."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from travian_api.exceptions import TravianError
from travian_api.web.sessions import TravianSession, get_travian_session, require_village_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/video", tags=["video"])

# Production boost types to iterate when claiming all
_PRODUCTION_BOOST_TYPES = [
    "productionBoost",
    "lumberProductionBonus",
    "clayProductionBonus",
    "ironProductionBonus",
    "cropProductionBonus",
]

# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class VideoClaimRequest(BaseModel):
    type: str  # reward type string
    village_id: int | None = None
    slot_id: int | None = None
    building_id: int | None = None


class VideoClaimAllRequest(BaseModel):
    # Village switching is client-side only, so the session default is forever
    # the login village — the UI passes its own selection.
    village_id: int | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/claim")
async def claim_reward(
    body: VideoClaimRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Claim a single video reward."""
    village_id = require_village_id(body.village_id)

    extra_params: dict = {}
    if village_id is not None:
        extra_params["villageId"] = village_id
    if body.slot_id is not None:
        extra_params["slotId"] = body.slot_id
    if body.building_id is not None:
        extra_params["buildingId"] = body.building_id

    try:
        result = await session.video_service.claim_reward(
            reward_type=body.type,
            **extra_params,
        )
        # VideoRewardResult is a plain class, not Pydantic — return a dict manually
        return {
            "success": result.success,
            "reward_type": result.reward_type,
            "message": result.message,
        }
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=exc.message,
        ) from exc


@router.post("/claim-all")
async def claim_all_production_boosts(
    body: VideoClaimAllRequest | None = None,
    session: TravianSession = Depends(get_travian_session),
):
    """Claim all production boost video rewards."""
    village_id = require_village_id(body.village_id if body else None)
    results = []

    for reward_type in _PRODUCTION_BOOST_TYPES:
        extra_params: dict = {}
        if village_id is not None:
            extra_params["villageId"] = village_id

        try:
            result = await session.video_service.claim_reward(
                reward_type=reward_type,
                **extra_params,
            )
            results.append(
                {
                    "success": result.success,
                    "reward_type": result.reward_type,
                    "message": result.message,
                }
            )
        except TravianError as exc:
            results.append(
                {
                    "success": False,
                    "reward_type": reward_type,
                    "message": exc.message,
                }
            )

    return {
        "results": results,
        "total": len(results),
        "succeeded": sum(1 for r in results if r["success"]),
    }
