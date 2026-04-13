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
    max_pop: int | None = Field(None, description="Max village population filter")
    min_pop: int | None = Field(None, description="Min village population filter")
    max_player_pop: int | None = Field(None, description="Max player population sum from visible villages in scan radius")
    show_oases: bool = Field(False, description="Include unoccupied oases in results")
    limit: int = Field(50, ge=1, le=500, description="Max results to return")
    exclude_alliance_ids: list[int] = Field(default_factory=list, description="Alliance IDs to exclude")
    exclude_alliance_names: list[str] = Field(default_factory=list, description="Alliance names/tags to exclude (case-insensitive)")
    exclude_player_names: list[str] = Field(default_factory=list, description="Player names to exclude")


class MapTileResponse(BaseModel):
    x: int
    y: int
    village_id: int = 0
    player_id: int | None = None
    alliance_id: int | None = None
    alliance_name: str = ""
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

        # Keep only relevant tiles: player villages + optionally oases
        own_village_ids = {v.id for v in session.auth_state.villages}
        relevant: list = []
        for t in tiles:
            if t.village_id <= 0:
                continue
            if t.village_id in own_village_ids:
                continue
            if t.player_id:
                relevant.append(t)
                continue
            if t.is_oasis and body.show_oases:
                relevant.append(t)
                continue
        tiles = relevant

        # Pre-enrichment: exclude alliances by ID
        if body.exclude_alliance_ids:
            excluded = set(body.exclude_alliance_ids)
            tiles = [t for t in tiles if not t.alliance_id or t.alliance_id not in excluded]

        # Enrich with population data
        if tiles:
            tiles = await svc.enrich_tiles(tiles)

        # Post-enrichment: exclude alliances by name
        logger.info(
            "Alliance filter: exclude_alliance_names=%s, exclude_alliance_ids=%s",
            body.exclude_alliance_names, body.exclude_alliance_ids,
        )
        if body.exclude_alliance_names:
            excluded_names = {n.lower() for n in body.exclude_alliance_names}
            before = len(tiles)
            tiles = [t for t in tiles if not t.alliance_name or t.alliance_name.lower() not in excluded_names]
            logger.info("Alliance name filter: %d -> %d (excluded %s)", before, len(tiles), excluded_names)

        # Exclude players by name
        exclude_player_ids: set[int] = set()
        if body.exclude_player_names:
            name_lower_set = {n.lower() for n in body.exclude_player_names}
            for t in tiles:
                if t.player_name and t.player_name.lower() in name_lower_set:
                    if t.player_id:
                        exclude_player_ids.add(t.player_id)

        # Compute real player total populations from profile pages when the
        # max_player_pop filter is active.  This is more accurate than summing
        # visible villages (which misses villages outside the scan radius).
        player_pops: dict[int, int] = {}
        if body.max_player_pop is not None:
            unique_pids = {t.player_id for t in tiles if t.player_id}
            if unique_pids:
                player_pops = await svc.fetch_player_populations(unique_pids)
                # Fall back to visible pop for any player whose profile fetch failed
                for t in tiles:
                    if t.player_id and not player_pops.get(t.player_id):
                        player_pops[t.player_id] = player_pops.get(t.player_id, 0) + t.population
                # Occupied oases inherit owner's total population
                for t in tiles:
                    if t.is_oasis and t.player_id and t.population == 0:
                        owner_pop = player_pops.get(t.player_id, 0)
                        if owner_pop > 0:
                            t.population = owner_pop

        # Apply village-level filters (population, oases, excluded players)
        tiles = svc.filter_targets(
            tiles,
            max_population=body.max_pop,
            min_population=body.min_pop,
            only_no_player=False,
            exclude_oases=not body.show_oases,
            exclude_player_ids=exclude_player_ids or None,
        )

        # Filter by max player total population (uses pre-computed sums)
        if body.max_player_pop is not None:
            tiles = [
                t for t in tiles
                if not t.player_id
                or player_pops.get(t.player_id, 0) <= body.max_player_pop
            ]

        # Apply limit
        tiles = tiles[: body.limit]

        tile_responses = [
            MapTileResponse(
                x=t.x,
                y=t.y,
                village_id=t.village_id,
                player_id=t.player_id,
                alliance_id=t.alliance_id,
                alliance_name=t.alliance_name,
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
