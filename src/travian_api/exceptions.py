"""Custom exceptions for Travian API."""

from typing import Optional, Dict, Any


class TravianError(Exception):
    """Base exception for all Travian API errors."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class AuthError(TravianError):
    """Authentication-related errors."""
    
    def __init__(self, message: str = "Authentication failed", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class SessionExpiredError(AuthError):
    """Session has expired and needs re-authentication."""
    
    def __init__(self, message: str = "Session expired", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class BuildError(TravianError):
    """Building-related operation errors."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class InsufficientResourcesError(BuildError):
    """Not enough resources for the requested building action."""
    
    def __init__(self, message: str = "Insufficient resources", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class BuildingNotFoundError(BuildError):
    """Building slot not found or invalid."""
    
    def __init__(self, slot: int, message: Optional[str] = None) -> None:
        msg = message or f"Building not found at slot {slot}"
        super().__init__(msg, {"slot": slot})
        self.slot = slot


class MilitaryError(TravianError):
    """Military operation errors."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class InsufficientTroopsError(MilitaryError):
    """Not enough troops for the requested military action."""
    
    def __init__(self, message: str = "Insufficient troops", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class InvalidTargetError(MilitaryError):
    """Invalid target coordinates or village."""
    
    def __init__(self, x: int, y: int, message: Optional[str] = None) -> None:
        msg = message or f"Invalid target at coordinates ({x}, {y})"
        super().__init__(msg, {"x": x, "y": y})
        self.x = x
        self.y = y


class ReportError(TravianError):
    """Report parsing and processing errors."""
    
    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class ReportNotFoundError(ReportError):
    """Report with specified ID not found."""
    
    def __init__(self, report_id: str, message: Optional[str] = None) -> None:
        msg = message or f"Report not found: {report_id}"
        super().__init__(msg, {"report_id": report_id})
        self.report_id = report_id


class ParseError(TravianError):
    """HTML parsing errors."""
    
    def __init__(self, message: str, html_content: Optional[str] = None) -> None:
        super().__init__(message, {"html_length": len(html_content) if html_content else 0})
        self.html_content = html_content


class ChecksumError(TravianError):
    """Checksum extraction or validation errors."""
    
    def __init__(self, message: str = "Failed to extract checksum", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


class NetworkError(TravianError):
    """Network-related errors."""
    
    def __init__(self, message: str, status_code: Optional[int] = None, response_text: Optional[str] = None) -> None:
        details = {}
        if status_code:
            details["status_code"] = status_code
        if response_text:
            details["response_length"] = len(response_text)
        super().__init__(message, details)
        self.status_code = status_code
        self.response_text = response_text


class ActivityBudgetExhausted(TravianError):
    """Activity budget (daily hours / continuous session) is used up."""

    def __init__(self, message: str = "Activity budget exhausted", details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message, details)


# Alias for backward compatibility
TravianAPIError = TravianError