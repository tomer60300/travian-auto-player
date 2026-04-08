"""Player status export route — collects data across all villages."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from travian_api.constants import TROOP_MAPPINGS, TribeType
from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/status", tags=["status"])


def _troop_name(tribe_id: int, key: str) -> str:
    """Resolve t1-t10 key to a human-readable troop name."""
    mapping = TROOP_MAPPINGS.get(TribeType(tribe_id), {})
    return mapping.get(key, key)


@router.get("/export")
async def export_player_status(
    session: TravianSession = Depends(get_travian_session),
):
    """Collect buildings, troops, resources and production for every village."""
    if session.auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No auth state — reconnect first.",
        )

    tribe_id = session.tribe_id or 0
    villages_out = []

    for village in session.auth_state.villages:
        vid = village.id

        # Fetch data for this village concurrently
        buildings_task = session.building_service.get_village_buildings(village_id=vid)
        resources_task = session.building_service.get_resources(village_id=vid)
        troops_task = session.military_service.get_village_troop_totals(
            village_id=vid, tribe_id=tribe_id,
        )

        try:
            buildings, resources, troops_raw = await asyncio.gather(
                buildings_task, resources_task, troops_task,
            )
        except Exception as exc:
            logger.warning("Failed to fetch data for village %s: %s", vid, exc)
            villages_out.append({
                "id": vid,
                "name": village.name,
                "x": village.x,
                "y": village.y,
                "error": str(exc),
            })
            continue

        # Map troop keys to readable names and drop zeroes
        troops = {
            _troop_name(tribe_id, k): v
            for k, v in sorted(troops_raw.items())
            if v > 0
        }

        villages_out.append({
            "id": vid,
            "name": village.name,
            "x": village.x,
            "y": village.y,
            "buildings": [b.model_dump() for b in buildings],
            "resources": resources.model_dump(),
            "troops": troops,
        })

    return {
        "player_name": session.player_name or "Unknown",
        "tribe_id": tribe_id,
        "villages": villages_out,
    }
