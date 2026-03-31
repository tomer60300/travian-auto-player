"""Pydantic models for Travian API data structures."""

from .auth import LoginRequest, LoginResponse, AuthStatus
from .buildings import (
    BuildingInfo,
    VillageBuildings, 
    UpgradeRequest,
    UpgradeResult,
    ConstructionItem,
    ConstructionQueue,
    Resources,
)
from .military import (
    TroopDispatch,
    TroopComposition,
    ScoutRequest,
    ScoutResult,
    RaidRequest, 
    RaidResult,
    AttackRequest,
    AttackResult,
    TargetInfo,
)
from .reports import (
    Report,
    ReportList,
    ScoutReport,
    BattleReport,
    TradeReport,
    AdventureReport,
    ReportSummary,
)
from .common import (
    Coordinates,
    VillageInfo,
    PlayerInfo,
)
from .farm_list import (
    FarmList,
    FarmListSlot,
    FarmListSendResult,
    LastRaid,
    RaidedResources,
    SlotTarget,
    SlotTroop,
    MapTileInfo,
)

__all__ = [
    # Auth models
    "LoginRequest",
    "LoginResponse", 
    "AuthStatus",
    
    # Building models
    "BuildingInfo",
    "VillageBuildings",
    "UpgradeRequest",
    "UpgradeResult", 
    "ConstructionItem",
    "ConstructionQueue",
    "Resources",
    
    # Military models
    "TroopDispatch",
    "TroopComposition",
    "ScoutRequest",
    "ScoutResult",
    "RaidRequest",
    "RaidResult", 
    "AttackRequest",
    "AttackResult",
    "TargetInfo",
    
    # Report models
    "Report",
    "ReportList",
    "ScoutReport",
    "BattleReport",
    "TradeReport",
    "AdventureReport",
    "ReportSummary",
    
    # Common models
    "Coordinates",
    "VillageInfo",
    "PlayerInfo",

    # Farm list models
    "FarmList",
    "FarmListSlot",
    "FarmListSendResult",
    "LastRaid",
    "RaidedResources",
    "SlotTarget",
    "SlotTroop",
    "MapTileInfo",
]