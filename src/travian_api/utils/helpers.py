"""Helper utilities for Travian API."""

from __future__ import annotations

import re
import time
from typing import Dict, Optional


def parse_time_string(time_str: str) -> int:
    """
    Parse time string to seconds.

    Args:
        time_str: Time string like "01:23:45" or "2h 30m 15s"

    Returns:
        Total seconds as integer
    """
    if not time_str:
        return 0

    # Try HH:MM:SS format first
    hms_match = re.match(r"(\d+):(\d+):(\d+)", time_str.strip())
    if hms_match:
        hours = int(hms_match.group(1))
        minutes = int(hms_match.group(2))
        seconds = int(hms_match.group(3))
        return hours * 3600 + minutes * 60 + seconds

    # Try parsing "2h 30m 15s" format
    total_seconds = 0

    # Hours
    hours_match = re.search(r"(\d+)h", time_str)
    if hours_match:
        total_seconds += int(hours_match.group(1)) * 3600

    # Minutes
    minutes_match = re.search(r"(\d+)m", time_str)
    if minutes_match:
        total_seconds += int(minutes_match.group(1)) * 60

    # Seconds
    seconds_match = re.search(r"(\d+)s", time_str)
    if seconds_match:
        total_seconds += int(seconds_match.group(1))

    return total_seconds


def format_duration(seconds: int) -> str:
    """
    Format seconds as human-readable duration.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted string like "1h 23m 45s"
    """
    if seconds <= 0:
        return "0s"

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    parts = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if secs > 0 or not parts:
        parts.append(f"{secs}s")

    return " ".join(parts)


def format_number(num: int) -> str:
    """
    Format number with thousands separators.

    Args:
        num: Number to format

    Returns:
        Formatted string like "1,234,567"
    """
    return f"{num:,}"


def parse_coordinates(coord_str: str) -> tuple[Optional[int], Optional[int]]:
    """
    Parse coordinate string to x, y tuple.

    Args:
        coord_str: String like "(123, -456)" or "123,-456"

    Returns:
        Tuple of (x, y) or (None, None) if parsing fails
    """
    # Remove parentheses and spaces
    clean_str = coord_str.replace("(", "").replace(")", "").replace(" ", "")

    # Try to split by comma
    parts = clean_str.split(",")
    if len(parts) != 2:
        return None, None

    try:
        x = int(parts[0])
        y = int(parts[1])
        return x, y
    except ValueError:
        return None, None


def calculate_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """
    Calculate distance between two coordinates.

    Args:
        x1, y1: First coordinate
        x2, y2: Second coordinate

    Returns:
        Distance as float
    """
    return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5


def is_valid_coordinates(x: int, y: int) -> bool:
    """
    Check if coordinates are within valid Travian map bounds.

    Args:
        x: X coordinate
        y: Y coordinate

    Returns:
        True if valid, False otherwise
    """
    # Travian map is typically -400 to 400 for both axes
    # Adjust bounds as needed for specific servers
    return -400 <= x <= 400 and -400 <= y <= 400


def mask_sensitive_data(data: str, patterns: Optional[Dict[str, str]] = None) -> str:
    """
    Mask sensitive data in strings for logging.

    Args:
        data: String potentially containing sensitive data
        patterns: Optional dict of pattern->replacement for custom masking

    Returns:
        String with sensitive data masked
    """
    if not patterns:
        patterns = {
            r'"password"\s*:\s*"[^"]*"': '"password": "***"',
            r'"jwt"\s*:\s*"[^"]*"': '"jwt": "***"',
            r"password=[\w\d]+": "password=***",
            r"checksum=[a-f0-9]{6}": "checksum=***",
        }

    result = data
    for pattern, replacement in patterns.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

    return result


def extract_village_id_from_url(url: str) -> Optional[int]:
    """
    Extract village ID from Travian URLs.

    Args:
        url: URL that may contain village ID

    Returns:
        Village ID as integer or None
    """
    # Common patterns for village IDs in URLs
    patterns = [r"village[ID]=(\d+)", r"newdid=(\d+)", r"id=(\d+)", r"/village/(\d+)"]

    for pattern in patterns:
        match = re.search(pattern, url, re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue

    return None


def get_tribe_from_id(tribe_id: int) -> str:
    """
    Get tribe name from tribe ID.

    Args:
        tribe_id: Numeric tribe ID

    Returns:
        Tribe name string
    """
    tribe_names = {1: "Roman", 2: "Teuton", 3: "Gaul"}

    return tribe_names.get(tribe_id, f"Unknown ({tribe_id})")


def sleep_with_jitter(base_seconds: float, jitter_ratio: float = 0.1) -> None:
    """
    Sleep with random jitter to avoid detection.

    Args:
        base_seconds: Base sleep time
        jitter_ratio: Maximum jitter as ratio of base time
    """
    import random

    jitter = random.uniform(-jitter_ratio, jitter_ratio) * base_seconds
    actual_sleep = max(0.1, base_seconds + jitter)
    time.sleep(actual_sleep)


def chunk_list(items: list, chunk_size: int) -> list[list]:
    """
    Split a list into chunks of specified size.

    Args:
        items: List to chunk
        chunk_size: Maximum size of each chunk

    Returns:
        List of chunks
    """
    chunks = []
    for i in range(0, len(items), chunk_size):
        chunks.append(items[i : i + chunk_size])
    return chunks


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0, backoff_factor: float = 2.0):
    """
    Decorator for retrying functions with exponential backoff.

    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial delay between retries
        backoff_factor: Multiplier for delay on each retry
    """

    def decorator(func):
        async def async_wrapper(*args, **kwargs):
            delay = base_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        await asyncio.sleep(delay)
                        delay *= backoff_factor
                    else:
                        raise last_exception

            raise last_exception

        def sync_wrapper(*args, **kwargs):
            delay = base_delay
            last_exception = None

            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries:
                        time.sleep(delay)
                        delay *= backoff_factor
                    else:
                        raise last_exception

            raise last_exception

        import asyncio

        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator
