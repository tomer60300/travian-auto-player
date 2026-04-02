"""Auto-Scout service — scan map, find villages, and send scouts automatically."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Callable, Dict, List, Optional, Set, Tuple

from ..clients.http_client import HttpClient
from ..models.farm_list import FarmListSlot, MapTileInfo

logger = logging.getLogger(__name__)


class AutoScoutService:
    """Scan the map for villages and send scouts based on filters."""

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
        self._status_cb: Optional[Callable[[str], None]] = None

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._status_cb = cb

    def _report(self, msg: str) -> None:
        if self._status_cb:
            self._status_cb(msg)
        logger.info(msg)

    # ── Map scanning ─────────────────────────────────────────────────

    async def scan_map(
        self,
        center_x: int,
        center_y: int,
        radius: int,
    ) -> List[MapTileInfo]:
        """
        Scan the map around (center_x, center_y) within radius.
        Returns basic tile info from the map/position endpoint.

        The API returns a 31x31 grid per call (zoomLevel=3).
        For larger radii, multiple calls are made in a grid pattern.
        """
        tiles: Dict[Tuple[int, int], dict] = {}
        step = 15  # half of the 31x31 grid

        # Calculate grid of center points needed to cover the radius
        scan_centers = []
        for cx in range(center_x - radius, center_x + radius + 1, step * 2):
            for cy in range(center_y - radius, center_y + radius + 1, step * 2):
                scan_centers.append((cx, cy))

        self._report(f"Scanning {len(scan_centers)} map region(s) around ({center_x},{center_y}) r={radius}")

        for sx, sy in scan_centers:
            resp = await self.http_client.post_json(
                "/api/v1/map/position",
                {
                    "data": {
                        "x": sx,
                        "y": sy,
                        "zoomLevel": 3,
                        "ignorePositions": [],
                    }
                },
            )
            for t in resp.get("tiles", []):
                pos = t.get("position", {})
                x, y = pos.get("x", 0), pos.get("y", 0)
                if (x, y) not in tiles:
                    tiles[(x, y)] = t

        # Filter to tiles within the actual radius and that have a village/oasis
        result: List[MapTileInfo] = []
        for (x, y), t in tiles.items():
            dist = math.sqrt((x - center_x) ** 2 + (y - center_y) ** 2)
            if dist > radius:
                continue

            did = t.get("did")
            if did is None:
                continue  # wilderness, no village

            uid = t.get("uid")
            aid = t.get("aid")
            title = t.get("title", "")

            # Parse village name from title like "{k.dt} VillageName"
            village_name = ""
            name_match = re.search(r"\{k\.dt\}\s*(.+)", title)
            if name_match:
                village_name = name_match.group(1).strip()

            is_oasis = "{k.fo}" in title or "{k.bt}" in title

            info = MapTileInfo(
                x=x,
                y=y,
                village_id=did if did > 0 else 0,
                player_id=uid if uid else None,
                alliance_id=aid if aid else None,
                village_name=village_name,
                distance=round(dist, 2),
                is_oasis=is_oasis,
                is_abandoned=did == -1 and uid is None,
            )
            result.append(info)

        self._report(f"Found {len(result)} tiles with villages/oases in radius")
        return result

    async def get_tile_details(self, x: int, y: int) -> MapTileInfo:
        """Get detailed info for a single tile via tile-details API."""
        resp = await self.http_client.post_json(
            "/api/v1/map/tile-details", {"x": x, "y": y}
        )
        html = resp.get("html", "")
        return self._parse_tile_details(x, y, html)

    async def enrich_tiles(
        self, tiles: List[MapTileInfo], concurrency: int = 5
    ) -> List[MapTileInfo]:
        """
        Enrich tiles with population/tribe/player from tile-details API.
        Uses limited concurrency to avoid flooding the server.
        """
        sem = asyncio.Semaphore(concurrency)
        enriched: List[MapTileInfo] = []

        async def _fetch(tile: MapTileInfo) -> MapTileInfo:
            async with sem:
                try:
                    detail = await self.get_tile_details(tile.x, tile.y)
                    # Merge — keep distance from scan, take details from tile-details
                    detail.distance = tile.distance
                    detail.is_oasis = tile.is_oasis
                    detail.is_abandoned = tile.is_abandoned
                    if not detail.village_name and tile.village_name:
                        detail.village_name = tile.village_name
                    return detail
                except Exception as e:
                    logger.warning(f"Failed to get details for ({tile.x},{tile.y}): {e}")
                    return tile

        tasks = [_fetch(t) for t in tiles]
        enriched = await asyncio.gather(*tasks)
        return list(enriched)

    def filter_targets(
        self,
        tiles: List[MapTileInfo],
        *,
        max_population: Optional[int] = None,
        min_population: Optional[int] = None,
        exclude_coords: Optional[Set[Tuple[int, int]]] = None,
        exclude_player_ids: Optional[Set[int]] = None,
        exclude_alliance_ids: Optional[Set[int]] = None,
        only_no_player: bool = False,
        exclude_oases: bool = True,
        max_distance: Optional[float] = None,
    ) -> List[MapTileInfo]:
        """Filter scanned tiles by conditions."""
        exclude_coords = exclude_coords or set()
        exclude_player_ids = exclude_player_ids or set()
        exclude_alliance_ids = exclude_alliance_ids or set()

        result = []
        for t in tiles:
            if (t.x, t.y) in exclude_coords:
                continue
            if t.player_id and t.player_id in exclude_player_ids:
                continue
            if t.alliance_id and t.alliance_id in exclude_alliance_ids:
                continue
            if only_no_player and t.player_id:
                continue
            if exclude_oases and t.is_oasis:
                continue
            if max_population is not None and t.population > max_population:
                continue
            if min_population is not None and t.population < min_population:
                continue
            if max_distance is not None and t.distance > max_distance:
                continue
            result.append(t)

        return sorted(result, key=lambda t: t.distance)

    # ── Scout sending ────────────────────────────────────────────────

    async def get_available_scout_count(
        self,
        tribe_id: int = 2,
        village_id: Optional[int] = None,
    ) -> int:
        """Query the rally point for how many scouts are currently available."""
        from ..constants import SCOUT_UNITS
        from ..services.military_service import MilitaryService
        from ..services.target_resolver import TargetResolver

        scout_unit = SCOUT_UNITS.get(tribe_id, "t4")
        resolver = TargetResolver(self.http_client)
        military = MilitaryService(self.http_client, resolver)
        troops = await military.get_available_troops(village_id)
        return troops.get(scout_unit, 0)

    async def send_scouts_to_targets(
        self,
        targets: List[MapTileInfo],
        scout_amount: int,
        scout_type: str = "resources",
        village_id: Optional[int] = None,
        tribe_id: int = 2,
        delay_between: float = 0.5,
        check_available: bool = False,
    ) -> List[Dict]:
        """
        Send scouts to a list of targets using the 2-step troop form.

        Args:
            targets: List of MapTileInfo targets
            scout_amount: Number of scouts per target
            scout_type: "resources" or "defenses"
            village_id: Source village ID
            tribe_id: Player tribe (1=Roman, 2=Teuton, 3=Gaul)
            delay_between: Seconds between sends to avoid rate limiting
            check_available: If True, query available scouts first and only
                             send to as many targets as scouts allow. If 0
                             scouts are available, skip entirely.
        """
        from ..constants import SCOUT_UNITS
        from ..services.military_service import MilitaryService
        from ..services.target_resolver import TargetResolver

        scout_unit = SCOUT_UNITS.get(tribe_id, "t4")
        resolver = TargetResolver(self.http_client)
        military = MilitaryService(self.http_client, resolver)

        # Check available scouts and cap targets if requested
        if check_available:
            available = await military.get_available_troops(village_id)
            scout_count = available.get(scout_unit, 0)
            self._report(f"Available scouts ({scout_unit}): {scout_count}")
            if scout_count == 0:
                self._report("No scouts available — skipping this round")
                return []
            max_targets = scout_count // scout_amount
            if max_targets < len(targets):
                self._report(
                    f"Scouts available for {max_targets}/{len(targets)} targets "
                    f"({scout_count} scouts, {scout_amount} per target)"
                )
                targets = targets[:max_targets]

        results = []
        for i, target in enumerate(targets):
            self._report(
                f"[{i + 1}/{len(targets)}] Scouting ({target.x},{target.y}) "
                f"{target.village_name or '?'} pop={target.population} dist={target.distance}"
            )
            try:
                result = await military.send_scouts(
                    x=target.x,
                    y=target.y,
                    amount=scout_amount,
                    scout_type=scout_type,
                    village_id=village_id,
                )
                status = "sent" if result.success else f"failed: {result.raw_response[:100]}"
                results.append({
                    "x": target.x,
                    "y": target.y,
                    "name": target.village_name,
                    "population": target.population,
                    "distance": target.distance,
                    "success": result.success,
                    "status": status,
                    "travel_time": result.travel_time,
                })
                if result.success:
                    self._report(f"  -> Scouts sent! Travel: {result.travel_time or '?'}")
                else:
                    self._report(f"  -> FAILED: {result.raw_response[:100]}")

            except Exception as e:
                self._report(f"  -> ERROR: {e}")
                results.append({
                    "x": target.x,
                    "y": target.y,
                    "name": target.village_name,
                    "success": False,
                    "status": f"error: {e}",
                })

            if i < len(targets) - 1:
                await asyncio.sleep(delay_between)

        sent = sum(1 for r in results if r["success"])
        self._report(f"Done: {sent}/{len(targets)} scouts sent successfully")
        return results

    # ── Parsing ──────────────────────────────────────────────────────

    def _parse_tile_details(self, x: int, y: int, html: str) -> MapTileInfo:
        """Parse the tile-details HTML for village info."""
        info = MapTileInfo(x=x, y=y)

        # Population
        pop_match = re.search(
            r"<th>\s*Population\s*</th>\s*<td>\s*(\d+)\s*</td>", html, re.DOTALL
        )
        if pop_match:
            info.population = int(pop_match.group(1))

        # Owner
        player_match = re.search(
            r'<th>\s*Owner\s*</th>\s*<td[^>]*>\s*<a\s+href="/profile/(\d+)"[^>]*>([^<]*)</a>',
            html,
            re.DOTALL,
        )
        if player_match:
            info.player_id = int(player_match.group(1))
            info.player_name = player_match.group(2).strip()

        # Tribe
        tribe_match = re.search(
            r"<th>\s*Tribe\s*</th>\s*<td>\s*(\w+)\s*</td>", html, re.DOTALL
        )
        if tribe_match:
            info.tribe = tribe_match.group(1)

        # Village name from h1
        name_match = re.search(r'<h1[^>]*>([^<]+)', html)
        if name_match:
            info.village_name = name_match.group(1).strip()

        # Village ID from links (e.g., villageId=69344)
        vid_match = re.search(r"villageId=(\d+)", html)
        if vid_match:
            info.village_id = int(vid_match.group(1))

        # Distance
        dist_match = re.search(
            r"<th>\s*Distance\s*</th>\s*<td>\s*([\d.]+)\s*fields?\s*</td>",
            html,
            re.DOTALL,
        )
        if dist_match:
            info.distance = float(dist_match.group(1))

        # Alliance
        alliance_match = re.search(
            r'<th>\s*Alliance\s*</th>\s*<td[^>]*>\s*<a\s+href="/alliance/(\d+)"[^>]*>([^<]*)</a>',
            html,
            re.DOTALL,
        )
        if alliance_match:
            aid = int(alliance_match.group(1))
            if aid > 0:
                info.alliance_id = aid

        # Check if oasis
        if "oasis" in html.lower() or 'class="oasis' in html:
            info.is_oasis = True

        return info
