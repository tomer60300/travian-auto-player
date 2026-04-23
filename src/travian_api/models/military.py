"""Military models for Travian API."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class TroopSendResult(BaseModel):
    """Result of troop sending operation."""

    success: bool = Field(..., description="Whether troop sending was successful")
    target_x: int = Field(..., description="Target X coordinate")
    target_y: int = Field(..., description="Target Y coordinate")
    target_name: str = Field(default="", description="Target village/location name")
    troops_sent: Dict[str, int] = Field(
        default_factory=dict, description="Troops sent by type (t1-t10)"
    )
    travel_time: str = Field(default="", description="Travel time string")
    command_id: str = Field(default="", description="Command ID if available")
    raw_response: str = Field(default="", description="Raw HTML response for debugging")


class TargetInfo(BaseModel):
    """Information about a target location."""

    x: int = Field(..., ge=-400, le=400, description="X coordinate")
    y: int = Field(..., ge=-400, le=400, description="Y coordinate")
    village_id: int = Field(..., description="Village ID")
    village_name: str = Field(default="", description="Village name (empty for oasis/wilderness)")
    player_name: str = Field(default="", description="Player name (empty for unoccupied)")
    population: int = Field(default=0, ge=0, description="Village population")
    alliance: str = Field(default="", description="Alliance name")


class TravelInfo(BaseModel):
    """Travel time calculation information."""

    duration_seconds: int = Field(..., ge=0, description="Travel duration in seconds")
    duration_string: str = Field(..., description="Human-readable duration string")
    distance: float = Field(..., ge=0, description="Distance in game units")
    speed: float = Field(..., ge=0, description="Movement speed")

    @field_validator("duration_seconds")
    @classmethod
    def validate_duration_seconds(cls, v: int) -> int:
        """Ensure duration is non-negative."""
        if v < 0:
            raise ValueError("Duration cannot be negative")
        return v

    @field_validator("duration_string")
    @classmethod
    def validate_duration_string(cls, v: str) -> str:
        """Ensure duration string is not empty."""
        if not v.strip():
            raise ValueError("Duration string cannot be empty")
        return v.strip()

    @field_validator("distance", "speed")
    @classmethod
    def validate_positive_values(cls, v: float) -> float:
        """Ensure distance and speed are non-negative."""
        if v < 0:
            raise ValueError("Value cannot be negative")
        return v


class TroopComposition(BaseModel):
    """Troop composition for military actions."""

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
        return sum(
            [
                self.t1,
                self.t2,
                self.t3,
                self.t4,
                self.t5,
                self.t6,
                self.t7,
                self.t8,
                self.t9,
                self.t10,
                self.t11,
            ]
        )


class VillageTroops(BaseModel):
    """Troops in a village."""

    own_troops: TroopComposition = Field(..., description="Own troops in village")
    incoming_troops: TroopComposition = Field(..., description="Incoming reinforcements")
    outgoing_troops: TroopComposition = Field(..., description="Outgoing troops")


class TroopDispatch(BaseModel):
    """Troop dispatch information."""

    troops: TroopComposition = Field(..., description="Troops to send")
    target: TargetInfo = Field(..., description="Target information")
    mission_type: str = Field(..., description="Mission type (attack, raid, reinforce, scout)")
    travel_time: int = Field(..., ge=0, description="Travel time in seconds")


class ScoutRequest(BaseModel):
    """Scout mission request."""

    target: TargetInfo = Field(..., description="Target to scout")
    scouts: int = Field(..., gt=0, description="Number of scouts to send")


class ScoutResult(BaseModel):
    """Scout mission result."""

    success: bool = Field(..., description="Whether scout mission was successful")
    target: TargetInfo = Field(..., description="Scouted target")
    resources_found: Optional[Dict[str, int]] = Field(None, description="Resources discovered")
    troops_found: Optional[TroopComposition] = Field(None, description="Troops discovered")
    buildings_found: Optional[List[Dict[str, Any]]] = Field(
        None, description="Buildings discovered"
    )
    casualties: int = Field(default=0, ge=0, description="Scout casualties")


class RaidRequest(BaseModel):
    """Raid mission request."""

    target: TargetInfo = Field(..., description="Target to raid")
    troops: TroopComposition = Field(..., description="Troops to send")


class RaidResult(BaseModel):
    """Raid mission result."""

    success: bool = Field(..., description="Whether raid was successful")
    target: TargetInfo = Field(..., description="Raided target")
    resources_stolen: Optional[Dict[str, int]] = Field(None, description="Resources stolen")
    casualties: TroopComposition = Field(..., description="Troop casualties")
    travel_info: TravelInfo = Field(..., description="Travel information")


class AttackRequest(BaseModel):
    """Attack mission request."""

    target: TargetInfo = Field(..., description="Target to attack")
    troops: TroopComposition = Field(..., description="Troops to send")
    catapult_targets: Optional[List[int]] = Field(None, description="Catapult target building IDs")


class AttackResult(BaseModel):
    """Attack mission result."""

    success: bool = Field(..., description="Whether attack was successful")
    target: TargetInfo = Field(..., description="Attacked target")
    battle_won: bool = Field(..., description="Whether battle was won")
    resources_stolen: Optional[Dict[str, int]] = Field(None, description="Resources stolen")
    attacker_casualties: TroopComposition = Field(..., description="Attacker casualties")
    defender_casualties: TroopComposition = Field(..., description="Defender casualties")
    buildings_damaged: Optional[List[Dict[str, Any]]] = Field(None, description="Buildings damaged")
    travel_info: TravelInfo = Field(..., description="Travel information")
