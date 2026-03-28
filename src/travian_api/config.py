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