"""Player status export route — collects data across all villages."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from travian_api.constants import TROOP_MAPPINGS, TribeType
from travian_api.exceptions import TravianError
from travian_api.web.sessions import TravianSession, get_travian_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/status", tags=["status"])


def _troop_name(tribe_id: int, key: str) -> str:
    """Resolve t1-t10 key to a human-readable troop name."""
    mapping = TROOP_MAPPINGS.get(TribeType(tribe_id), {})
    return mapping.get(key, key)


@router.get("/export")
async def export_player_status(
    include_buildings: bool = False,
    session: TravianSession = Depends(get_travian_session),
):
    """Collect resources, production and troops for every village.

    Resources and troops come from the account-wide /village/statistics tables,
    which render every village per page — four requests for the whole account
    regardless of village count. Building levels are the only genuinely
    per-village data (dorf1 + dorf2 each), so they are opt-in: setting
    ``include_buildings`` adds two throttled requests per village, which on a
    large account takes minutes.
    """
    if session.auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No auth state — reconnect first.",
        )

    tribe_id = session.tribe_id or 0

    try:
        resources_by_village = await session.building_service.get_all_villages_resources()
        troops_by_village = await session.military_service.get_all_villages_troops(tribe_id)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to read village statistics: {exc}",
        ) from exc

    villages_out = []

    for village in session.auth_state.villages:
        vid = village.id
        resources = resources_by_village.get(vid)
        troops_raw = troops_by_village.get(vid, {})

        # Map troop keys to readable names and drop zeroes
        troops = {_troop_name(tribe_id, k): v for k, v in sorted(troops_raw.items()) if v > 0}

        entry = {
            "id": vid,
            "name": village.name,
            "x": village.x,
            "y": village.y,
            "resources": resources.model_dump() if resources is not None else None,
            "troops": troops,
        }

        if include_buildings:
            try:
                buildings, _ = await session.building_service.get_village_snapshot(village_id=vid)
                entry["buildings"] = [b.model_dump() for b in buildings]
            except Exception as exc:
                logger.warning("Failed to fetch buildings for village %s: %s", vid, exc)
                entry["error"] = str(exc)

        villages_out.append(entry)

    return {
        "player_name": session.player_name or "Unknown",
        "tribe_id": tribe_id,
        "include_buildings": include_buildings,
        "villages": villages_out,
    }
