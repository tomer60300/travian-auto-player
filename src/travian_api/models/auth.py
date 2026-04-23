"""Authentication models for Travian API."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class AuthStatus(BaseModel):
    """Current authentication status."""

    is_authenticated: bool = Field(default=False, description="Whether user is authenticated")
    jwt_token: Optional[str] = Field(None, description="Current JWT token")
    username: Optional[str] = Field(None, description="Authenticated username")
    expires_at: Optional[int] = Field(None, description="Token expiration timestamp")

    @property
    def is_expired(self) -> bool:
        """Check if the token is expired."""
        if not self.expires_at:
            return False
        import time

        return int(time.time()) >= self.expires_at


class LoginRequest(BaseModel):
    """Login request data."""

    name: str = Field(..., description="Username")
    password: str = Field(..., description="Password")
    lowRes: int = Field(default=1, description="Low resolution flag")
    w: str = Field(default="1920:1080", description="Screen resolution")
    mobile_optimizations: bool = Field(default=False, description="Enable mobile optimizations")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Ensure username is not empty."""
        if not v.strip():
            raise ValueError("Username cannot be empty")
        return v.strip()

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        """Ensure password is not empty."""
        if not v.strip():
            raise ValueError("Password cannot be empty")
        return v


class LoginResponse(BaseModel):
    """Login response data."""

    success: bool = Field(default=True, description="Whether login was successful")
    jwt: Optional[str] = Field(None, description="JWT token if successful")
    error_message: Optional[str] = Field(None, description="Error message if failed")
    player_name: Optional[str] = Field(None, description="Player name if successful")
    tribe_id: Optional[int] = Field(None, description="Tribe ID if successful")
    village_id: Optional[int] = Field(None, description="Current village ID if successful")
    redirectTo: Optional[str] = Field(None, description="Redirect URL")

    @property
    def redirect_to(self) -> Optional[str]:
        """Get redirect URL."""
        return self.redirectTo


class Village(BaseModel):
    """A player village."""

    id: int = Field(..., description="Village ID")
    name: str = Field(default="", description="Village name")
    x: int = Field(default=0, description="X coordinate")
    y: int = Field(default=0, description="Y coordinate")
    is_main_village: bool = Field(default=False, description="Whether this is the main village")


class AuthState(BaseModel):
    """Authentication state with JWT and player info."""

    jwt: str = Field(..., description="JWT token")
    expires_at: int = Field(default=0, description="JWT expiration timestamp")
    player_name: str = Field(default="Unknown", description="Player name")
    tribe_id: int = Field(default=0, description="Tribe ID (1=Romans, 2=Teutons, 3=Gauls)")
    village_id: int = Field(default=0, description="Current/default village ID")
    villages: List[Village] = Field(default_factory=list, description="All player villages")


class JWTCache(BaseModel):
    """JWT token cache for persistent authentication."""

    token: str = Field(..., description="JWT token")
    username: str = Field(..., description="Username")
    server_url: str = Field(..., description="Server URL")
    cached_at: Optional[datetime] = Field(
        default_factory=lambda: datetime.now(), description="When token was cached"
    )

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        """Validate JWT format."""
        if not v.strip():
            raise ValueError("JWT token cannot be empty")
        return v.strip()

    def is_stale(self, max_age_hours: float = 24.0) -> bool:
        """Check if the cached token is stale."""
        if not self.cached_at:
            return True

        age_hours = (datetime.now() - self.cached_at).total_seconds() / 3600
        return age_hours >= max_age_hours
