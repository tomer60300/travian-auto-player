"""Building service for Travian API."""

from __future__ import annotations

from typing import List, Optional, Dict, Any

from ..clients.http_client import HttpClient
from ..exceptions import TravianError, BuildingNotFoundError
from ..models.buildings import Building, BuildingDetail, QueueItem, Resources, UpgradeResult
from ..parsers.html_parser import (
    parse_dorf1, 
    parse_dorf2, 
    parse_resources,
    parse_build_page,
    parse_construction_queue
)
from ..constants import BUILDING_NAMES


class BuildingService:
    """Service for managing buildings and resources."""
    
    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
    
    async def get_village_buildings(self, village_id: Optional[int] = None) -> List[Building]:
        """
        Get all buildings in the village.
        
        Args:
            village_id: Village ID (unused for now, uses current village)
            
        Returns:
            List of Building objects
            
        Raises:
            TravianError: If request fails
        """
        buildings = []
        
        try:
            # Get resource fields from dorf1.php
            dorf1_html = await self.http_client.get_html("/dorf1.php")
            resource_fields = parse_dorf1(dorf1_html)
            
            for field in resource_fields:
                buildings.append(Building(
                    slot_id=field['slot_id'],
                    gid=field['gid'],
                    name=field['name'],
                    level=field['level']
                ))
            
            # Get village buildings from dorf2.php
            dorf2_html = await self.http_client.get_html("/dorf2.php")
            village_buildings = parse_dorf2(dorf2_html)
            
            for building in village_buildings:
                buildings.append(Building(
                    slot_id=building['slot_id'],
                    gid=building['gid'],
                    name=building['name'],
                    level=building['level']
                ))
            
            return buildings
            
        except Exception as e:
            raise TravianError(f"Failed to get village buildings: {e}") from e
    
    async def get_building_detail(self, slot_id: int, village_id: Optional[int] = None) -> BuildingDetail:
        """
        Get detailed information about a specific building.
        
        Args:
            slot_id: Building slot ID (1-40)
            village_id: Village ID (switches village if set)
            
        Returns:
            BuildingDetail object
            
        Raises:
            TravianError: If request fails
        """
        try:
            url = f"/build.php?id={slot_id}"
            if village_id:
                url = f"/build.php?newdid={village_id}&id={slot_id}"
            build_html = await self.http_client.get_html(url)
            return parse_build_page(build_html, slot_id=slot_id)
            
        except Exception as e:
            raise TravianError(f"Failed to get building detail for slot {slot_id}: {e}") from e
    
    async def find_building(self, name_or_gid: str | int) -> Optional[Building]:
        """
        Find a building by name or GID.
        
        Args:
            name_or_gid: Building name (partial match) or GID number
            
        Returns:
            First matching Building or None
        """
        buildings = await self.get_village_buildings()
        
        if isinstance(name_or_gid, int):
            # Search by GID
            for building in buildings:
                if building.gid == name_or_gid:
                    return building
        else:
            # Search by name (case-insensitive partial match)
            search_term = str(name_or_gid).lower()
            for building in buildings:
                if search_term in building.name.lower():
                    return building
        
        return None
    
    async def upgrade_building(self, slot_id: int, allow_gold: bool = False) -> UpgradeResult:
        """
        Upgrade a building.
        
        SAFETY: Will refuse to upgrade if construction queue is occupied
        unless allow_gold=True (master builder costs 1 gold per extra slot).
        
        Args:
            slot_id: Building slot ID to upgrade
            allow_gold: If True, allow spending gold on master builder.
                        Default False — NEVER spend gold without explicit consent.
            
        Returns:
            UpgradeResult object
            
        Raises:
            TravianError: If upgrade fails or would cost gold
        """
        try:
            # SAFETY CHECK: Check construction queue BEFORE upgrading
            # If queue is occupied and allow_gold is False, REFUSE.
            queue = await self.get_construction_queue()
            if queue and not allow_gold:
                queue_names = ", ".join(f"{q.name} Lv{q.level}" for q in queue)
                return UpgradeResult(
                    success=False,
                    village_id=0,
                    building_id=slot_id,
                    building_name="Unknown",
                    old_level=0,
                    new_level=0,
                    construction_time="",
                    reward_used=False,
                    raw_response=f"BLOCKED: Construction queue already has [{queue_names}]. "
                                 f"Upgrading now would cost gold (master builder). "
                                 f"Use allow_gold=True to override.",
                )
            
            # Get building details to extract checksum and upgrade URL
            building_detail = await self.get_building_detail(slot_id)
            
            if not building_detail.checksum:
                raise TravianError(f"No upgrade available for building in slot {slot_id}")
            
            # Construct upgrade URL — always use &buildmaster
            # On Travian Plus accounts, the only button is "Construct with master builder"
            # which uses &buildmaster. When the queue is empty, this is FREE (0 gold).
            # When queue has an item, it uses the 2nd slot (costs 1 gold).
            upgrade_url = building_detail.upgrade_url
            if not upgrade_url:
                dorf_page = "dorf1" if slot_id <= 18 else "dorf2"
                upgrade_url = f"/{dorf_page}.php?id={slot_id}&gid={building_detail.gid}&action=build&checksum={building_detail.checksum}&buildmaster"
            elif '&buildmaster' not in upgrade_url:
                upgrade_url += '&buildmaster'
            
            # Perform upgrade by GET request to the URL
            response_html = await self.http_client.get_html(upgrade_url, skip_reauth=True)
            
            # Success detection: after upgrade, server redirects to dorf1/dorf2 page
            # which shows the construction queue with the new item.
            # Check for the building in the queue OR check that no error dialog appeared.
            import re
            has_queue_item = bool(re.search(r'showCancelBuildingDialog', response_html))
            has_error_dialog = bool(re.search(r'class="errorMessage"', response_html))
            # Also check for specific error messages
            not_enough = 'notEnough' in response_html or 'not_enough' in response_html
            
            success = has_queue_item and not has_error_dialog and not not_enough
            
            return UpgradeResult(
                success=success,
                village_id=0,
                building_id=slot_id,
                building_name=building_detail.name,
                old_level=building_detail.level,
                new_level=building_detail.level + 1 if success else building_detail.level,
                construction_time=building_detail.construction_time,
                reward_used=False,
                raw_response="" if success else response_html[:500],
            )
            
        except Exception as e:
            return UpgradeResult(
                success=False,
                village_id=0,
                building_id=slot_id,
                building_name="Unknown",
                old_level=0,
                new_level=0,
                construction_time="",
                reward_used=False,
                raw_response=str(e)
            )
    
    async def get_construction_queue(self) -> List[QueueItem]:
        """
        Get current construction queue.
        
        Returns:
            List of QueueItem objects
            
        Raises:
            TravianError: If request fails
        """
        try:
            # Construction queue is typically shown on dorf1 or dorf2
            # Try dorf2 first as it usually has the buildingList
            dorf2_html = await self.http_client.get_html("/dorf2.php")
            queue = parse_construction_queue(dorf2_html)
            
            if not queue:
                # Try dorf1 as fallback
                dorf1_html = await self.http_client.get_html("/dorf1.php")
                queue = parse_construction_queue(dorf1_html)
            
            return queue
            
        except Exception as e:
            raise TravianError(f"Failed to get construction queue: {e}") from e
    
    async def use_construction_reward(self) -> bool:
        """
        Use video reward to speed up construction.
        
        Returns:
            True if successful, False otherwise
            
        Note:
            This is a TODO - video reward flow needs implementation
        """
        # TODO: Implement video reward flow
        # POST /api/v1/videofeature/open/buildingUpgrade -> {vrid, videoIframeUrl}
        # POST /api/v1/videofeature/start -> {vrid}  
        # POST /api/v1/videofeature/ends -> {vrid, hash}
        
        raise NotImplementedError("Video reward flow not yet implemented")
    
    async def get_resources(self) -> Resources:
        """
        Get current village resources.
        
        Returns:
            Resources object
            
        Raises:
            TravianError: If request fails
        """
        try:
            # Resources are typically shown on dorf1 and dorf2
            dorf1_html = await self.http_client.get_html("/dorf1.php")
            resources = parse_resources(dorf1_html)
            
            # If not found, try dorf2
            if not any([resources.lumber, resources.clay, resources.iron, resources.crop]):
                dorf2_html = await self.http_client.get_html("/dorf2.php")
                resources = parse_resources(dorf2_html)
            
            return resources
            
        except Exception as e:
            raise TravianError(f"Failed to get resources: {e}") from e