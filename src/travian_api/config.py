"""Configuration management for Travian API."""

import os
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""
    
    # Server configuration — all three are required for auth
    # base_url = the game server (e.g. https://ts1.x1.europe.travian.com)
    # Same credentials can be used on multiple servers, but each server is a separate game world
    base_url: str = Field(default="", description="Game server URL (required)")
    x_version: str = Field(default="389", description="Game client version (from gpack)")
    
    # Authentication — username + password + server = full auth identity
    username: str = Field(default="", description="Account email/username")
    password: str = Field(default="", description="Account password")
    
    # Debugging and logging
    debug: bool = Field(default=False)
    log_level: str = Field(default="INFO")
    
    # Network
    timeout: int = Field(default=30)
    
    # Cache settings
    jwt_cache_file: str = Field(default=".jwt_cache.json")
    jwt_cache_path: str = Field(default="")
    
    # Stealth / anti-bot settings
    stealth: bool = Field(default=True, description="Enable stealth mode (human-like behavior)")
    stealth_speed: float = Field(default=1.0, description="Speed multiplier: 0.5=fast, 1.0=normal, 2.0=cautious")
    stealth_min_gap: float = Field(default=1.5, description="Min seconds between requests")
    stealth_max_gap: float = Field(default=3.0, description="Max seconds between requests")
    stealth_navigate: bool = Field(default=True, description="Simulate page navigation before actions")
    stealth_burst_max: int = Field(default=20, description="Max requests per 60s window before cooldown")
    stealth_burst_cooldown: float = Field(default=15.0, description="Cooldown seconds when burst limit hit")
    stealth_noise_rate: float = Field(default=0.0, description="Noise injection probability (0.0-1.0, default disabled)")
    stealth_max_daily_hours: float = Field(default=10.0, description="Maximum daily active hours")
    stealth_max_continuous_hours: float = Field(default=4.0, description="Max hours before forced break")
    stealth_min_break_minutes: float = Field(default=30.0, description="Minimum break duration in minutes")
    stealth_timing_variance: float = Field(default=0.4, description="Delay variance factor (0=tight, 1=very spread)")
    
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_prefix": "TRAVIAN_",
        "case_sensitive": False,
        "extra": "ignore",
    }
        
    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, v: str) -> str:
        """Ensure base URL doesn't end with slash."""
        return v.rstrip("/")
        
    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is one of the standard levels."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid_levels:
            raise ValueError(f"Log level must be one of: {', '.join(valid_levels)}")
        return v.upper()
        
    @field_validator("jwt_cache_file")
    @classmethod
    def validate_jwt_cache_file(cls, v: str) -> str:
        """Convert to absolute path if relative."""
        path = Path(v)
        if not path.is_absolute():
            return str(Path.cwd() / path)
        return str(path)


def get_settings() -> Settings:
    """Get application settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()