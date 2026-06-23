"""Auto-Scout service — scan map, find villages, and send scouts automatically."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import random
import re
from contextlib import asynccontextmanager
from html import unescape as html_unescape
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple

from ..clients.http_client import HttpClient
from ..exceptions import ReconStrictViolation
from ..models.farm_list import MapTileInfo
from ..parsers.html_parser import clean_unicode

logger = logging.getLogger(__name__)


# Module-level — module-private. Used by _parse_tile_details below.
# Outer wrapper to anchor parsing inside the right table block.
_OASIS_BONUS_TABLE_RE = re.compile(r'<table[^>]*\bid="distribution"[^>]*>(.*?)</table>', re.DOTALL)
# Each <tr> in the bonus table emits three cells in fixed order on modern
# Travian (post-2025): ico → val → desc. The ``ico`` cell carries an
# ``<i class="rN">`` icon (r1=wood, r2=clay, r3=iron, r4=crop); the
# ``val`` cell carries the percentage, often bidi-wrapped and HTML-entity
# encoded (``&#x202d;&#x202d;50&#x202c;&#37;&#x202c;``); the ``desc``
# cell carries the localized resource name (used for display).
#
# Older skins emitted desc → val only; we keep that as a backstop by
# searching for any (resource-class, percentage) pair within a row.
_OASIS_BONUS_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
# Resource icon (modern shape). Catches `<i class="r4" ...>` and
# `<img class="resource r4" ...>` variants. The group captures the
# numeric ID alone.
_OASIS_BONUS_ICON_RE = re.compile(r'class="[^"]*\br(\d)\b[^"]*"', re.IGNORECASE)
# Locale-stable canonical mapping. Travian's resource icon numbering is
# stable across servers and locales: r1=Wood, r2=Clay, r3=Iron, r4=Crop.
_RESOURCE_ID_BY_ICON = {
    "1": "wood",
    "2": "clay",
    "3": "iron",
    "4": "crop",
}
# Localized name fallback — only used when the icon class is missing
# (very old skin). English plus common locales likely to be hit; new
# locales will produce empty bonus and log a warning, surfacing the
# need to extend this list.
_RESOURCE_ID_BY_NAME = {
    "wood": "wood",
    "lumber": "wood",
    "holz": "wood",
    "drewno": "wood",
    "drevo": "wood",
    "ahşap": "wood",
    "дерево": "wood",
    "ξύλο": "wood",
    "clay": "clay",
    "lehm": "clay",
    "glina": "clay",
    "hlina": "clay",
    "kil": "clay",
    "глина": "clay",
    "πηλός": "clay",
    "iron": "iron",
    "eisen": "iron",
    "żelazo": "iron",
    "železo": "iron",
    "demir": "iron",
    "железо": "iron",
    "σίδηρος": "iron",
    "crop": "crop",
    "cereal": "crop",
    "cereals": "crop",
    "getreide": "crop",
    "zboże": "crop",
    "obilie": "crop",
    "tahıl": "crop",
    "зерно": "crop",
    "δημητριακά": "crop",
}
# Display labels in English. Used when rendering a locale-stable
# string for users on any locale. Order = Wood, Clay, Iron, Crop.
_RESOURCE_DISPLAY = {
    "wood": "Wood",
    "clay": "Clay",
    "iron": "Iron",
    "crop": "Crop",
}


# Capital marker keywords across Travian locales. Add new locales here
# when discovered. Substring-and-case-insensitive against the value of
# ``typeText`` (e.g. ``(Capital)``, ``(Hauptdorf)``). Avoid short prefix
# stems (e.g. plain "hlavn"): those false-positive against unrelated
# Czech/Slovak words. Stick to recognisable, locale-explicit forms.
_CAPITAL_KEYWORDS_RE = re.compile(
    r"capital"  # en/es/pt/it (also matches "capitale")
    r"|hauptdorf"  # de
    r"|stolica|stolnica"  # pl, sl
    r"|kapital"  # also fragments of de "Hauptstadt"-style skins
    r"|столица"  # ru/uk/sr
    r"|başkent"  # tr
    r"|hlavní|hlavné"  # cs, sk
    r"|főváros"  # hu
    r"|hoofdstad"  # nl
    r"|huvudstad"  # sv
    r"|pääkaupunki"  # fi
    r"|首都"  # cjk
    r"|العاصمة",  # ar
    re.IGNORECASE,
)


def _extract_balanced(text: str, start: int, opener: str, closer: str) -> Optional[str]:
    """Return the substring from text[start] (must be ``opener``) through
    its balanced ``closer``, treating JSON-style string literals as opaque
    so braces/brackets inside string values don't fool the depth counter.

    Returns None if start is past the end of ``text`` or no matching
    closer is found.
    """
    if start >= len(text) or text[start] != opener:
        return None
    depth = 0
    in_string = False
    escape = False
    i = start
    while i < len(text):
        c = text[i]
        if escape:
            escape = False
        elif c == "\\":
            escape = True
        elif c == '"':
            in_string = not in_string
        elif not in_string:
            if c == opener:
                depth += 1
            elif c == closer:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        i += 1
    return None


def _parse_capital_id_from_profile_html(html: str) -> Optional[int]:
    """Locate the capital village ID inside a Travian profile page.

    Modern Travian (post-2025 redesign) embeds the player's villages as a
    JSON array inside the profile payload, with each village carrying a
    localized ``"typeText":"(Capital)"`` marker on the capital row. Older
    versions used boolean ``"isMainVillage":true`` / ``"isCapital":true``
    keys instead. Very old skins drop both and only mark the capital row
    in plain HTML next to the village's ``newdid=N`` link.

    Strategies (in order):
      1. Locate the ``"villages":[ ... ]`` array, extract it as a
         structurally-balanced substring, ``json.loads`` it, and inspect
         each entry. Handles nested ``occupiedOases``, arbitrarily large
         village objects, and string values containing braces.
      2. Same array — fall back to legacy boolean markers.
      3. HTML pattern: capital keyword text adjacent to ``newdid=N``.

    Returns the capital's village id (matches the ``did`` the map scan
    stores in ``MapTileInfo.village_id``) or None if nothing matches.
    """
    villages = _extract_villages_array(html)
    if villages:
        for v in villages:
            if not isinstance(v, dict):
                continue
            type_text = v.get("typeText")
            if not isinstance(type_text, str) or not type_text:
                continue
            if not _CAPITAL_KEYWORDS_RE.search(type_text):
                continue
            vid = v.get("id")
            if isinstance(vid, int):
                return vid
            if isinstance(vid, str) and vid.isdigit():
                return int(vid)

        for v in villages:
            if not isinstance(v, dict):
                continue
            if v.get("isMainVillage") is True or v.get("isCapital") is True:
                vid = v.get("id")
                if isinstance(vid, int):
                    return vid
                if isinstance(vid, str) and vid.isdigit():
                    return int(vid)

    # Strategy 3 — HTML last resort. Reuse the consolidated keyword
    # regex source via .pattern and widen the proximity window to 200
    # chars. Use ``.`` with DOTALL so the match can span across HTML
    # tag boundaries — the realistic case is a marker cell and a link
    # cell sitting in the same table row separated by ``</td><td>``.
    kw_src = _CAPITAL_KEYWORDS_RE.pattern
    for marker_pat in (
        rf"<a[^>]*newdid=(\d+)[^>]*>.{{0,200}}?(?:{kw_src})",
        rf"(?:{kw_src}).{{0,200}}?<a[^>]*newdid=(\d+)",
    ):
        m = re.search(marker_pat, html, re.IGNORECASE | re.DOTALL)
        if m:
            try:
                return int(m.group(1))
            except (TypeError, ValueError):
                continue
    return None


def _extract_villages_array(html: str) -> Optional[list]:
    """Pull the profile target's ``"villages":[ ... ]`` JSON array out
    of the page HTML.

    Returns a Python list of dicts on success, or None if no suitable
    array is found. The structural balancer handles nested objects
    (e.g. populated ``occupiedOases``) and string-value braces.

    A profile page can legitimately contain multiple ``"villages"``
    keys (e.g. profile target's villages, an alliance roster, hero
    villages on future redesigns). Pick the one whose entries look
    like village descriptors — i.e. carry the ``typeText`` marker
    (modern Travian) or one of the legacy boolean keys. Falls back
    to the first parseable list when no entry has any marker, so an
    unfamiliar locale doesn't break detection completely.
    """
    fallback: Optional[list] = None
    for m in re.finditer(r'"villages"\s*:\s*\[', html):
        bracket_start = m.end() - 1  # position of '['
        array_str = _extract_balanced(html, bracket_start, "[", "]")
        if array_str is None:
            continue
        try:
            parsed = json.loads(array_str)
        except (ValueError, TypeError):
            continue
        if not isinstance(parsed, list):
            continue
        if fallback is None:
            fallback = parsed
        if any(
            isinstance(v, dict) and ("typeText" in v or "isMainVillage" in v or "isCapital" in v)
            for v in parsed
        ):
            return parsed
    return fallback


def _coerce_int(value: Any) -> int:
    """Best-effort int coercion for profile JSON fields that may arrive as
    ints or numeric strings. Returns 0 on anything unparseable so callers
    don't need a separate error path."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


def _resource_id_from_type(resource_type: Any) -> Optional[str]:
    """Map a profile ``resourceType`` object to a canonical resource id.

    Travian's profile JSON tags each oasis bonus with
    ``"resourceType":{"id":N,"code":"..."}`` where the numeric id is
    locale-stable (1=wood/lumber, 2=clay, 3=iron, 4=crop). Prefer the id;
    fall back to the textual code for forward-compat.
    """
    if not isinstance(resource_type, dict):
        return None
    rid = resource_type.get("id")
    if rid is not None:
        canon = _RESOURCE_ID_BY_ICON.get(str(rid))
        if canon:
            return canon
    code = resource_type.get("code")
    if isinstance(code, str):
        return _RESOURCE_ID_BY_NAME.get(code.strip().lower())
    return None


def _extract_village_oases(html: str) -> List[dict]:
    """Per-village aggregated occupied-oasis bonus from a profile page.

    The profile's ``occupiedOases`` array embeds each occupied oasis's bonus
    directly — no separate tile-details fetch is needed::

        "occupiedOases":[
          {"bonus":[{"amount":25,"resourceType":{"id":3,"code":"iron"}},
                    {"amount":25,"resourceType":{"id":4,"code":"crop"}}]},
          {"bonus":[{"amount":25,"resourceType":{"id":1,"code":"lumber"}}]}]

    Reuses :func:`_extract_villages_array` (shared structural balancer) and
    sums every oasis's bonus into one canonical breakdown per village::

        [{"village_id": int, "x": int, "y": int, "name": str,
          "population": int, "oasis_count": int,
          "breakdown": {"wood"|"clay"|"iron"|"crop": pct}}, ...]

    ``oasis_count`` is the number of occupied oases (lets the filter reject a
    village with zero for free). Villages without a usable integer id are
    skipped. Returns ``[]`` when no villages array is present.
    """
    villages = _extract_villages_array(html)
    if not villages:
        return []
    out: List[dict] = []
    for v in villages:
        if not isinstance(v, dict):
            continue
        vid = v.get("id")
        if isinstance(vid, str) and vid.isdigit():
            vid = int(vid)
        if not isinstance(vid, int) or vid <= 0:
            continue
        breakdown: Dict[str, int] = {}
        oasis_count = 0
        raw_oases = v.get("occupiedOases")
        if isinstance(raw_oases, list):
            for oasis in raw_oases:
                if not isinstance(oasis, dict):
                    continue
                oasis_count += 1
                for bonus in oasis.get("bonus") or []:
                    if not isinstance(bonus, dict):
                        continue
                    amount = bonus.get("amount")
                    if not isinstance(amount, int) or amount <= 0:
                        continue
                    rid = _resource_id_from_type(bonus.get("resourceType"))
                    if rid:
                        breakdown[rid] = breakdown.get(rid, 0) + amount
        out.append(
            {
                "village_id": vid,
                "x": _coerce_int(v.get("x")),
                "y": _coerce_int(v.get("y")),
                "name": v.get("name") if isinstance(v.get("name"), str) else "",
                "population": _coerce_int(v.get("population")),
                "oasis_count": oasis_count,
                "breakdown": breakdown,
            }
        )
    return out


def _extract_pct_from_val_cell(cell_html: str) -> Optional[int]:
    """Pull the integer percentage out of a Travian ``<td class="val">``
    cell. Travian wraps the number in HTML-entity-encoded bidi-override
    markers (``&#x202d;&#x202d;50&#x202c;&#37;&#x202c;``) — we
    HTML-unescape, run ``clean_unicode`` to strip the raw bidi
    codepoints, then read the first run of digits.

    Strips any nested tags first so digits living inside attributes
    (``<i class="r4" title="2">``) or class names (``r4``) can't be
    picked up before the actual percentage. Skinned skins emit varied
    inner markup; tag-stripping makes the parse robust to all of them.
    """
    text_only = re.sub(r"<[^>]+>", "", cell_html)
    cleaned = clean_unicode(html_unescape(text_only))
    m = re.search(r"\+?(\d+)", cleaned)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _parse_oasis_bonus_breakdown(html: str) -> Dict[str, int]:
    """Return ``{resource_id: pct}`` for every row in the oasis bonus
    table — canonical IDs (``wood``/``clay``/``iron``/``crop``) regardless
    of the user's Travian locale.

    Strategy:
      1. Identify each ``<tr>`` inside ``<table id="distribution">``.
      2. For each row, extract a resource ID — first by the icon
         ``class="rN"`` (locale-stable), falling back to localized
         ``class="desc">Name</td>`` against the synonym dict.
      3. Extract the percentage out of ``<td class="val">…</td>``,
         stripping bidi/entity wrapping.

    Returns ``{}`` for missing/empty tables or rows we can't classify
    (locale we haven't catalogued, malformed HTML). A non-empty
    dict where ``sum(values()) > 0`` indicates a usable bonus.
    """
    out: Dict[str, int] = {}
    m = _OASIS_BONUS_TABLE_RE.search(html)
    if not m:
        return out
    body = m.group(1)
    for row_m in _OASIS_BONUS_ROW_RE.finditer(body):
        row = row_m.group(1)

        # Resource ID — prefer icon class.
        resource_id: Optional[str] = None
        icon_m = _OASIS_BONUS_ICON_RE.search(row)
        if icon_m:
            resource_id = _RESOURCE_ID_BY_ICON.get(icon_m.group(1))

        # Fallback — pull text from the ``class="desc"`` cell.
        if resource_id is None:
            desc_m = re.search(
                r'<td[^>]*\bclass="desc"[^>]*>\s*([^<]+?)\s*</td>',
                row,
                re.DOTALL,
            )
            if desc_m:
                name = clean_unicode(html_unescape(desc_m.group(1))).strip().lower()
                resource_id = _RESOURCE_ID_BY_NAME.get(name)
        if resource_id is None:
            continue

        # Percentage — from the ``class="val"`` cell.
        val_m = re.search(
            r'<td[^>]*\bclass="val"[^>]*>(.*?)</td>',
            row,
            re.DOTALL,
        )
        if not val_m:
            continue
        pct = _extract_pct_from_val_cell(val_m.group(1))
        if pct is None or pct <= 0:
            continue

        # If a row appears twice (defensive — shouldn't on real Travian),
        # take the largest value to avoid dropping legitimate data.
        prior = out.get(resource_id, 0)
        if pct > prior:
            out[resource_id] = pct
    return out


def _format_bonus_breakdown(breakdown: Dict[str, int]) -> str:
    """Render a canonical breakdown into the human-readable string the
    frontend column displays (e.g. ``"25% Iron, 25% Crop"``). Order is
    fixed: wood → clay → iron → crop, so two oases with the same bonus
    profile always read identically.
    """
    parts: list[str] = []
    for rid in ("wood", "clay", "iron", "crop"):
        pct = breakdown.get(rid, 0)
        if pct > 0:
            parts.append(f"{pct}% {_RESOURCE_DISPLAY[rid]}")
    return ", ".join(parts)


def _parse_oasis_bonus_html(html: str) -> str:
    """Return a human-readable oasis bonus string for display.

    Wraps :py:func:`_parse_oasis_bonus_breakdown` and uses the
    canonical IDs to render a stable English label string regardless
    of the user's Travian locale. Empty string when no bonus table or
    no usable rows.
    """
    return _format_bonus_breakdown(_parse_oasis_bonus_breakdown(html))


# Per-coroutine scope for the optional recon HttpClient used by
# `AutoScoutService._read_client()`. ContextVar values are propagated
# automatically to tasks created within the current task's context,
# so an `async with svc.with_recon_client(...)` block inside the
# scan coroutine will correctly route every sub-call's reads through
# the recon, even if the calls fan out via `asyncio.gather` etc.
# Concurrent operations on the same AutoScoutService instance see
# independent values without racing on shared state.
_recon_context: contextvars.ContextVar[Optional[HttpClient]] = contextvars.ContextVar(
    "auto_scout_recon_client",
    default=None,
)

# Per-coroutine strict-mode flag. When True, `_read_client()` refuses to
# fall back to the primary account: if no recon client is available it
# raises ReconStrictViolation rather than leaking an account-independent
# read onto the user's primary login. This is the load-bearing
# enforcement of the "Require background account" toggle — the per-coro
# entry guard in scout_ws is a friendlier up-front abort layered on top.
_recon_strict_context: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "auto_scout_recon_strict",
    default=False,
)


class AutoScoutService:
    """Scan the map for villages and send scouts based on filters."""

    def __init__(self, http_client: HttpClient):
        self.http_client = http_client
        # Backward-compat slot. Tests and any callers that don't use
        # the new async context manager can still set this directly
        # and `_read_client()` will honor it as a fallback. New code
        # should prefer the `with_recon_client` async context manager
        # below, which scopes the recon client to one coroutine via
        # a ContextVar — safe under concurrent scans without races
        # between scan-and-auto-scout operations sharing the same
        # AutoScoutService instance.
        #
        # Hard rule: WRITE operations (sending scouts, querying the
        # user's rally point / village state) NEVER consult this —
        # recon has no villages. See `_read_client()` callers vs
        # `self.http_client` direct callers at each site.
        self.recon_http_client: Optional[HttpClient] = None
        self._status_cb: Optional[Callable[[str], None]] = None

    @asynccontextmanager
    async def with_recon_client(
        self,
        recon_client: Optional[HttpClient],
    ) -> AsyncIterator[None]:
        """Scope a recon HttpClient to a single coroutine tree.

        Why a context manager + ContextVar instead of the simpler
        mutable attribute pattern: two concurrent scout operations
        (e.g. ``_build_scout_scan_coro`` and ``_build_auto_scout_coro``
        for the same user) share the same ``AutoScoutService`` and
        would otherwise race on a shared slot — one coroutine's
        recon-failed `None` could clobber another's `recon` and
        silently leak primary identity onto the leakier coroutine's
        read traffic. ``ContextVar`` is per-asyncio-task by default;
        ``set`` / ``reset`` is the standard pattern for scoped
        injection.

        Usage::

            async with svc.with_recon_client(recon_client):
                tiles = await svc.scan_map(cx, cy, radius)
                ...
        """
        token = _recon_context.set(recon_client)
        try:
            yield
        finally:
            _recon_context.reset(token)

    def _read_client(self) -> HttpClient:
        """HttpClient for account-independent reads (map_position,
        tile-details, profile pages). Prefers, in order:

          1. The recon client set via ``with_recon_client`` on the
             current coroutine's context (race-safe per task).
          2. The legacy mutable ``self.recon_http_client`` attribute
             (backward compat for tests / pre-context-manager code).
          3. ``self.http_client`` — the user's primary account.

        Falls through to (3) only when both (1) and (2) are absent,
        so callers can never accidentally lose recon routing
        mid-scan to a concurrent operation's cleanup.

        Exception: when strict mode is active on the current
        coroutine (``_recon_strict_context`` is True), the task-local
        ContextVar (1) is the SOLE accepted authority. If it is unset,
        this raises :class:`ReconStrictViolation` — it will NOT fall
        back to the legacy shared attribute (2) or the primary (3),
        because (2) is mutable shared state another concurrent
        operation could have populated with an unrelated client.
        That makes "Require background account" un-bypassable: under
        strict mode no read can reach the primary, no read can silently
        ride a stale cross-operation recon client, and even a scoped
        client that turns out to BE the primary (recon-manager bug) is
        rejected.
        """
        strict = _recon_strict_context.get()
        scoped = _recon_context.get()
        if scoped is not None:
            # Under strict mode the scoped client must be a GENUINE recon
            # client — never the primary. If the recon manager ever handed
            # back the primary (bug / cache corruption), refuse rather than
            # "succeed" with a read on the user's own account.
            if strict and scoped is self.http_client:
                raise ReconStrictViolation()
            return scoped
        # Strict mode: the per-task ContextVar above is the only client
        # we trust. It was unset, so refuse — raising here guarantees the
        # read NEVER physically dispatches on the primary (and never on a
        # stale shared recon client), regardless of any caller that
        # forgot the up-front entry guard.
        if strict:
            raise ReconStrictViolation()
        if self.recon_http_client is not None:
            return self.recon_http_client
        return self.http_client

    def on_status(self, cb: Callable[[str], None]) -> None:
        self._status_cb = cb

    def _report(self, msg: str) -> None:
        if self._status_cb:
            self._status_cb(msg)
        logger.info(msg)

    # ── Map scanning ─────────────────────────────────────────────────

    async def scan_map(
        self,
        center_x: int,
        center_y: int,
        radius: int,
    ) -> List[MapTileInfo]:
        """
        Scan the map around (center_x, center_y) within radius.
        Returns basic tile info from the map/position endpoint.

        The API returns a 31x31 grid per call (zoomLevel=3).
        For larger radii, multiple calls are made in a grid pattern.
        """
        tiles: Dict[Tuple[int, int], dict] = {}
        # Each /api/v1/map/position call returns a 31x31 tile region
        # CENTERED on the requested point — so one call covers
        # center ± HALF tiles in each axis. To span the user-requested
        # radius around (center_x, center_y), walk concentric rings
        # of centers spaced STRIDE apart, just enough that the outer
        # regions reach `radius` from the user-supplied center.
        # Critically: when radius <= HALF we issue ONE call AT
        # (center_x, center_y) — the old algorithm placed the first
        # center at (center_x - radius, center_y - radius) which only
        # covered the bottom-left quadrant of the requested area for
        # small radii.
        HALF = 15
        STRIDE = 30
        extras = max(0, (radius - HALF + STRIDE - 1) // STRIDE)
        scan_centers: List[Tuple[int, int]] = []
        for dx in range(-extras, extras + 1):
            for dy in range(-extras, extras + 1):
                scan_centers.append((center_x + dx * STRIDE, center_y + dy * STRIDE))

        # Stealth: a player panning around their map clicks the area near
        # their village first and pans outward in clusters — not raster
        # left-to-right top-to-bottom. Sort by distance from the requested
        # center, then shuffle within small buckets of 4 so the visit order
        # is "nearby cluster, slightly varied" instead of a deterministic
        # nested-coordinate grid.
        scan_centers.sort(key=lambda p: (p[0] - center_x) ** 2 + (p[1] - center_y) ** 2)
        for i in range(0, len(scan_centers), 4):
            # NOTE: list slice creates a copy; random.shuffle on the slice
            # alone is a no-op. Assign the shuffled bucket back in-place.
            bucket = scan_centers[i : i + 4]
            random.shuffle(bucket)
            scan_centers[i : i + 4] = bucket

        self._report(
            f"Scanning {len(scan_centers)} map region(s) around ({center_x},{center_y}) r={radius}"
        )

        # Establish map context once before the scan batch (stealth) — the
        # tile XHRs are fired by the map page's frontend JS, so the
        # Referer chain must lead from karte.php. Navigate the SAME
        # client that will actually fire the XHRs (recon when active,
        # otherwise primary) — otherwise the Referer header doesn't
        # match the requesting account's last page-load.
        read_client = self._read_client()
        navigator = getattr(read_client, "navigator", None)
        if navigator is not None and navigator.enabled:
            await navigator.navigate_to_map()

        for sx, sy in scan_centers:
            resp = await read_client.post_json(
                "/api/v1/map/position",
                {
                    "data": {
                        "x": sx,
                        "y": sy,
                        "zoomLevel": 3,
                        "ignorePositions": [],
                    }
                },
                request_type="xhr",
            )
            for t in resp.get("tiles", []):
                pos = t.get("position", {})
                x, y = pos.get("x", 0), pos.get("y", 0)
                if (x, y) not in tiles:
                    tiles[(x, y)] = t

        # Filter to tiles within the actual radius and that have a village/oasis
        result: List[MapTileInfo] = []
        for (x, y), t in tiles.items():
            dist = math.sqrt((x - center_x) ** 2 + (y - center_y) ** 2)
            if dist > radius:
                continue

            did = t.get("did")
            if did is None:
                continue  # wilderness, no village

            uid = t.get("uid")
            aid = t.get("aid")
            title = t.get("title", "")

            # Parse village name from title like "{k.dt} VillageName".
            # Strip Travian's bidi-override markers (U+202D / U+202C, also
            # served as &#x202d; entities) so the UI doesn't render them
            # as literal "&#x202d;" text.
            village_name = ""
            name_match = re.search(r"\{k\.dt\}\s*(.+)", title)
            if name_match:
                village_name = clean_unicode(html_unescape(name_match.group(1)))

            is_oasis = "{k.fo}" in title or "{k.bt}" in title

            info = MapTileInfo(
                x=x,
                y=y,
                village_id=did if did > 0 else 0,
                player_id=uid if uid else None,
                alliance_id=aid if aid else None,
                village_name=village_name,
                distance=round(dist, 2),
                is_oasis=is_oasis,
                is_abandoned=did == -1 and uid is None,
            )
            result.append(info)

        self._report(f"Found {len(result)} tiles with villages/oases in radius")
        return result

    async def get_tile_details(self, x: int, y: int) -> MapTileInfo:
        """Get detailed info for a single tile via tile-details API.

        Account-independent — the response is identical regardless of
        which logged-in player queries it. Routes through the recon
        HttpClient when configured (concentrates bot-detection on the
        disposable account).
        """
        resp = await self._read_client().post_json(
            "/api/v1/map/tile-details", {"x": x, "y": y}, request_type="xhr"
        )
        html = resp.get("html", "")
        return self._parse_tile_details(x, y, html)

    async def enrich_tiles(
        self, tiles: List[MapTileInfo], concurrency: int = 15
    ) -> List[MapTileInfo]:
        """
        Enrich tiles with population/tribe/player from tile-details API.

        Processes tiles sequentially to respect the global request throttler.
        The ``concurrency`` parameter is accepted for API compatibility but
        no longer used — concurrent requests bypass the throttler's burst
        protection and trigger Travian's rate limiter (connection resets).
        """
        enriched: List[MapTileInfo] = []
        total = len(tiles)

        for i, tile in enumerate(tiles):
            try:
                detail = await self.get_tile_details(tile.x, tile.y)
                detail.distance = tile.distance
                detail.is_oasis = tile.is_oasis
                detail.is_abandoned = tile.is_abandoned
                if not detail.village_name and tile.village_name:
                    detail.village_name = tile.village_name
                # Preserve player/alliance from map scan when tile-details
                # didn't extract them (occupied oases may use different HTML)
                if not detail.player_id and tile.player_id:
                    detail.player_id = tile.player_id
                if not detail.player_name and tile.player_name:
                    detail.player_name = tile.player_name
                if not detail.alliance_id and tile.alliance_id:
                    detail.alliance_id = tile.alliance_id
                if not detail.alliance_name and tile.alliance_name:
                    detail.alliance_name = tile.alliance_name
                enriched.append(detail)
            except ReconStrictViolation:
                # Strict background-account mode with no recon available:
                # this is an invariant breach, never a per-tile hiccup.
                # Propagate so the operation fails loudly instead of
                # degrading to a half-enriched result set.
                raise
            except Exception as e:
                logger.warning("Failed to get details for (%s,%s): %s", tile.x, tile.y, e)
                enriched.append(tile)

            # Progress report every 10 tiles
            if (i + 1) % 10 == 0 or i == total - 1:
                self._report(f"Enriched {i + 1}/{total} tiles...")

        return enriched

    @staticmethod
    def filter_canonical(
        tiles: List[MapTileInfo],
        *,
        exclude_oases: bool = True,
        exclude_abandoned: bool = True,
        exclude_natars_alliance_tag: bool = True,
        exclude_own_village_ids: Optional[Set[int]] = None,
        exclude_player_ids: Optional[Set[int]] = None,
        exclude_alliance_tags_lower: Optional[Set[str]] = None,
        exclude_player_names_lower: Optional[Set[str]] = None,
        player_total_pops: Optional[Dict[int, int]] = None,
        max_player_total_pop: Optional[int] = None,
        max_target_village_pop: Optional[int] = None,
        min_target_village_pop: Optional[int] = None,
        within_chebyshev_of: Optional[List[Tuple[int, int, int]]] = None,
    ) -> Tuple[List[MapTileInfo], Dict[str, int]]:
        """Canonical farm-builder filter — shared single source of truth.

        Returns a tuple ``(survivors, drop_counts)`` where ``drop_counts`` is
        a dict keyed by reason string. This is the ONE filter function both
        the dry-run preview and the live execution path must call. Do not
        reimplement any of these predicates elsewhere; call this instead.
        """
        drop_counts: Dict[str, int] = {}

        def _drop(reason: str) -> None:
            drop_counts[reason] = drop_counts.get(reason, 0) + 1

        result: List[MapTileInfo] = []
        own_vids = exclude_own_village_ids or set()
        exc_pids = exclude_player_ids or set()
        exc_tags = {t.lower() for t in (exclude_alliance_tags_lower or set())}
        exc_names = {n.lower() for n in (exclude_player_names_lower or set())}

        for t in tiles:
            if exclude_oases and t.is_oasis:
                _drop("oasis")
                continue
            if exclude_abandoned and t.is_abandoned:
                _drop("abandoned")
                continue
            if t.player_id is None or t.player_id <= 0:
                _drop("no_player")
                continue
            if t.village_id <= 0:
                _drop("no_village_id")
                continue
            if t.population <= 0:
                _drop("zero_population")
                continue
            if t.village_id in own_vids:
                _drop("own_village")
                continue
            if t.player_id in exc_pids:
                _drop("excluded_player_id")
                continue
            tag = (t.alliance_name or "").lower()
            if exclude_natars_alliance_tag and tag in {"natars", "natar"}:
                _drop("natars")
                continue
            if tag and tag in exc_tags:
                _drop("excluded_alliance")
                continue
            name = (t.player_name or "").lower()
            if name and name in exc_names:
                _drop("excluded_player_name")
                continue
            if max_target_village_pop is not None and t.population > max_target_village_pop:
                _drop("target_pop_too_high")
                continue
            if min_target_village_pop is not None and t.population < min_target_village_pop:
                _drop("target_pop_too_low")
                continue
            if player_total_pops is not None and max_player_total_pop is not None:
                total = player_total_pops.get(t.player_id, 0)
                if total > max_player_total_pop:
                    _drop("player_total_pop_too_high")
                    continue
            if within_chebyshev_of:
                ok = any(
                    max(abs(t.x - cx), abs(t.y - cy)) <= r for cx, cy, r in within_chebyshev_of
                )
                if not ok:
                    _drop("outside_chebyshev_radius")
                    continue
            result.append(t)

        return result, drop_counts

    def filter_targets(
        self,
        tiles: List[MapTileInfo],
        *,
        max_population: Optional[int] = None,
        min_population: Optional[int] = None,
        exclude_coords: Optional[Set[Tuple[int, int]]] = None,
        exclude_player_ids: Optional[Set[int]] = None,
        exclude_alliance_ids: Optional[Set[int]] = None,
        only_no_player: bool = False,
        exclude_oases: bool = True,
        max_distance: Optional[float] = None,
    ) -> List[MapTileInfo]:
        """Filter scanned tiles by conditions."""
        exclude_coords = exclude_coords or set()
        exclude_player_ids = exclude_player_ids or set()
        exclude_alliance_ids = exclude_alliance_ids or set()

        result = []
        for t in tiles:
            if (t.x, t.y) in exclude_coords:
                continue
            if t.player_id and t.player_id in exclude_player_ids:
                continue
            if t.alliance_id and t.alliance_id in exclude_alliance_ids:
                continue
            if only_no_player and t.player_id:
                continue
            if exclude_oases and t.is_oasis:
                continue
            # Min/Max village-pop applies uniformly to villages AND to
            # oases. For occupied oases, scout_ws inherits the owning
            # village's population into `t.population` before this filter
            # runs, so the comparison is meaningful. Unoccupied oases keep
            # `population = 0`, which means "Min Village Pop ≥ 1" drops
            # them — that is the intended behaviour.
            if max_population is not None and t.population > max_population:
                continue
            if min_population is not None and t.population < min_population:
                continue
            if max_distance is not None and t.distance > max_distance:
                continue
            result.append(t)

        # Stealth: a pure distance sort produces an identical permutation
        # across runs with the same scan input, which is a recognizable
        # automation pattern even for a player who consistently prefers
        # nearby targets. Bucket by rounded distance, then shuffle within
        # each bucket so order varies between runs while still favoring
        # close targets first.
        result.sort(key=lambda t: t.distance)
        bucketed: List[MapTileInfo] = []
        bucket: List[MapTileInfo] = []
        last_key: Optional[int] = None
        for t in result:
            key = int(t.distance)  # 1-tile buckets
            if last_key is not None and key != last_key and bucket:
                random.shuffle(bucket)
                bucketed.extend(bucket)
                bucket = []
            bucket.append(t)
            last_key = key
        if bucket:
            random.shuffle(bucket)
            bucketed.extend(bucket)
        return bucketed

    # ── Player profile population lookup ────────────────────────────

    async def get_player_population(self, player_id: int) -> int:
        """Fetch a player's true total population from their profile page.

        Thin wrapper around :meth:`get_player_profile_info` that drops the
        capital_village_id and keeps only the integer population, so older
        callers don't have to deal with the dict shape.

        Returns 0 on failure so callers can fall back gracefully.
        """
        info = await self.get_player_profile_info(player_id)
        return info.get("pop", 0)

    async def get_player_profile_info(self, player_id: int) -> Dict[str, Any]:
        """Fetch population AND capital village id from one profile page.

        The profile page ``/profile/<player_id>`` embeds a React
        ``PlayerProfile.render(...)`` call whose ``viewData`` JSON
        contains ``"ranks":{"populationRank":N,"population":N,...}`` and a
        per-village list. The capital is marked with ``"isMainVillage":true``
        inside the village entry.

        Returns ``{"pop": int, "capital_id": int | None, "villages": list}``.
        All fields are defaulted (0 / None / []) so callers don't need
        separate error paths; the capital flag and villages list are
        best-effort and silently empty when the profile page changes shape.
        ``villages`` is parsed from the SAME page HTML (no extra request) and
        carries per-village occupied-oasis coordinates for the village-oasis
        bonus aggregation — see :func:`_extract_village_oases`.
        """
        try:
            # Profile pages are account-independent reads — route
            # through recon when active.
            page_html = await self._read_client().get_html(f"/profile/{player_id}")
        except ReconStrictViolation:
            # Strict mode breach — never silently degrade to an empty
            # profile (which would also corrupt population/capital data).
            # Propagate so the operation fails loudly.
            raise
        except Exception as exc:
            logger.warning("Failed to fetch profile for player %d: %s", player_id, exc)
            return {"pop": 0, "capital_id": None, "villages": []}

        pop = 0
        m = re.search(
            r'"ranks"\s*:\s*\{[^}]*"population"\s*:\s*(\d+)',
            page_html,
        )
        if m:
            pop = int(m.group(1))
            logger.debug("Player %d profile population: %d", player_id, pop)
        else:
            logger.warning("Could not extract population from profile for player %d", player_id)

        capital_id = _parse_capital_id_from_profile_html(page_html)
        villages = _extract_village_oases(page_html)
        return {"pop": pop, "capital_id": capital_id, "villages": villages}

    async def fetch_player_populations(
        self,
        player_ids: Set[int],
    ) -> Dict[int, int]:
        """Fetch true total populations for a set of players via their profile pages.

        Processes players sequentially to respect the global request throttler.
        Reports progress via :meth:`_report`.
        """
        result: Dict[int, int] = {}
        total = len(player_ids)
        for i, pid in enumerate(player_ids):
            pop = await self.get_player_population(pid)
            result[pid] = pop
            if (i + 1) % 5 == 0 or i == total - 1:
                self._report(f"Fetched player profiles: {i + 1}/{total}")
        return result

    async def fetch_player_profiles(
        self,
        player_ids: Set[int],
        *,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> Dict[int, Dict[str, Any]]:
        """Fetch (population + capital_id) for a set of players in one pass.

        Returns ``{player_id: {"pop": int, "capital_id": int | None}}``.

        ``progress_cb(done, total)`` fires after EVERY profile so callers can
        push a per-profile ``phase`` message to the WebSocket — without it,
        long profile-fetch phases (hundreds of players) sit silent for many
        minutes and the client WS times out before any output arrives.
        """
        result: Dict[int, Dict[str, Any]] = {}
        total = len(player_ids)
        cb_logged = False
        for i, pid in enumerate(player_ids):
            info = await self.get_player_profile_info(pid)
            result[pid] = info
            done = i + 1
            if progress_cb is not None:
                try:
                    progress_cb(done, total)
                except Exception:
                    # Progress reporting must never block the fetch loop,
                    # but a silent swallow makes broken callbacks
                    # invisible — log the first occurrence per scan so a
                    # bad callback surfaces without a per-tile flood.
                    if not cb_logged:
                        logger.warning(
                            "Profile-fetch progress_cb raised; subsequent "
                            "failures are suppressed for this scan.",
                            exc_info=True,
                        )
                        cb_logged = True
            if done % 5 == 0 or done == total:
                self._report(f"Fetched player profiles: {done}/{total}")
        return result

    # ── Scout sending ────────────────────────────────────────────────

    async def get_available_scout_count(
        self,
        tribe_id: int = 2,
        village_id: Optional[int] = None,
    ) -> int:
        """Query the rally point for how many scouts are currently available."""
        from ..constants import SCOUT_UNITS
        from ..services.military_service import MilitaryService
        from ..services.target_resolver import TargetResolver

        scout_unit = SCOUT_UNITS.get(tribe_id, "t4")
        resolver = TargetResolver(self.http_client)
        military = MilitaryService(self.http_client, resolver)
        troops = await military.get_available_troops(village_id)
        return troops.get(scout_unit, 0)

    async def send_scouts_to_targets(
        self,
        targets: List[MapTileInfo],
        scout_amount: int,
        scout_type: str = "resources",
        village_id: Optional[int] = None,
        tribe_id: int = 2,
        delay_between: float = 0.5,
        check_available: bool = False,
    ) -> List[Dict]:
        """
        Send scouts to a list of targets using the 2-step troop form.

        Args:
            targets: List of MapTileInfo targets
            scout_amount: Number of scouts per target
            scout_type: "resources" or "defenses"
            village_id: Source village ID
            tribe_id: Player tribe (1=Roman, 2=Teuton, 3=Gaul)
            delay_between: Seconds between sends to avoid rate limiting
            check_available: If True, query available scouts first and only
                             send to as many targets as scouts allow. If 0
                             scouts are available, skip entirely.
        """
        from ..constants import SCOUT_UNITS
        from ..services.military_service import MilitaryService
        from ..services.target_resolver import TargetResolver

        scout_unit = SCOUT_UNITS.get(tribe_id, "t4")
        resolver = TargetResolver(self.http_client)
        military = MilitaryService(self.http_client, resolver)

        # Check available scouts and cap targets if requested
        if check_available:
            available = await military.get_available_troops(village_id)
            scout_count = available.get(scout_unit, 0)
            self._report(f"Available scouts ({scout_unit}): {scout_count}")
            if scout_count == 0:
                self._report("No scouts available — skipping this round")
                return []
            max_targets = scout_count // scout_amount
            if max_targets < len(targets):
                self._report(
                    f"Scouts available for {max_targets}/{len(targets)} targets "
                    f"({scout_count} scouts, {scout_amount} per target)"
                )
                targets = targets[:max_targets]

        results = []
        for i, target in enumerate(targets):
            self._report(
                f"[{i + 1}/{len(targets)}] Scouting ({target.x},{target.y}) "
                f"{target.village_name or '?'} pop={target.population} dist={target.distance}"
            )
            try:
                result = await military.send_scouts(
                    x=target.x,
                    y=target.y,
                    amount=scout_amount,
                    scout_type=scout_type,
                    village_id=village_id,
                )
                # Retry once with backoff if we got a "no confirmation form" error
                # (likely rate limit or transient server issue). Jittered so the
                # retry interval isn't a fixed clockwork tell.
                if not result.success and "No confirmation form" in result.raw_response:
                    from ..stealth.timing import HumanTiming as _HT

                    backoff = _HT.micro_jitter(3.0, 0.35)
                    self._report(f"  -> Retrying after {backoff:.1f}s (possible rate limit)...")
                    await asyncio.sleep(backoff)
                    result = await military.send_scouts(
                        x=target.x,
                        y=target.y,
                        amount=scout_amount,
                        scout_type=scout_type,
                        village_id=village_id,
                    )
                status = "sent" if result.success else f"failed: {result.raw_response[:100]}"
                results.append(
                    {
                        "x": target.x,
                        "y": target.y,
                        "name": target.village_name,
                        "population": target.population,
                        "distance": target.distance,
                        "success": result.success,
                        "status": status,
                        "travel_time": result.travel_time,
                    }
                )
                if result.success:
                    self._report(f"  -> Scouts sent! Travel: {result.travel_time or '?'}")
                else:
                    self._report(f"  -> FAILED: {result.raw_response[:100]}")

            except Exception as e:
                self._report(f"  -> ERROR: {e}")
                results.append(
                    {
                        "x": target.x,
                        "y": target.y,
                        "name": target.village_name,
                        "success": False,
                        "status": f"error: {e}",
                    }
                )

            if i < len(targets) - 1:
                # Stealth: heavy-tailed delay between scout sends, mean scaled
                # by the shared session tempo so inter-scout cadence drifts
                # coherently with the rest of the session.
                from ..stealth.timing import HumanTiming

                await asyncio.sleep(HumanTiming.delay(self.http_client.tempo_scale(delay_between)))
                # Stealth: occasional noise between scouts
                try:
                    await self.http_client.noise_injector.maybe_inject_noise()
                except Exception:
                    pass

        sent = sum(1 for r in results if r["success"])
        self._report(f"Done: {sent}/{len(targets)} scouts sent successfully")
        return results

    # ── Parsing ──────────────────────────────────────────────────────

    def _parse_tile_details(self, x: int, y: int, html: str) -> MapTileInfo:
        """Parse the tile-details HTML for village info."""
        info = MapTileInfo(x=x, y=y)

        # Population
        pop_match = re.search(r"<th>\s*Population\s*</th>\s*<td>\s*(\d+)\s*</td>", html, re.DOTALL)
        if pop_match:
            info.population = int(pop_match.group(1))

        # Owner (villages) or Occupied by (occupied oases)
        player_match = re.search(
            r'<th>\s*(?:Owner|Occupied\s+by)\s*</th>\s*<td[^>]*>\s*<a\s+href="/profile/(\d+)"[^>]*>([^<]*)</a>',
            html,
            re.DOTALL,
        )
        if player_match:
            info.player_id = int(player_match.group(1))
            info.player_name = player_match.group(2).strip()

        # Tribe
        tribe_match = re.search(r"<th>\s*Tribe\s*</th>\s*<td>\s*(\w+)\s*</td>", html, re.DOTALL)
        if tribe_match:
            info.tribe = tribe_match.group(1)

        # Village name from h1. Travian wraps text with bidi-override
        # markers (&#x202d; / &#x202c;) — decode entities AND strip the
        # raw codepoints so neither leaks to the UI.
        name_match = re.search(r"<h1[^>]*>([^<]+)", html)
        if name_match:
            info.village_name = clean_unicode(html_unescape(name_match.group(1)))

        # Village ID from links (e.g., villageId=69344)
        vid_match = re.search(r"villageId=(\d+)", html)
        if vid_match:
            info.village_id = int(vid_match.group(1))

        # Distance
        dist_match = re.search(
            r"<th>\s*Distance\s*</th>\s*<td>\s*([\d.]+)\s*fields?\s*</td>",
            html,
            re.DOTALL,
        )
        if dist_match:
            info.distance = float(dist_match.group(1))

        # Alliance
        alliance_match = re.search(
            r'<th>\s*Alliance\s*</th>\s*<td[^>]*>\s*<a\s+href="/alliance/(\d+)"[^>]*>([^<]*)</a>',
            html,
            re.DOTALL,
        )
        if alliance_match:
            aid = int(alliance_match.group(1))
            if aid > 0:
                info.alliance_id = aid
                info.alliance_name = alliance_match.group(2).strip()

        # Check if oasis
        if "oasis" in html.lower() or 'class="oasis' in html:
            info.is_oasis = True

        # Oasis resource bonus(es). Travian renders these in a
        # <table id="distribution"> with one row per non-zero bonus:
        #   <td class="ico"><i class="rN" title="Crop"></i></td>
        #   <td class="val">+25%</td>
        #   <td class="desc">Wood</td>
        # We capture the same data the operator sees in the popup so
        # the scout UI can show the bonus in a dedicated column with
        # per-resource minimum and total-bucket filtering.
        #
        # Always attempt the parse rather than gating on info.is_oasis.
        # The map-scan's is_oasis (set from {k.fo}/{k.bt} title markers
        # in the map JSON) is the authoritative signal and is applied
        # by the caller AFTER this function returns; if it disagrees
        # with the html "oasis" substring check above, we'd miss the
        # bonus on a real oasis. The parser returns {} for non-oasis
        # tiles (no distribution table) so this is cheap.
        info.bonus_breakdown = _parse_oasis_bonus_breakdown(html)
        info.bonus = _format_bonus_breakdown(info.bonus_breakdown)

        # For occupied oases, the popup links to the owning village via
        # /karte.php?x=&y=. Capture those coords so the post-enrichment
        # phase can copy that village's population into the oasis row
        # (otherwise V.Pop would always be 0 for occupied oases).
        if info.is_oasis and info.player_id:
            for m in re.finditer(r"karte\.php\?x=(-?\d+)&(?:amp;)?y=(-?\d+)", html):
                ox, oy = int(m.group(1)), int(m.group(2))
                if (ox, oy) != (x, y):
                    info.oasis_owner_x = ox
                    info.oasis_owner_y = oy
                    break

        return info
