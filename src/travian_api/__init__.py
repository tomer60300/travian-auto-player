"""
Travian Legends API - Game automation library and CLI.

A comprehensive library for automating Travian Legends gameplay including:
- Authentication and session management
- Building management and upgrades
- Military operations (scouting, raids, attacks)
- Report parsing and analysis
- Target resolution

Example usage:
    from travian_api.config import Settings
    from travian_api.clients.http_client import HttpClient
    from travian_api.services.auth_service import AuthService

    async def main():
        settings = Settings()
        http_client = HttpClient(settings)
        auth_service = AuthService(http_client, settings)

        auth_state = await auth_service.login()
        print(f"Logged in as {auth_state.player_name}")
"""

__version__ = "0.1.0"
__author__ = "Travian API"

from .config import Settings
from .exceptions import (
    AuthError,
    BuildError,
    BuildingNotFoundError,
    ChecksumError,
    InsufficientResourcesError,
    InsufficientTroopsError,
    InvalidTargetError,
    MilitaryError,
    NetworkError,
    ParseError,
    ReportError,
    ReportNotFoundError,
    SessionExpiredError,
    TravianError,
)

# Create alias to match the original spec
TravianAPIError = TravianError
AuthenticationError = AuthError

# Re-export commonly used classes
__all__ = [
    "Settings",
    "__version__",
    # Exceptions (original names)
    "TravianError",
    "AuthError",
    "SessionExpiredError",
    "BuildError",
    "InsufficientResourcesError",
    "BuildingNotFoundError",
    "MilitaryError",
    "InsufficientTroopsError",
    "InvalidTargetError",
    "ReportError",
    "ReportNotFoundError",
    "ParseError",
    "ChecksumError",
    "NetworkError",
    # Aliases for backward compatibility
    "TravianAPIError",
    "AuthenticationError",
]
