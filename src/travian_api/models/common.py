"""Common data models used across the Travian API."""

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Coordinates(BaseModel):
    """Map coordinates for a location."""

    x: int = Field(..., ge=-400, le=400, description="X coordinate (-400 to 400)")
    y: int = Field(..., ge=-400, le=400, description="Y coordinate (-400 to 400)")

    @field_validator("x", "y")
    @classmethod
    def validate_coordinates(cls, v: int) -> int:
        """Ensure coordinates are within valid game bounds."""
        if not -400 <= v <= 400:
            raise ValueError(f"Coordinate {v} is outside valid range (-400 to 400)")
        return v

    def __str__(self) -> str:
        return f"({self.x}, {self.y})"

    def distance_to(self, other: "Coordinates") -> float:
        """Calculate distance to another coordinate."""
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2) ** 0.5


class VillageInfo(BaseModel):
    """Information about a village."""

    id: str = Field(..., description="Village ID")
    name: str = Field(..., description="Village name")
    coordinates: Coordinates = Field(..., description="Village coordinates")
    population: Optional[int] = Field(None, description="Village population")
    distance: Optional[float] = Field(None, description="Distance from current village")

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        """Ensure village ID is not empty."""
        if not v.strip():
            raise ValueError("Village ID cannot be empty")
        return v.strip()

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Clean and validate village name."""
        if not v.strip():
            raise ValueError("Village name cannot be empty")
        return v.strip()


class PlayerInfo(BaseModel):
    """Information about a player."""

    name: str = Field(..., description="Player name")
    tribe_id: Optional[int] = Field(None, description="Tribe ID (1=Romans, 2=Teutons, 3=Gauls)")
    alliance: Optional[str] = Field(None, description="Alliance name")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Clean and validate player name."""
        if not v.strip():
            raise ValueError("Player name cannot be empty")
        return v.strip()

    @field_validator("tribe_id")
    @classmethod
    def validate_tribe_id(cls, v: Optional[int]) -> Optional[int]:
        """Validate tribe ID is one of the known values."""
        if v is not None and v not in [1, 2, 3]:
            raise ValueError("Tribe ID must be 1 (Romans), 2 (Teutons), or 3 (Gauls)")
        return v


class ResourceAmount(BaseModel):
    """Amount of resources."""

    wood: int = Field(default=0, ge=0, description="Wood amount")
    clay: int = Field(default=0, ge=0, description="Clay amount")
    iron: int = Field(default=0, ge=0, description="Iron amount")
    crop: int = Field(default=0, ge=0, description="Crop amount")

    def total(self) -> int:
        """Get total resource amount."""
        return self.wood + self.clay + self.iron + self.crop

    def __add__(self, other: "ResourceAmount") -> "ResourceAmount":
        """Add two resource amounts."""
        return ResourceAmount(
            wood=self.wood + other.wood,
            clay=self.clay + other.clay,
            iron=self.iron + other.iron,
            crop=self.crop + other.crop,
        )

    def __sub__(self, other: "ResourceAmount") -> "ResourceAmount":
        """Subtract two resource amounts."""
        return ResourceAmount(
            wood=max(0, self.wood - other.wood),
            clay=max(0, self.clay - other.clay),
            iron=max(0, self.iron - other.iron),
            crop=max(0, self.crop - other.crop),
        )

    def can_afford(self, cost: "ResourceAmount") -> bool:
        """Check if we have enough resources for the given cost."""
        return (
            self.wood >= cost.wood
            and self.clay >= cost.clay
            and self.iron >= cost.iron
            and self.crop >= cost.crop
        )


class TroopCount(BaseModel):
    """Troop counts for all unit types."""

    t1: int = Field(default=0, ge=0, description="Unit type 1 count")
    t2: int = Field(default=0, ge=0, description="Unit type 2 count")
    t3: int = Field(default=0, ge=0, description="Unit type 3 count")
    t4: int = Field(default=0, ge=0, description="Unit type 4 count")
    t5: int = Field(default=0, ge=0, description="Unit type 5 count")
    t6: int = Field(default=0, ge=0, description="Unit type 6 count")
    t7: int = Field(default=0, ge=0, description="Unit type 7 count")
    t8: int = Field(default=0, ge=0, description="Unit type 8 count")
    t9: int = Field(default=0, ge=0, description="Unit type 9 count")
    t10: int = Field(default=0, ge=0, description="Unit type 10 count")
    t11: int = Field(default=0, ge=0, description="Hero count")

    def total(self) -> int:
        """Get total troop count."""
        return (
            self.t1
            + self.t2
            + self.t3
            + self.t4
            + self.t5
            + self.t6
            + self.t7
            + self.t8
            + self.t9
            + self.t10
            + self.t11
        )

    def is_empty(self) -> bool:
        """Check if no troops are present."""
        return self.total() == 0
