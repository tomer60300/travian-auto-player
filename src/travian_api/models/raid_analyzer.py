"""Pydantic models for the Raid Analyzer feature."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field


class TargetVillageState(BaseModel):
    """Reconstructed state of a target village from report data."""

    # Identity
    village_name: str = ""
    player_name: str = ""
    x: int = 0
    y: int = 0
    village_id: int = 0

    # Resources
    estimated_raidable: int = 0
    raidable_confidence: str = "none"  # scouted|raided|depleted|stale|none

    # Defenders
    defenders: Dict[str, int] = Field(default_factory=dict)
    defender_source: str = "none"
    defender_timestamp: Optional[datetime] = None

    # Infrastructure
    trap_capacity: int = 0
    wall_level: int = 0
    wall_tribe: str = ""

    # Timing
    last_scout_time: Optional[datetime] = None
    last_raid_time: Optional[datetime] = None
    last_raid_bounty: int = 0
    last_report_time: Optional[datetime] = None
    last_report_id: str = ""

    # Metadata
    alliance_tag: str = ""
    village_population: int = 0
    player_population: int = 0
    distance: float = 0.0
    report_count: int = 0


class RaidRecommendation(BaseModel):
    """Scoring result for a single target."""

    n_send: int = 0
    unit_type: str = "CLUB"  # CLUB or AXE
    send_label: str = ""
    profit: float = 0.0
    score: float = 0.0
    mode: str = "RAID"
    round_trip_minutes: int = 0
    est_loot: int = 0


class AnalysisResult(BaseModel):
    """Complete analysis output."""

    targets: List[Tuple[TargetVillageState, RaidRecommendation]] = Field(default_factory=list)

    # Pipeline stats
    generated_at: datetime = Field(default_factory=datetime.now)
    analysis_duration_seconds: float = 0.0
    source_village_id: int = 0
    source_village_name: str = ""
    source_x: int = 0
    source_y: int = 0

    # Report pipeline stats
    total_reports_listed: int = 0
    reports_fetched_ok: int = 0
    reports_fetched_fail: int = 0
    reports_skipped_type: int = 0
    failed_report_ids: List[str] = Field(default_factory=list)
    pages_fetched: int = 0
    pages_failed: int = 0
    last_report_id: str = ""
    last_report_time: Optional[datetime] = None

    # Filter settings
    min_resources: int = 0
    max_report_age_hours: int = 0
    radius: Optional[float] = None
    excluded_alliances: List[str] = Field(default_factory=list)
    excluded_players: List[str] = Field(default_factory=list)

    # Skip counters
    skipped_needs_scout: int = 0
    skipped_low_resources: int = 0
    skipped_out_of_range: int = 0
    skipped_alliance: int = 0
    skipped_player: int = 0

    # Warnings
    warnings: List[str] = Field(default_factory=list)


class AnalyzerSettings(BaseModel):
    """Input settings for the analyzer."""

    village_id: Optional[int] = None
    min_resources: int = 200
    max_report_age_hours: int = 24
    max_pages: int = 20
    exclude_alliances: List[str] = Field(default_factory=list)
    exclude_players: List[str] = Field(default_factory=list)
    smithy_level: int = 0
    hero_offense: int = 0
    hero_strength: int = 0
    radius: Optional[float] = None
    include_alliance_reports: bool = False
    output_json: bool = False
