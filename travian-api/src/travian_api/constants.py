"""Constants used throughout the Travian API."""

from enum import IntEnum
from typing import Dict, Set


# Building type IDs (from game data)
class BuildingType(IntEnum):
    """Building type identifiers."""
    WOODCUTTER = 1
    CLAY_PIT = 2
    IRON_MINE = 3
    CROPLAND = 4
    SAWMILL = 5
    BRICKYARD = 6
    IRON_FOUNDRY = 7
    GRAIN_MILL = 8
    BAKERY = 9
    WAREHOUSE = 10
    GRANARY = 11
    BLACKSMITH = 13
    ARMOURY = 14
    MAIN_BUILDING = 15
    RALLY_POINT = 16
    MARKETPLACE = 17
    EMBASSY = 18
    BARRACKS = 19
    STABLE = 20
    WORKSHOP = 21
    ACADEMY = 22
    CRANNY = 23
    TOWNHALL = 24
    RESIDENCE = 25
    PALACE = 26
    TREASURY = 27
    TRADE_OFFICE = 28
    GREAT_BARRACKS = 29
    GREAT_STABLE = 30
    CITY_WALL = 31  # Romans
    EARTH_WALL = 32  # Teutons  
    PALISADE = 33   # Gauls
    STONEMASON = 34
    BREWERY = 35
    TRAPPER = 36
    HERO_MANSION = 37
    GREAT_WAREHOUSE = 38
    GREAT_GRANARY = 39
    WONDER = 40
    HOSPITAL = 46


# Resource fields are slots 1-18, village buildings are 19-40
RESOURCE_FIELD_SLOTS = set(range(1, 19))  # 1-18
VILLAGE_BUILDING_SLOTS = set(range(19, 41))  # 19-40


# Building names for display
BUILDING_NAMES = {
    BuildingType.WOODCUTTER: "Woodcutter",
    BuildingType.CLAY_PIT: "Clay Pit", 
    BuildingType.IRON_MINE: "Iron Mine",
    BuildingType.CROPLAND: "Cropland",
    BuildingType.SAWMILL: "Sawmill",
    BuildingType.BRICKYARD: "Brickyard",
    BuildingType.IRON_FOUNDRY: "Iron Foundry",
    BuildingType.GRAIN_MILL: "Grain Mill",
    BuildingType.BAKERY: "Bakery",
    BuildingType.WAREHOUSE: "Warehouse",
    BuildingType.GRANARY: "Granary",
    BuildingType.BLACKSMITH: "Blacksmith",
    BuildingType.ARMOURY: "Armoury",
    BuildingType.MAIN_BUILDING: "Main Building",
    BuildingType.RALLY_POINT: "Rally Point",
    BuildingType.MARKETPLACE: "Marketplace",
    BuildingType.EMBASSY: "Embassy",
    BuildingType.BARRACKS: "Barracks",
    BuildingType.STABLE: "Stable",
    BuildingType.WORKSHOP: "Workshop",
    BuildingType.ACADEMY: "Academy",
    BuildingType.CRANNY: "Cranny",
    BuildingType.TOWNHALL: "Town Hall",
    BuildingType.RESIDENCE: "Residence",
    BuildingType.PALACE: "Palace",
    BuildingType.TREASURY: "Treasury",
    BuildingType.TRADE_OFFICE: "Trade Office",
    BuildingType.GREAT_BARRACKS: "Great Barracks",
    BuildingType.GREAT_STABLE: "Great Stable",
    BuildingType.CITY_WALL: "City Wall",
    BuildingType.EARTH_WALL: "Earth Wall",
    BuildingType.PALISADE: "Palisade",
    BuildingType.STONEMASON: "Stonemason's Lodge",
    BuildingType.BREWERY: "Brewery",
    BuildingType.TRAPPER: "Trapper",
    BuildingType.HERO_MANSION: "Hero's Mansion",
    BuildingType.GREAT_WAREHOUSE: "Great Warehouse",
    BuildingType.GREAT_GRANARY: "Great Granary",
    BuildingType.WONDER: "Wonder of the World",
    BuildingType.HOSPITAL: "Hospital",
}


# Event types for military actions
class EventType(IntEnum):
    """Military event type identifiers."""
    SCOUT = 2
    ATTACK = 3
    RAID = 4
    REINFORCE = 5


# Tribe identifiers
class TribeType(IntEnum):
    """Tribe type identifiers."""
    ROMANS = 1
    TEUTONS = 2
    GAULS = 3


# Troop type mappings per tribe
TROOP_MAPPINGS = {
    TribeType.ROMANS: {
        "t1": "Legionnaire",
        "t2": "Praetorian", 
        "t3": "Imperian",
        "t4": "Equites Legati",  # Scout unit
        "t5": "Equites Imperatoris",
        "t6": "Equites Caesaris",
        "t7": "Battering Ram",
        "t8": "Fire Catapult",
        "t9": "Senator",
        "t10": "Settler",
    },
    TribeType.TEUTONS: {
        "t1": "Clubswinger",
        "t2": "Spearman",
        "t3": "Axeman", 
        "t4": "Scout",  # Scout unit
        "t5": "Paladin",
        "t6": "Teutonic Knight",
        "t7": "Ram",
        "t8": "Catapult",
        "t9": "Chief",
        "t10": "Settler",
    },
    TribeType.GAULS: {
        "t1": "Phalanx",
        "t2": "Swordsman",
        "t3": "Pathfinder",  # Scout unit
        "t4": "Theutates Thunder",
        "t5": "Druidrider", 
        "t6": "Haeduan",
        "t7": "Ram",
        "t8": "Trebuchet",
        "t9": "Chieftain",
        "t10": "Settler",
    },
}


# Scout unit types per tribe
SCOUT_UNITS = {
    TribeType.ROMANS: "t4",    # Equites Legati
    TribeType.TEUTONS: "t4",   # Scout
    TribeType.GAULS: "t3",     # Pathfinder
}


# Resource types
class ResourceType(IntEnum):
    """Resource type identifiers."""
    WOOD = 1
    CLAY = 2  
    IRON = 3
    CROP = 4


RESOURCE_NAMES = {
    ResourceType.WOOD: "Wood",
    ResourceType.CLAY: "Clay",
    ResourceType.IRON: "Iron", 
    ResourceType.CROP: "Crop",
}


# Report types (from CSS classes)
REPORT_TYPES = {
    "iReport1": "scout",
    "iReport2": "trade", 
    "iReport3": "reinforce",
    "iReport4": "attack",
    "iReport5": "defend",
    "iReport6": "adventure",
    "iReport7": "misc",
}


# HTML parsing patterns
CHECKSUM_PATTERN = r'checksum=([a-f0-9]{6})'
RESOURCES_PATTERN = r'var resources = ({[^}]+});'
VILLAGE_ID_PATTERN = r'villageId["\']?\s*:\s*["\']?(\d+)'


# API endpoints
API_ENDPOINTS = {
    "auth_login": "/api/v1/auth/login",
    "auth_redirect": "/api/v1/auth",
    "validate_destination": "/api/v1/validate-destination",
    "autocomplete_village": "/api/v1/autocomplete/villagename", 
    "map_position": "/api/v1/map/position",
    "graphql": "/api/v1/graphql",
    "video_open": "/api/v1/videofeature/open/buildingUpgrade",
    "video_start": "/api/v1/videofeature/start",
    "video_end": "/api/v1/videofeature/ends",
}


# Page endpoints
PAGE_ENDPOINTS = {
    "dorf1": "/dorf1.php",  # Resource fields
    "dorf2": "/dorf2.php",  # Village buildings
    "build": "/build.php",
    "reports": "/report/all",
    "report": "/report",
}


# HTTP headers
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "empty", 
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


# Retry configuration
MAX_RETRIES = 3
RETRY_DELAY = 1.0  # seconds
BACKOFF_FACTOR = 2.0


# Tribe-specific scout unit slots (map slot index to unit type)  
TRIBE_SCOUT_SLOTS = {
    TribeType.ROMANS: {"t4": 4},     # Equites Legati in slot 4
    TribeType.TEUTONS: {"t4": 4},    # Scout in slot 4
    TribeType.GAULS: {"t3": 3},      # Pathfinder in slot 3
}


# Event types for calendar/scheduling
EVENT_TYPES = {
    "building_complete": "Building Complete",
    "troop_return": "Troops Return",
    "attack_arrival": "Attack Arrival", 
    "trade_arrival": "Trade Arrival",
    "celebration_complete": "Celebration Complete",
}