"""Farm list models for Travian API."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


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
            ]
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

    @field_validator("distance", mode="before")
    @classmethod
    def _distance_default(cls, v: float | None) -> float:
        """Travian sends ``distance: null`` for un-raided slots; coerce to 0.0."""
        return 0.0 if v is None else v


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
    owner_village: OwnerVillage = Field(default_factory=OwnerVillage, alias="ownerVillage")
    default_troop: SlotTroop = Field(default_factory=SlotTroop, alias="defaultTroop")
    slots: List[FarmListSlot] = Field(default_factory=list)
    slots_amount: int = Field(default=0, alias="slotsAmount")

    model_config = {"populate_by_name": True}

    @field_validator("owner_village", mode="before")
    @classmethod
    def _owner_village_default(cls, v: object) -> object:
        """Travian sends ``ownerVillage: null`` for some lists; coerce to default."""
        return {} if v is None else v

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
    # Population shown as "V.Pop" in the UI. For player villages it's
    # the village's own pop. For OCCUPIED oases the post-enrichment phase
    # in scout_ws inherits the owning village's pop into this field, so
    # the same min/max-pop filter applies uniformly. UNOCCUPIED oases
    # and abandoned valleys stay at 0.
    population: int = 0
    # Owner's TOTAL population summed across all their villages.
    # 0 when the tile has no owner (unoccupied oasis, abandoned valley).
    # For player villages: filled with the player's profile total OR the
    # visible-village sum if profile fetch was skipped.
    # For occupied oases: same — the OWNER's total goes here, separate
    # from the per-village pop in `population`.
    owner_population: int = 0
    distance: float = 0.0
    is_oasis: bool = False
    is_abandoned: bool = False  # did=-1 with no uid
    is_capital: bool = False  # owner's capital village (extracted during enrichment)
    # Oasis bonus summary as a human-readable string, e.g. "25% Clay" or
    # "25% Iron, 25% Crop" or "50% Crop". Empty for non-oasis tiles or
    # when the bonus table can't be parsed from the tile-details popup.
    bonus: str = ""
    # Canonical breakdown — locale-stable resource_id → pct map. Keys are
    # one of "wood", "clay", "iron", "crop". Empty for non-oasis tiles
    # AND for oasis tiles whose bonus can't be classified (icon class
    # missing AND locale not in our synonyms list). Server-side filters
    # read this dict; the frontend renders ``bonus`` for display.
    bonus_breakdown: Dict[str, int] = Field(default_factory=dict)
    # Coords of the village that occupies this oasis (parsed from the
    # `karte.php?x=&y=` link in the tile-details popup). Only set on
    # occupied oases; used to copy the owner-village's population into
    # `population` so the V.Pop column and min/max-pop filters are
    # uniform across villages and occupied oases.
    oasis_owner_x: Optional[int] = None
    oasis_owner_y: Optional[int] = None
    # Aggregated oasis bonus for a VILLAGE — the sum of the bonuses of every
    # oasis this village occupies (from its profile `occupiedOases`), keyed by
    # canonical resource id. Populated only in the "villages by oasis bonus"
    # scan mode, on non-oasis tiles. Empty {} for villages with no occupied
    # oases or in any other mode. Can exceed 100% across multiple oases.
    village_oasis_breakdown: Dict[str, int] = Field(default_factory=dict)
    # Number of oases this village occupies (from the profile). 0 lets the
    # village-oasis-bonus filter reject a village for free, without any
    # tile-details fetch.
    village_oasis_count: int = 0
