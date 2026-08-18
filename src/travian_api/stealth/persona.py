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


# Exact sec-ch-ua the libcurl-impersonate transport advertises for each pinned
# target, captured from the locked curl_cffi 0.15.0. Chrome's GREASE brand token,
# its version, AND the brand ORDER differ from release to release, so a single
# synthesized string cannot match every target: a detector that knows the target
# (from the TLS/HTTP2 fingerprint) can compare the expected brand list against
# what we send. Sending the target's real value keeps the two coherent. The
# brand list is OS-independent (it carries no platform), so these are correct
# whether the UA advertises Windows or macOS. Re-verify with a live capture (see
# test_persona_client_hints) whenever curl_cffi is upgraded.
_SEC_CH_UA_BY_MAJOR: dict[int, str] = {
    142: '"Chromium";v="142", "Google Chrome";v="142", "Not_A Brand";v="99"',
    145: '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
    146: '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
}


def _sec_ch_ua_for(chrome_major: int) -> str:
    """sec-ch-ua header value for a Chrome major version.

    Uses the exact per-target brand list the transport advertises (see
    ``_SEC_CH_UA_BY_MAJOR``). An unknown/future major falls back to the
    current-era GREASE format (``"Not-A.Brand";v="24"``) rather than the legacy
    ``v="99"`` marker, with the real brand versions tracking the UA major.
    """
    exact = _SEC_CH_UA_BY_MAJOR.get(chrome_major)
    if exact is not None:
        return exact
    return (
        f'"Chromium";v="{chrome_major}", "Not-A.Brand";v="24", "Google Chrome";v="{chrome_major}"'
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

    # Platform is advertised as Windows to match the Windows UA pool above. The
    # libcurl-impersonate transport's own TLS/HTTP2 fingerprint for these targets
    # may present a different OS; the sec-ch-ua brand list is OS-independent so it
    # stays coherent either way, but if a transport upgrade makes the OS a
    # correlatable tell, switch the UA pool and this platform together (they must
    # always agree). Kept as a single deliberate choice here rather than split.
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
        # Client hints are DERIVED from the User-Agent, so recompute them on load
        # from the persisted UA rather than trusting the serialized values. A
        # persona written by an earlier revision keeps the stale sec-ch-ua GREASE
        # (e.g. the legacy Not-A.Brand;v=99 marker) for its whole 365-day TTL
        # otherwise — the in-place upgrade population that most needs the hardening
        # would never get it. The UA, impersonation target, salt, server URL and
        # created_at are preserved so cookies see no identity rotation and the TTL
        # is not reset.
        major = _chrome_major(data["user_agent"])
        canonical_sec_ch_ua = _sec_ch_ua_for(major) if major else data["sec_ch_ua"]
        canonical_impersonate = _impersonate_for(major) if major else data["impersonate"]
        persona = Persona(
            user_agent=data["user_agent"],
            impersonate=canonical_impersonate,
            sec_ch_ua=canonical_sec_ch_ua,
            sec_ch_ua_platform=data["sec_ch_ua_platform"],
            sec_ch_ua_mobile=data["sec_ch_ua_mobile"],
            accept_language=data["accept_language"],
            is_chromium=data["is_chromium"],
            salt=data.get("salt", ""),
        )
        normalized = (
            canonical_sec_ch_ua != data["sec_ch_ua"] or canonical_impersonate != data["impersonate"]
        )
        if not persona.salt:
            # Migrate a pre-salt persona file: generate a stable salt so the
            # account's behavioral seeds stay fixed across future restarts.
            persona.salt = _new_salt()
            normalized = True
        if normalized:
            # Persist the normalization (and/or backfilled salt), preserving
            # created_at so the TTL isn't reset; pin a missing server_url now so
            # future server-mismatch rotation works. A second load then returns
            # the same result without rotating identity.
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
