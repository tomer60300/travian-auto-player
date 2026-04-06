"""Scout routes -- map scanning and target discovery."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from travian_api.web.sessions import get_travian_session, TravianSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scout", tags=["scout"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ScanRequest(BaseModel):
    radius: int = Field(..., ge=1, le=100, description="Scan radius from village center")
    village_id: int | None = Field(None, description="Source village ID (default: active village)")
    max_pop: int | None = Field(None, description="Max population filter")
    min_pop: int | None = Field(None, description="Min population filter")
    no_player: bool = Field(False, description="Only show unoccupied villages")
    show_oases: bool = Field(False, description="Include oases in results")
    limit: int = Field(50, ge=1, le=500, description="Max results to return")


class MapTileResponse(BaseModel):
    x: int
    y: int
    village_id: int = 0
    player_id: int | None = None
    alliance_id: int | None = None
    village_name: str = ""
    player_name: str = ""
    tribe: str = ""
    population: int = 0
    distance: float = 0.0
    is_oasis: bool = False
    is_abandoned: bool = False


class ScanResponse(BaseModel):
    center_x: int
    center_y: int
    center_village: str
    radius: int
    total: int
    tiles: list[MapTileResponse]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/scan", response_model=ScanResponse)
async def scan_map(
    body: ScanRequest,
    session: TravianSession = Depends(get_travian_session),
):
    """Scan the map around a village and return filtered tile results."""
    if session.auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has no auth state. Try reconnecting.",
        )

    # Resolve center village
    vid = body.village_id or session.active_village_id
    center_village = next(
        (v for v in session.auth_state.villages if v.id == vid), None
    )
    if not center_village:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Village {vid} not found for this player.",
        )

    cx, cy = center_village.x, center_village.y
    svc = session.scout_service

    try:
        # Scan the map
        tiles = await svc.scan_map(cx, cy, body.radius)

        # Filter out own villages
        own_village_ids = {v.id for v in session.auth_state.villages}
        tiles = [t for t in tiles if t.village_id not in own_village_ids]

        # Filter non-oasis unless requested
        if not body.show_oases:
            tiles = [t for t in tiles if not t.is_oasis]

        # Only keep tiles with actual village IDs
        tiles = [t for t in tiles if t.village_id > 0]

        # Enrich with population data
        if tiles:
            tiles = await svc.enrich_tiles(tiles)

        # Apply filters via the service
        tiles = svc.filter_targets(
            tiles,
            max_population=body.max_pop,
            min_population=body.min_pop,
            only_no_player=body.no_player,
            exclude_oases=not body.show_oases,
        )

        # Apply limit
        tiles = tiles[: body.limit]

        tile_responses = [
            MapTileResponse(
                x=t.x,
                y=t.y,
                village_id=t.village_id,
                player_id=t.player_id,
                alliance_id=t.alliance_id,
                village_name=t.village_name,
                player_name=t.player_name,
                tribe=t.tribe,
                population=t.population,
                distance=t.distance,
                is_oasis=t.is_oasis,
                is_abandoned=t.is_abandoned,
            )
            for t in tiles
        ]

        return ScanResponse(
            center_x=cx,
            center_y=cy,
            center_village=center_village.name,
            radius=body.radius,
            total=len(tile_responses),
            tiles=tile_responses,
        )

    except Exception as exc:
        logger.exception("Map scan failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Map scan failed: {exc}",
        ) from exc
