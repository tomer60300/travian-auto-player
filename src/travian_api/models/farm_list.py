"""Farm list models for Travian API."""

from __future__ import annotations

from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class RaidedResources(BaseModel):
    """Resources obtained from a raid."""
    lumber: int = 0
    clay: int = 0
    iron: int = 0
    crop: int = 0

    @property
    def total(self) -> int:
        return self.lumber + self.clay + self.iron + self.crop


class LastRaid(BaseModel):
    """Last raid information for a farm list slot."""
    report_object_id: Optional[str] = Field(default="", alias="reportObjectId")
    auth_key: Optional[str] = Field(default="", alias="authKey")
    time: Optional[int] = None
    raided_resources: RaidedResources = Field(
        default_factory=RaidedResources, alias="raidedResources"
    )
    booty_max: int = Field(default=0, alias="bootyMax")
    icon: int = 0  # 1=green(no loss), 2=yellow(some loss), 3=red(all dead)

    model_config = {"populate_by_name": True}

    @property
    def icon_label(self) -> str:
        return {1: "no_loss", 2: "some_loss", 3: "all_dead"}.get(self.icon, "unknown")


class TotalBooty(BaseModel):
    """Cumulative raid statistics for a slot."""
    booty: int = 0
    raids: int = 0


class SlotTarget(BaseModel):
    """Target village info for a farm list slot."""
    id: int = 0
    map_id: int = Field(default=0, alias="mapId")
    x: int = 0
    y: int = 0
    name: str = ""
    type: int = 0  # 0=normal, 3=oasis
    population: int = 0

    model_config = {"populate_by_name": True}


class SlotTroop(BaseModel):
    """Troop composition for a farm list slot."""
    t1: int = 0
    t2: int = 0
    t3: int = 0
    t4: int = 0
    t5: int = 0
    t6: int = 0
    t7: int = 0
    t8: int = 0
    t9: int = 0
    t10: int = 0

    @property
    def total(self) -> int:
        return sum(
            [self.t1, self.t2, self.t3, self.t4, self.t5,
             self.t6, self.t7, self.t8, self.t9, self.t10]
        )

    def to_dict(self) -> Dict[str, int]:
        return {f"t{i}": getattr(self, f"t{i}") for i in range(1, 11)}


class FarmListSlot(BaseModel):
    """A single target/slot within a farm list."""
    id: int = 0
    target: SlotTarget = Field(default_factory=SlotTarget)
    troop: SlotTroop = Field(default_factory=SlotTroop)
    distance: float = 0.0
    is_active: bool = Field(default=True, alias="isActive")
    is_running: bool = Field(default=False, alias="isRunning")
    is_spying: bool = Field(default=False, alias="isSpying")
    running_attacks: int = Field(default=0, alias="runningAttacks")
    next_attack_at: Optional[int] = Field(default=None, alias="nextAttackAt")
    last_raid: Optional[LastRaid] = Field(default=None, alias="lastRaid")
    total_booty: TotalBooty = Field(default_factory=TotalBooty, alias="totalBooty")

    model_config = {"populate_by_name": True}


class OwnerVillageTroops(BaseModel):
    """Available troops in the owner village."""
    units: SlotTroop = Field(default_factory=SlotTroop)


class OwnerVillageOwnTroops(BaseModel):
    own_troops_at_town: OwnerVillageTroops = Field(
        default_factory=OwnerVillageTroops, alias="ownTroopsAtTown"
    )
    model_config = {"populate_by_name": True}


class OwnerVillage(BaseModel):
    """Source village info for a farm list."""
    id: int = 0
    troops: Optional[OwnerVillageOwnTroops] = None

    def get_available_troops(self) -> SlotTroop:
        if self.troops and self.troops.own_troops_at_town:
            return self.troops.own_troops_at_town.units
        return SlotTroop()


class FarmList(BaseModel):
    """A farm list with all its slots."""
    id: int = 0
    name: str = ""
    running_raids_amount: int = Field(default=0, alias="runningRaidsAmount")
    is_expanded: bool = Field(default=True, alias="isExpanded")
    sort_index: int = Field(default=0, alias="sortIndex")
    last_started_time: Optional[int] = Field(default=None, alias="lastStartedTime")
    sort_field: Optional[str] = Field(default=None, alias="sortField")
    sort_direction: Optional[str] = Field(default=None, alias="sortDirection")
    use_ship: bool = Field(default=False, alias="useShip")
    only_losses: bool = Field(default=False, alias="onlyLosses")
    owner_village: OwnerVillage = Field(
        default_factory=OwnerVillage, alias="ownerVillage"
    )
    default_troop: SlotTroop = Field(
        default_factory=SlotTroop, alias="defaultTroop"
    )
    slots: List[FarmListSlot] = Field(default_factory=list)
    slots_amount: int = Field(default=0, alias="slotsAmount")

    model_config = {"populate_by_name": True}

    @property
    def active_slots(self) -> List[FarmListSlot]:
        return [s for s in self.slots if s.is_active]


class FarmListSendTargetResult(BaseModel):
    """Result for a single target in a farm list send."""
    id: int = 0
    status: str = ""
    error: str = ""


class FarmListSendResult(BaseModel):
    """Result of sending a farm list."""
    targets: List[FarmListSendTargetResult] = Field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for t in self.targets if t.error == "")

    @property
    def fail_count(self) -> int:
        return sum(1 for t in self.targets if t.error != "")


class MapTileInfo(BaseModel):
    """Parsed info from a map tile for auto-scout scanning."""
    x: int = 0
    y: int = 0
    village_id: int = 0  # did
    player_id: Optional[int] = None  # uid
    alliance_id: Optional[int] = None  # aid
    alliance_name: str = ""
    village_name: str = ""
    player_name: str = ""
    tribe: str = ""
    population: int = 0
    distance: float = 0.0
    is_oasis: bool = False
    is_abandoned: bool = False  # did=-1 with no uid
