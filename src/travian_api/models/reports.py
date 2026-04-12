"""Report models for Travian API."""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field, field_validator


class ReportListItem(BaseModel):
    """Report list item from report list page."""
    
    report_id: str = Field(..., description="Report ID")
    icon_type: int = Field(..., description="Icon type number (iReportN)")
    report_type: str = Field(..., description="Report type (battle/scout/trade/etc)")
    subject: str = Field(..., description="Report subject/title")
    date_str: str = Field(..., description="Date string as shown in list")
    is_read: bool = Field(..., description="Whether report has been read")
    
    @field_validator("report_id")
    @classmethod
    def validate_report_id(cls, v: str) -> str:
        """Ensure report ID is not empty."""
        if not v.strip():
            raise ValueError("Report ID cannot be empty")
        return v.strip()
    
    @field_validator("subject")
    @classmethod
    def validate_subject(cls, v: str) -> str:
        """Clean subject text."""
        return v.strip() if v else ""


class Report(BaseModel):
    """Full report information."""
    
    id: str = Field(..., description="Report ID")
    kind: str = Field(..., description="Report kind (battle/scout/trade/etc)")
    created_at: datetime = Field(..., description="Report creation time")
    title: str = Field(..., description="Report title")
    attacker: Optional[Dict[str, Any]] = Field(None, description="Attacker information")
    defender: Optional[Dict[str, Any]] = Field(None, description="Defender information")
    source_village: Optional[Dict[str, Any]] = Field(None, description="Source village info")
    target_village: Optional[Dict[str, Any]] = Field(None, description="Target village info")
    coordinates: Dict[str, int] = Field(default_factory=dict, description="Target coordinates")
    units: Dict[str, Any] = Field(default_factory=dict, description="Unit information")
    battle_result: Optional[str] = Field(None, description="Battle result (victory/defeat)")
    haul: Dict[str, int] = Field(default_factory=dict, description="Resources stolen/gained")
    losses: Dict[str, int] = Field(default_factory=dict, description="Unit losses")
    scouting_details: Optional[Dict[str, Any]] = Field(None, description="Scout report details")
    is_read: bool = Field(default=True, description="Whether report has been read")
    raw_html: str = Field(default="", description="Raw HTML content")
    
    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        """Ensure report ID is not empty."""
        if not v.strip():
            raise ValueError("Report ID cannot be empty")
        return v.strip()
    
    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        """Clean title text."""
        return v.strip() if v else ""


class BattleReportData(BaseModel):
    """Parsed battle report data."""

    attacker: Dict[str, Any] = Field(default_factory=dict, description="Attacker info")
    defender: Dict[str, Any] = Field(default_factory=dict, description="Defender info")
    attacker_troops: Dict[str, int] = Field(default_factory=dict, description="Attacker troops")
    defender_troops: Dict[str, int] = Field(default_factory=dict, description="Defender troops")
    battle_result: str = Field(default="unknown", description="Battle outcome")
    bounty: Dict[str, int] = Field(default_factory=dict, description="Resources stolen")
    attacker_losses: Dict[str, int] = Field(default_factory=dict, description="Attacker losses")
    defender_losses: Dict[str, int] = Field(default_factory=dict, description="Defender losses")
    carry_used: int = Field(default=0, description="Resources actually carried")
    carry_max: int = Field(default=0, description="Total carry capacity of surviving troops")
    carry_full: bool = Field(default=False, description="True if troops were fully loaded")
    attacker_combat_strength: int = Field(default=0, description="Attacker combat strength")
    defender_combat_strength: int = Field(default=0, description="Defender combat strength")

    @field_validator("battle_result")
    @classmethod
    def validate_battle_result(cls, v: str) -> str:
        """Validate battle result."""
        valid_results = ["victory", "defeat", "draw", "unknown"]
        v_lower = v.lower()
        if v_lower not in valid_results:
            return "unknown"
        return v_lower


class ScoutReportData(BaseModel):
    """Parsed scout report data."""
    
    target: Dict[str, Any] = Field(default_factory=dict, description="Target info")
    resources: Dict[str, int] = Field(default_factory=dict, description="Target resources")
    stealable_resources: Dict[str, int] = Field(default_factory=dict, description="Stealable resources")
    troops: Dict[str, int] = Field(default_factory=dict, description="Target troops (if visible)")
    buildings: List[Dict[str, Any]] = Field(default_factory=list, description="Target buildings")
    
    @field_validator("resources", "stealable_resources")
    @classmethod
    def validate_resources(cls, v: Dict[str, int]) -> Dict[str, int]:
        """Ensure resource values are non-negative."""
        return {k: max(0, val) for k, val in v.items()}
    
    @field_validator("troops")
    @classmethod
    def validate_troops(cls, v: Dict[str, int]) -> Dict[str, int]:
        """Ensure troop counts are non-negative."""
        return {k: max(0, val) for k, val in v.items()}


class ReportSummary(BaseModel):
    """Summary of reports."""
    
    total_reports: int = Field(default=0, ge=0, description="Total number of reports")
    unread_reports: int = Field(default=0, ge=0, description="Number of unread reports")
    scout_reports: int = Field(default=0, ge=0, description="Number of scout reports")
    battle_reports: int = Field(default=0, ge=0, description="Number of battle reports")
    trade_reports: int = Field(default=0, ge=0, description="Number of trade reports")
    other_reports: int = Field(default=0, ge=0, description="Number of other reports")


class ReportList(BaseModel):
    """List of reports with metadata."""
    
    reports: List[Report] = Field(default_factory=list, description="List of reports")
    summary: Optional[ReportSummary] = Field(None, description="Report summary")
    page: int = Field(default=1, ge=1, description="Current page number")
    has_more: bool = Field(default=False, description="Whether more reports are available")
    
    
class ScoutReport(Report):
    """Scout report details."""
    
    target_village: str = Field(..., description="Target village name")
    target_coordinates: Optional[str] = Field(None, description="Target coordinates")
    resources_found: Dict[str, int] = Field(default_factory=dict, description="Resources found")
    stealable_resources: Dict[str, int] = Field(default_factory=dict, description="Stealable resources")
    troops_found: Dict[str, int] = Field(default_factory=dict, description="Troops found")
    buildings_found: List[Dict[str, Any]] = Field(default_factory=list, description="Buildings found")
    

class BattleReport(Report):
    """Battle report details."""
    
    attacker: str = Field(..., description="Attacker name")
    defender: str = Field(..., description="Defender name") 
    attacker_village: str = Field(..., description="Attacker village")
    defender_village: str = Field(..., description="Defender village")
    result: str = Field(..., description="Battle result")
    resources_stolen: Dict[str, int] = Field(default_factory=dict, description="Resources stolen")
    attacker_losses: Dict[str, int] = Field(default_factory=dict, description="Attacker losses")
    defender_losses: Dict[str, int] = Field(default_factory=dict, description="Defender losses")
    

class TradeReport(Report):
    """Trade report details."""
    
    trader: str = Field(..., description="Trader name")
    source_village: str = Field(..., description="Source village") 
    target_village: str = Field(..., description="Target village")
    resources_sent: Dict[str, int] = Field(default_factory=dict, description="Resources sent")
    resources_received: Dict[str, int] = Field(default_factory=dict, description="Resources received")
    

class AdventureReport(Report):
    """Adventure report details."""
    
    adventure_type: str = Field(..., description="Adventure type")
    result: str = Field(..., description="Adventure result")
    rewards: Dict[str, Any] = Field(default_factory=dict, description="Adventure rewards")
    experience_gained: int = Field(default=0, ge=0, description="Experience gained")