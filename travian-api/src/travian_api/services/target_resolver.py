"""Target resolver service for finding villages and players."""

from __future__ import annotations

from typing import Optional

from ..clients.http_client import HttpClient
from ..exceptions import TravianAPIError, InvalidTargetError
from ..models.military import TargetInfo


class TargetResolver:
    """Service for resolving target coordinates and names."""
    
    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
    
    async def resolve_by_coords(self, x: int, y: int) -> TargetInfo:
        """
        Resolve target information by coordinates.
        
        Args:
            x: X coordinate
            y: Y coordinate
            
        Returns:
            TargetInfo object
            
        Raises:
            InvalidTargetError: If coordinates are invalid
            TravianAPIError: If request fails
        """
        try:
            # Use map/position API to get tile data
            response = await self.http_client.post_json(
                "/api/v1/map/position",
                {
                    "data": {
                        "x": x,
                        "y": y,
                        "zoomLevel": 1,
                        "ignorePositions": []
                    }
                }
            )
            
            # Look for our target tile in the response
            tiles = response.get("tiles", [])
            target_tile = None
            for tile in tiles:
                pos = tile.get("position", {})
                if pos.get("x") == x and pos.get("y") == y:
                    target_tile = tile
                    break
            
            if not target_tile:
                # Tile exists but may be empty/unoccupied — still valid target
                return TargetInfo(
                    x=x, y=y,
                    village_id=0,
                    village_name="",
                    player_name="",
                    population=0,
                    alliance=""
                )
            
            # Parse title field: "{k.dt} Village Name" or "{k.spieler} Player<br/>{k.einwohner} 123"
            import re
            title = target_tile.get("title", "")
            text = target_tile.get("text", "")
            
            # Clean title — extract village name after pattern like "{k.dt} "
            village_name = re.sub(r'\{[^}]+\}\s*', '', title).strip()
            
            # Extract player name from text
            player_match = re.search(r'\{k\.spieler\}\s*(.+?)(?:<br|$)', text)
            player_name = player_match.group(1).strip() if player_match else ""
            
            # Extract population
            pop_match = re.search(r'\{k\.einwohner\}\s*(\d+)', text)
            population = int(pop_match.group(1)) if pop_match else 0
            
            # Extract alliance — text between {k.allianz} and next <br or {k.
            alliance_match = re.search(r'\{k\.allianz\}\s*(.*?)(?:<br|$|\{k\.)', text)
            alliance = alliance_match.group(1).strip() if alliance_match else ""
            # Clean any remaining template tags
            alliance = re.sub(r'\{[^}]+\}', '', alliance).strip()
            
            return TargetInfo(
                x=x, y=y,
                village_id=target_tile.get("did", 0),
                village_name=village_name,
                player_name=player_name,
                population=population,
                alliance=alliance
            )
            
        except Exception as e:
            if isinstance(e, InvalidTargetError):
                raise
            raise TravianAPIError(f"Failed to resolve coordinates ({x}, {y}): {e}") from e
    
    async def resolve_by_name(self, name: str) -> TargetInfo:
        """
        Resolve target information by village or player name.
        
        Args:
            name: Village or player name
            
        Returns:
            TargetInfo object
            
        Raises:
            InvalidTargetError: If name is not found
            TravianAPIError: If request fails
        """
        try:
            # Try village name autocomplete first
            village_response = await self.http_client.post_json(
                "/api/v1/autocomplete/villagename",
                {"query": name}
            )
            
            villages = village_response.get("suggestions", [])
            if villages:
                # Use first matching village
                village = villages[0]
                return TargetInfo(
                    x=village.get("x", 0),
                    y=village.get("y", 0),
                    village_id=village.get("id", 0),
                    village_name=village.get("name", ""),
                    player_name=village.get("playerName", ""),
                    population=village.get("population", 0),
                    alliance=village.get("alliance", "")
                )
            
            # Try player name autocomplete
            player_response = await self.http_client.post_json(
                "/api/v1/autocomplete/playername", 
                {"query": name}
            )
            
            players = player_response.get("suggestions", [])
            if players:
                # Use first village of first matching player
                player = players[0]
                villages = player.get("villages", [])
                
                if villages:
                    village = villages[0]
                    return TargetInfo(
                        x=village.get("x", 0),
                        y=village.get("y", 0),
                        village_id=village.get("id", 0),
                        village_name=village.get("name", ""),
                        player_name=player.get("name", ""),
                        population=village.get("population", 0),
                        alliance=player.get("alliance", "")
                    )
            
            raise InvalidTargetError(f"No target found with name: {name}")
            
        except Exception as e:
            if isinstance(e, InvalidTargetError):
                raise
            raise TravianAPIError(f"Failed to resolve name '{name}': {e}") from e
    
    async def resolve_target(
        self, 
        x: Optional[int] = None, 
        y: Optional[int] = None, 
        name: Optional[str] = None
    ) -> TargetInfo:
        """
        Resolve target by coordinates or name.
        
        Args:
            x: X coordinate (if resolving by coords)
            y: Y coordinate (if resolving by coords)
            name: Village or player name (if resolving by name)
            
        Returns:
            TargetInfo object
            
        Raises:
            ValueError: If neither coords nor name provided
            InvalidTargetError: If target not found
            TravianAPIError: If request fails
        """
        if x is not None and y is not None:
            return await self.resolve_by_coords(x, y)
        elif name:
            return await self.resolve_by_name(name)
        else:
            raise ValueError("Must provide either coordinates (x, y) or name")