"""Browser persona — coherent identity for TLS fingerprint, headers, and UA.

A Persona ties together User-Agent string, curl_cffi impersonate target,
sec-ch-ua headers, platform, and accept-language so that every layer of the
stealth stack presents the same browser identity.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ── Chrome-on-Windows UA pool (curl_cffi can impersonate these) ────────

_CHROME_WINDOWS_UAS = [
    # Only versions with EXACT curl_cffi impersonation support, so the UA,
    # sec-ch-ua and TLS fingerprint always agree. Keep this in step with the
    # newest targets the installed curl_cffi ships (see _IMPERSONATE_MAP): a
    # pool pinned years behind real Chrome ages every layer in lockstep.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36",
]

# Map Chrome major version -> curl_cffi impersonate target.
# Only exact matches — no version skew between UA and TLS fingerprint.
_IMPERSONATE_MAP: dict[int, str] = {
    146: "chrome146",
    145: "chrome145",
    142: "chrome142",
}
_IMPERSONATE_FALLBACK = "chrome146"


def _chrome_major(ua: str) -> int:
    """Extract Chrome major version from a UA string."""
    m = re.search(r"Chrome/(\d+)", ua)
    return int(m.group(1)) if m else 0


def _impersonate_for(chrome_major: int) -> str:
    """Pick the best curl_cffi impersonate target for a Chrome version."""
    return _IMPERSONATE_MAP.get(chrome_major, _IMPERSONATE_FALLBACK)


def _sec_ch_ua_for(chrome_major: int) -> str:
    """Build sec-ch-ua header value from Chrome major version.

    The GREASE brand uses the CURRENT-era token (``"Not?A_Brand";v="24"``), not
    the legacy ``"Not-A.Brand";v="99"`` from the Chrome ~100 era — a v="99"
    GREASE on a Chrome 14x UA is an obvious mismatch. The GREASE is
    spec-randomized and servers must ignore it, so a plausible current token is
    what matters; the real brand versions track the UA major so they can't skew.
    """
    return (
        f'"Chromium";v="{chrome_major}", "Google Chrome";v="{chrome_major}", "Not?A_Brand";v="24"'
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
    # Per-account random salt. The visible identity (UA/lang/server) has low
    # entropy on a single world — only a few UAs and one server-derived
    # language — so accounts would otherwise collide into a handful of latent
    # behavioral buckets (gap shape, warm-up routes). The salt is the entropy
    # source that makes each account's behavioral seeds genuinely distinct. It
    # is never sent to the server; it only seeds local RNGs.
    salt: str = ""


def _new_salt() -> str:
    """Generate a fresh high-entropy behavioral salt."""
    return secrets.token_hex(8)


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
        salt=_new_salt(),
    )


# ── Persistence (F2) ──────────────────────────────────────────────────

# A real browser profile keeps its UA/TLS for the lifetime of the install,
# not 7 days. Rotating UA mid-cookie-jar is itself a tell: same auth cookie,
# different Chrome version, different TLS fingerprint. Pin the persona to
# roughly cookie lifetime so it only rotates on a deliberate identity reset.
_PERSONA_TTL_DAYS = 365


def save_persona(
    persona: Persona, path: Path, server_url: str = "", created_at: str | None = None
) -> None:
    """Persist persona to a JSON file with a creation timestamp.

    The server URL is recorded alongside the persona so a server change
    (e.g. switching from .com to .de, or moving to a new world) rotates
    the persona automatically — language pack and timezone implications
    of accept_language wouldn't survive a server migration realistically.

    ``created_at`` lets a caller preserve the original timestamp (e.g. when
    re-saving only to backfill a salt) so the persona TTL isn't reset.
    """
    data = asdict(persona)
    data["created_at"] = created_at or datetime.now(UTC).isoformat()
    if server_url:
        data["server_url"] = server_url
    try:
        path.write_text(json.dumps(data, indent=2))
        logger.debug("Saved persona to %s", path)
    except Exception as exc:
        logger.warning("Failed to save persona to %s: %s", path, exc)


def load_persona(
    path: Path,
    ttl_days: int = _PERSONA_TTL_DAYS,
    *,
    server_url: str = "",
) -> Persona | None:
    """Load persona from JSON file, returning None if missing/expired/mismatched.

    Returns None — forcing a fresh persona — when the saved server URL
    doesn't match the current ``server_url`` argument. Same cookie file
    against a different server is the same scenario as an account change:
    we don't want to reuse the old TLS/locale identity.
    """
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text())
        created_at = datetime.fromisoformat(data["created_at"])
        # Ensure timezone-aware comparison
        now = datetime.now(UTC)
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        age_days = (now - created_at).total_seconds() / 86400
        if age_days > ttl_days:
            logger.info(
                "Persona expired (%.1f days old, ttl=%d), will create new", age_days, ttl_days
            )
            return None
        saved_server = data.get("server_url", "")
        if server_url and saved_server and saved_server != server_url:
            logger.info(
                "Persona server mismatch (saved=%s, current=%s) — rotating identity",
                saved_server,
                server_url,
            )
            return None
        persona = Persona(
            user_agent=data["user_agent"],
            impersonate=data["impersonate"],
            sec_ch_ua=data["sec_ch_ua"],
            sec_ch_ua_platform=data["sec_ch_ua_platform"],
            sec_ch_ua_mobile=data["sec_ch_ua_mobile"],
            accept_language=data["accept_language"],
            is_chromium=data["is_chromium"],
            salt=data.get("salt", ""),
        )
        if not persona.salt:
            # Migrate a pre-salt persona file: generate a stable salt and
            # persist it (preserving created_at so the TTL isn't reset) so the
            # account's behavioral seeds stay fixed across future restarts. If
            # the legacy file also lacked a server_url, pin it to the current
            # server now so future server-mismatch rotation works.
            persona.salt = _new_salt()
            save_persona(
                persona,
                path,
                server_url=saved_server or server_url,
                created_at=data["created_at"],
            )
        return persona
    except Exception as exc:
        logger.warning("Failed to load persona from %s: %s", path, exc)
        return None
