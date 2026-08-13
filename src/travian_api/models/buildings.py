"""Building models for Travian API."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class Building(BaseModel):
    """Basic building information."""

    slot_id: int = Field(..., ge=1, le=40, description="Building slot ID (1-40)")
    gid: int = Field(..., ge=0, description="Building type ID (GID)")
    name: str = Field(..., description="Building name")
    level: int = Field(..., ge=0, le=100, description="Building level")

    @field_validator("slot_id")
    @classmethod
    def validate_slot_id(cls, v: int) -> int:
        """Ensure slot ID is within valid range."""
        if not (1 <= v <= 40):
            raise ValueError("Building slot ID must be between 1 and 40")
        return v

    @field_validator("level")
    @classmethod
    def validate_level(cls, v: int) -> int:
        """Ensure level is non-negative."""
        if v < 0:
            raise ValueError("Building level cannot be negative")
        return v


class BuildingDetail(BaseModel):
    """Detailed building information from build page."""

    slot_id: int = Field(..., ge=1, le=40, description="Building slot ID")
    gid: int = Field(..., ge=0, description="Building type ID (GID)")
    name: str = Field(..., description="Building name")
    level: int = Field(..., ge=0, description="Current building level")
    costs: Dict[str, int] = Field(
        default_factory=dict, description="Upgrade costs by resource type"
    )
    construction_time: str = Field(default="", description="Construction time string")
    checksum: str = Field(default="", description="Upgrade checksum")
    upgrade_url: str = Field(default="", description="Full upgrade URL")

    @field_validator("costs")
    @classmethod
    def validate_costs(cls, v: Dict[str, int]) -> Dict[str, int]:
        """Ensure all costs are non-negative."""
        for resource, cost in v.items():
            if cost < 0:
                raise ValueError(f"Cost for {resource} cannot be negative")
        return v


class Resources(BaseModel):
    """Village resource information."""

    lumber: int = Field(default=0, ge=0, description="Current lumber amount")
    clay: int = Field(default=0, ge=0, description="Current clay amount")
    iron: int = Field(default=0, ge=0, description="Current iron amount")
    crop: int = Field(default=0, ge=0, description="Current crop amount")
    free_crop: int = Field(
        default=0,
        description=(
            "Raw production.l5 from Travian. NOT net crop and NOT a starvation "
            "signal -- it can be positive while the village drains. Use "
            "crop_per_hour. See docs/20-resource-production.md."
        ),
    )
    max_lumber: int = Field(default=0, ge=0, description="Maximum lumber storage")
    max_clay: int = Field(default=0, ge=0, description="Maximum clay storage")
    max_iron: int = Field(default=0, ge=0, description="Maximum iron storage")
    max_crop: int = Field(default=0, ge=0, description="Maximum crop storage")
    lumber_per_hour: int = Field(default=0, description="Lumber production per hour")
    clay_per_hour: int = Field(default=0, description="Clay production per hour")
    iron_per_hour: int = Field(default=0, description="Iron production per hour")
    crop_per_hour: int = Field(
        default=0,
        description=(
            "NET crop per hour (production.l4): production minus population, "
            "constructions and every troop feeding from this village. Negative "
            "means the granary is draining and troops will starve."
        ),
    )


class QueueItem(BaseModel):
    """Construction queue item."""

    event_id: str = Field(..., description="Event ID for cancellation")
    building_name: str = Field(..., description="Name of building being constructed")
    target_level: int = Field(..., ge=1, description="Target level after construction")
    remaining_seconds: int = Field(..., ge=0, description="Remaining construction time in seconds")

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        """Ensure event ID is not empty."""
        if not v.strip():
            raise ValueError("Event ID cannot be empty")
        return v.strip()

    @field_validator("building_name")
    @classmethod
    def validate_building_name(cls, v: str) -> str:
        """Ensure building name is not empty."""
        if not v.strip():
            raise ValueError("Building name cannot be empty")
        return v.strip()


class UpgradeResult(BaseModel):
    """Result of building upgrade operation."""

    success: bool = Field(..., description="Whether upgrade was successful")
    village_id: int = Field(..., description="Village ID")
    building_id: int = Field(..., description="Building slot ID")
    building_name: str = Field(..., description="Building name")
    old_level: int = Field(..., ge=0, description="Level before upgrade")
    new_level: int = Field(..., ge=0, description="Level after upgrade")
    construction_time: str = Field(..., description="Construction time string")
    reward_used: bool = Field(..., description="Whether video reward was used")
    raw_response: str = Field(..., description="Raw HTML response for debugging")

    @field_validator("building_name")
    @classmethod
    def validate_building_name(cls, v: str) -> str:
        """Ensure building name is not empty."""
        if not v.strip():
            raise ValueError("Building name cannot be empty")
        return v.strip()

    @field_validator("new_level")
    @classmethod
    def validate_new_level(cls, v: int, info) -> int:
        """Ensure new level is valid relative to old level."""
        if hasattr(info, "data") and "old_level" in info.data:
            old_level = info.data["old_level"]
            success = info.data.get("success", False)

            if success and v <= old_level:
                raise ValueError("New level must be greater than old level for successful upgrades")

        return v


class UpgradeRequest(BaseModel):
    """Request to upgrade a building."""

    slot: int = Field(..., description="Building slot to upgrade")
    building_type: Optional[str] = Field(None, description="Expected building type")
    use_gold: bool = Field(default=False, description="Whether to use gold to speed up")

    @field_validator("slot")
    @classmethod
    def validate_slot(cls, v: int) -> int:
        """Ensure slot is within valid range."""
        if not (1 <= v <= 40):
            raise ValueError("Building slot must be between 1 and 40")
        return v


class VillageBuildings(BaseModel):
    """Complete building information for a village."""

    buildings: Dict[int, Building] = Field(..., description="Buildings by slot number")
    resources: Resources = Field(..., description="Current resources")
    construction_queue: List[QueueItem] = Field(..., description="Construction queue")
    village_id: str = Field(..., description="Village ID")
    village_name: str = Field(..., description="Village name")

    @field_validator("buildings")
    @classmethod
    def validate_buildings(cls, v: Dict[int, Building]) -> Dict[int, Building]:
        """Ensure all building slots are valid."""
        for slot, building in v.items():
            if slot != building.slot_id:
                raise ValueError(
                    f"Building slot mismatch: key {slot} != building.slot_id {building.slot_id}"
                )
        return v

    def get_building(self, slot: int) -> Optional[Building]:
        """Get building at specific slot."""
        return self.buildings.get(slot)

    def get_resource_fields(self) -> List[Building]:
        """Get all resource field buildings (slots 1-18)."""
        return [building for slot, building in self.buildings.items() if 1 <= slot <= 18]

    def get_village_buildings(self) -> List[Building]:
        """Get all village buildings (slots 19-40)."""
        return [building for slot, building in self.buildings.items() if 19 <= slot <= 40]


# Aliases for backward compatibility
BuildingInfo = Building
ConstructionItem = QueueItem
ConstructionQueue = List[QueueItem]  # Queue is a list of items
