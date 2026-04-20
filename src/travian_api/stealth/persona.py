"""Browser persona — coherent identity for TLS fingerprint, headers, and UA.

A Persona ties together User-Agent string, curl_cffi impersonate target,
sec-ch-ua headers, platform, and accept-language so that every layer of the
stealth stack presents the same browser identity.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Chrome-on-Windows UA pool (curl_cffi can impersonate these) ────────

_CHROME_WINDOWS_UAS = [
    # Chrome 132-136 on Windows 10/11 (current stable range early 2026)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
]

# Map Chrome major version -> curl_cffi impersonate target.
# curl_cffi supports: chrome131, chrome133a, chrome136.
# For versions without an exact match we pick the nearest lower target.
_IMPERSONATE_MAP: dict[int, str] = {
    136: "chrome136",
    135: "chrome136",
    134: "chrome133a",
    133: "chrome133a",
    132: "chrome131",
}
_IMPERSONATE_FALLBACK = "chrome136"


def _chrome_major(ua: str) -> int:
    """Extract Chrome major version from a UA string."""
    m = re.search(r"Chrome/(\d+)", ua)
    return int(m.group(1)) if m else 0


def _impersonate_for(chrome_major: int) -> str:
    """Pick the best curl_cffi impersonate target for a Chrome version."""
    return _IMPERSONATE_MAP.get(chrome_major, _IMPERSONATE_FALLBACK)


def _sec_ch_ua_for(chrome_major: int) -> str:
    """Build sec-ch-ua header value from Chrome major version."""
    return (
        f'"Chromium";v="{chrome_major}", '
        f'"Google Chrome";v="{chrome_major}", '
        f'"Not-A.Brand";v="99"'
    )


# ── Accept-Language from server URL ────────────────────────────────────

_LOCALE_MAP: list[tuple[str, str]] = [
    (".de.", "de-DE,de;q=0.9,en;q=0.8"),
    (".fr.", "fr-FR,fr;q=0.9,en;q=0.8"),
    (".it.", "it-IT,it;q=0.9,en;q=0.8"),
    (".es.", "es-ES,es;q=0.9,en;q=0.8"),
    (".pl.", "pl-PL,pl;q=0.9,en;q=0.8"),
    (".nl.", "nl-NL,nl;q=0.9,en;q=0.8"),
    (".pt.", "pt-PT,pt;q=0.9,en;q=0.8"),
    (".ru.", "ru-RU,ru;q=0.9,en;q=0.8"),
    (".tr.", "tr-TR,tr;q=0.9,en;q=0.8"),
    (".cz.", "cs-CZ,cs;q=0.9,en;q=0.8"),
    (".ro.", "ro-RO,ro;q=0.9,en;q=0.8"),
    (".hu.", "hu-HU,hu;q=0.9,en;q=0.8"),
    (".se.", "sv-SE,sv;q=0.9,en;q=0.8"),
    (".dk.", "da-DK,da;q=0.9,en;q=0.8"),
    (".no.", "nb-NO,nb;q=0.9,en;q=0.8"),
    (".fi.", "fi-FI,fi;q=0.9,en;q=0.8"),
    (".gr.", "el-GR,el;q=0.9,en;q=0.8"),
    ("europe.travian.com", "en-GB,en;q=0.9"),
]
_DEFAULT_ACCEPT_LANG = "en-US,en;q=0.9"


def _accept_language_for(server_url: str) -> str:
    """Derive Accept-Language from the Travian server URL."""
    if not server_url:
        return _DEFAULT_ACCEPT_LANG
    host = urlparse(server_url).hostname or server_url
    for pattern, lang in _LOCALE_MAP:
        if pattern in host:
            return lang
    return _DEFAULT_ACCEPT_LANG


# ── Persona dataclass ──────────────────────────────────────────────────


@dataclass
class Persona:
    """Coherent browser identity used across all stealth layers."""

    user_agent: str
    impersonate: str  # curl_cffi target e.g. "chrome136", "chrome131"
    sec_ch_ua: str  # e.g. '"Chromium";v="136", "Google Chrome";v="136"...'
    sec_ch_ua_platform: str  # e.g. '"Windows"'
    sec_ch_ua_mobile: str  # "?0"
    accept_language: str  # e.g. "en-US,en;q=0.9"
    is_chromium: bool  # True for Chrome/Edge


# ── Factory ────────────────────────────────────────────────────────────


def build_persona(ua: str | None = None, server_url: str = "") -> Persona:
    """Build a coherent Persona from a UA string (or pick one at random).

    If *ua* is None, a random Chrome-on-Windows UA is selected from the
    internal pool.  The resulting Persona has consistent sec-ch-ua,
    platform, impersonate target, and accept-language values.
    """
    import random

    if ua is None:
        ua = random.choice(_CHROME_WINDOWS_UAS)

    major = _chrome_major(ua)
    if major == 0:
        # Non-Chrome UA slipped in — force a safe Chrome default
        ua = random.choice(_CHROME_WINDOWS_UAS)
        major = _chrome_major(ua)

    return Persona(
        user_agent=ua,
        impersonate=_impersonate_for(major),
        sec_ch_ua=_sec_ch_ua_for(major),
        sec_ch_ua_platform='"Windows"',
        sec_ch_ua_mobile="?0",
        accept_language=_accept_language_for(server_url),
        is_chromium=True,
    )


# ── Persistence (F2) ──────────────────────────────────────────────────

_PERSONA_TTL_DAYS = 7


def save_persona(persona: Persona, path: Path) -> None:
    """Persist persona to a JSON file with a creation timestamp."""
    data = asdict(persona)
    data["created_at"] = datetime.now(timezone.utc).isoformat()
    try:
        path.write_text(json.dumps(data, indent=2))
        logger.debug("Saved persona to %s", path)
    except Exception as exc:
        logger.warning("Failed to save persona to %s: %s", path, exc)


def load_persona(path: Path, ttl_days: int = _PERSONA_TTL_DAYS) -> Persona | None:
    """Load persona from JSON file, returning None if missing or expired."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        created_at = datetime.fromisoformat(data["created_at"])
        # Ensure timezone-aware comparison
        now = datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        age_days = (now - created_at).total_seconds() / 86400
        if age_days > ttl_days:
            logger.info("Persona expired (%.1f days old, ttl=%d), will create new", age_days, ttl_days)
            return None
        return Persona(
            user_agent=data["user_agent"],
            impersonate=data["impersonate"],
            sec_ch_ua=data["sec_ch_ua"],
            sec_ch_ua_platform=data["sec_ch_ua_platform"],
            sec_ch_ua_mobile=data["sec_ch_ua_mobile"],
            accept_language=data["accept_language"],
            is_chromium=data["is_chromium"],
        )
    except Exception as exc:
        logger.warning("Failed to load persona from %s: %s", path, exc)
        return None
