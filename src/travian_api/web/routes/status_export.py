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

    Troops come from the account-wide /village/statistics/troops table — one
    request for the whole account. Resources are read per village from dorf1:
    the account-wide production table only carries GROSS crop, while the
    ``var resources`` blob holds the net rate (production minus troop feeding,
    negative when starving) plus free crop. Setting ``include_buildings`` adds
    a dorf2 fetch per village for building levels; resources then ride along
    on the dorf1 fetch the snapshot already makes.
    """
    if session.auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No auth state — reconnect first.",
        )

    tribe_id = session.tribe_id or 0

    try:
        troops_by_village = await session.military_service.get_all_villages_troops(tribe_id)
    except TravianError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to read village statistics: {exc}",
        ) from exc

    villages_out = []

    for village in session.auth_state.villages:
        vid = village.id
        troops_raw = troops_by_village.get(vid, {})

        # Map troop keys to readable names and drop zeroes
        troops = {_troop_name(tribe_id, k): v for k, v in sorted(troops_raw.items()) if v > 0}

        entry = {
            "id": vid,
            "name": village.name,
            "x": village.x,
            "y": village.y,
            "resources": None,
            "troops": troops,
        }

        try:
            if include_buildings:
                buildings, resources = await session.building_service.get_village_snapshot(
                    village_id=vid
                )
                entry["buildings"] = [b.model_dump() for b in buildings]
            else:
                resources = await session.building_service.get_resources(village_id=vid)
            entry["resources"] = resources.model_dump()
        except Exception as exc:
            logger.warning("Failed to fetch village %s: %s", vid, exc)
            entry["error"] = str(exc)

        villages_out.append(entry)

    return {
        "player_name": session.player_name or "Unknown",
        "tribe_id": tribe_id,
        "include_buildings": include_buildings,
        "villages": villages_out,
    }
