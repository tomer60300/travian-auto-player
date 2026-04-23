"""Pydantic models for Travian API data structures."""

from .auth import AuthStatus, LoginRequest, LoginResponse
from .buildings import (
    BuildingInfo,
    ConstructionItem,
    ConstructionQueue,
    Resources,
    UpgradeRequest,
    UpgradeResult,
    VillageBuildings,
)
from .common import (
    Coordinates,
    PlayerInfo,
    VillageInfo,
)
from .farm_list import (
    FarmList,
    FarmListSendResult,
    FarmListSlot,
    LastRaid,
    MapTileInfo,
    RaidedResources,
    SlotTarget,
    SlotTroop,
)
from .military import (
    AttackRequest,
    AttackResult,
    RaidRequest,
    RaidResult,
    ScoutRequest,
    ScoutResult,
    TargetInfo,
    TroopComposition,
    TroopDispatch,
)
from .reports import (
    AdventureReport,
    BattleReport,
    Report,
    ReportList,
    ReportSummary,
    ScoutReport,
    TradeReport,
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
