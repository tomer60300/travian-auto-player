"""Farm Builder orchestrator — scan → filter → classify → create lists → scout → assign.

Reuses:
  - auto_scout_service.scan_map / enrich_tiles / get_player_population / filter_canonical
  - farm_list_service.create_farm_list / add_slot / get_all_farm_lists
  - military_service.send_scouts
  - reports_service.fetch_village_reports / fetch_report_detail

The DEF→troop-composition table for each tribe lives here as the single source
of truth. Add new tribes by populating the corresponding entry.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from ..models.farm_list import MapTileInfo

logger = logging.getLogger(__name__)


# ─── Troop composition tables (DEF → troops) ──────────────────────────────
# Key: tribe name lowercase. Entries: (def_min, def_max, units_dict).
# Villages with DEF outside any row's range are SKIPPED with logged reason.
TROOP_COMPOSITION_TABLES: Dict[str, List[Tuple[int, int, Dict[str, int]]]] = {
    "teutons": [
        (10, 38, {"t1": 2}),
        (39, 41, {"t1": 3}),
        (42, 43, {"t1": 4}),
        (44, 46, {"t1": 5}),
        (47, 48, {"t1": 6}),
        (49, 149, {"t6": 1}),
        (150, 189, {"t1": 1, "t6": 1}),
        (190, 194, {"t1": 3, "t6": 3}),
        (195, 196, {"t1": 3, "t6": 4}),
        (197, 207, {"t1": 4, "t6": 4}),
        (208, 210, {"t1": 4, "t6": 5}),
        (211, 219, {"t1": 5, "t6": 5}),
    ],
    "romans": [],
    "gauls": [],
}

DEF_MIN = 10
DEF_MAX = 219

SLOT_LIMIT = 100  # Travian farm list cap


def lookup_troop_row(def_val: int, tribe: str) -> Optional[Dict[str, int]]:
    """Return the troop dict for a given def value, or None if out of range."""
    table = TROOP_COMPOSITION_TABLES.get(tribe.lower(), [])
    for lo, hi, units in table:
        if lo <= def_val <= hi:
            return dict(units)
    return None


def chebyshev(x1: int, y1: int, x2: int, y2: int) -> int:
    return max(abs(x1 - x2), abs(y1 - y2))


# ─── Bucket / rule evaluation ──────────────────────────────────────────────


def matrix_to_buckets(
    matrix_spec: Dict[str, Any], home_villages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Convert a matrix spec into a concrete list of buckets.

    matrix_spec shape::
        {
            "name_template": "{home_short}-{pop_label}-{radius}",
            "radius": 30,
            "pop_buckets": [                        # 1-4 user-editable
                {"label": "S", "max_pop": 120},
                {"label": "M", "max_pop": null},    # null = infinity
            ],
            "home_priority": [vid1, vid2, ...],     # tie-break order
        }

    home_villages: list of {"id", "x", "y", "short": "Village01"}

    Returns list of buckets::
        [{"name", "home_village_id", "home_short", "pop_label",
          "pop_min", "pop_max", "closer_to_home_id"}]
    """
    template = matrix_spec.get("name_template", "{home_short}-{pop_label}-{radius}")
    radius = matrix_spec.get("radius", 30)
    pop_buckets = matrix_spec.get("pop_buckets") or [
        {"label": "S", "max_pop": 120},
        {"label": "M", "max_pop": None},
    ]
    # Sort pop buckets by max_pop (None = infinity, goes last)
    pb_sorted = sorted(
        pop_buckets,
        key=lambda b: (b.get("max_pop") is None, b.get("max_pop") or 0),
    )
    # Compute (min, max) ranges from sorted max_pops (inclusive-exclusive ladder)
    ranges: List[Tuple[int, Optional[int], str]] = []
    prev = 0
    for b in pb_sorted:
        mx = b.get("max_pop")
        label = b.get("label", "?")
        ranges.append((prev + 1 if prev > 0 else 1, mx, label))
        prev = mx if mx is not None else prev

    buckets: List[Dict[str, Any]] = []
    for home in home_villages:
        for pmin, pmax, plabel in ranges:
            name = template.format(
                home_short=home.get("short", f"V{home['id']}"),
                pop_label=plabel,
                radius=radius,
                home_id=home["id"],
            )
            buckets.append(
                {
                    "name": name,
                    "home_village_id": home["id"],
                    "home_short": home.get("short", f"V{home['id']}"),
                    "pop_label": plabel,
                    "pop_min": pmin,
                    "pop_max": pmax,
                    "closer_to_home_id": home["id"],
                }
            )
    return buckets


def classify_matrix(
    tile: MapTileInfo,
    buckets: List[Dict[str, Any]],
    home_villages: List[Dict[str, Any]],
    home_priority: List[int],
) -> Optional[Dict[str, Any]]:
    """Assign a tile to the correct matrix bucket."""
    if not home_villages:
        return None
    # Find the closer_to_home_id by Chebyshev distance; ties broken by home_priority.
    priority_index = {vid: i for i, vid in enumerate(home_priority)}
    best: Optional[Tuple[int, int, int]] = None  # (dist, priority_idx, home_id)
    for home in home_villages:
        d = chebyshev(tile.x, tile.y, home["x"], home["y"])
        pi = priority_index.get(home["id"], len(home_villages))
        key = (d, pi, home["id"])
        if best is None or key < best:
            best = key
    if best is None:
        return None
    closer_id = best[2]
    pop = tile.population
    for b in buckets:
        if b["closer_to_home_id"] != closer_id:
            continue
        if pop < b["pop_min"]:
            continue
        if b["pop_max"] is not None and pop > b["pop_max"]:
            continue
        return b
    return None


def eval_advanced_rule(tile: MapTileInfo, rule: Dict[str, Any], ctx: Dict[str, Any]) -> bool:
    """Evaluate a single rule against a tile.

    rule: {"field", "op", "value"}
    ctx:  {"player_total_pops": {pid: int}, "home_villages": [...]}
    """
    field = rule.get("field")
    op = rule.get("op")
    value = rule.get("value")

    if field == "target_village_pop":
        lhs = tile.population
    elif field == "player_total_pop":
        lhs = (ctx.get("player_total_pops") or {}).get(tile.player_id, 0)
    elif field == "distance_to_home":
        # value format: {"home_id": vid, "distance": n}
        if isinstance(value, dict) and "home_id" in value and "distance" in value:
            home = next(
                (h for h in ctx.get("home_villages", []) if h["id"] == value["home_id"]), None
            )
            if home is None:
                return False
            lhs = chebyshev(tile.x, tile.y, home["x"], home["y"])
            value = value["distance"]
        else:
            return False
    elif field == "alliance_tag":
        lhs = (tile.alliance_name or "").lower()
        if isinstance(value, list):
            value = [str(v).lower() for v in value]
        elif isinstance(value, str):
            value = value.lower()
    elif field == "player_name":
        lhs = (tile.player_name or "").lower()
        if isinstance(value, list):
            value = [str(v).lower() for v in value]
        elif isinstance(value, str):
            value = value.lower()
    else:
        return False

    if op in ("=", "=="):
        return lhs == value
    if op == "!=":
        return lhs != value
    if op == "<":
        return lhs < value
    if op == "<=":
        return lhs <= value
    if op == ">":
        return lhs > value
    if op == ">=":
        return lhs >= value
    if op == "in":
        return lhs in (value or [])
    if op == "not in":
        return lhs not in (value or [])
    return False


def classify_advanced(
    tile: MapTileInfo,
    rows: List[Dict[str, Any]],
    ctx: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """First-match wins over a list of advanced rule rows."""
    for row in rows:
        rules = row.get("rules") or []
        if all(eval_advanced_rule(tile, r, ctx) for r in rules):
            return row
    return None


def find_closest_home(
    tile: MapTileInfo,
    home_villages: List[Dict[str, Any]],
    home_priority: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """Find the closest home village by Chebyshev distance. Ties broken by priority order."""
    if not home_villages:
        return None
    pri = {vid: i for i, vid in enumerate(home_priority or [])}
    best = None
    for h in home_villages:
        d = chebyshev(tile.x, tile.y, h["x"], h["y"])
        p = pri.get(h["id"], len(home_villages))
        key = (d, p, h["id"])
        if best is None or key < best:
            best = key
    if best is None:
        return None
    return next((h for h in home_villages if h["id"] == best[2]), None)


def classify_by_closest_home(
    tile: MapTileInfo,
    per_home_lists: Dict[int, List[Dict[str, Any]]],
    home_villages: List[Dict[str, Any]],
    home_priority: Optional[List[int]],
    ctx: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Assign tile to closest home village, then first-match within that home's lists."""
    # Only consider homes that have lists defined
    active_homes = [
        h for h in home_villages if h["id"] in per_home_lists and per_home_lists[h["id"]]
    ]
    closest = find_closest_home(tile, active_homes, home_priority)
    if closest is None:
        return None
    lists = per_home_lists.get(closest["id"], [])
    for lst in lists:
        rules = lst.get("rules") or []
        if not rules or all(eval_advanced_rule(tile, r, ctx) for r in rules):
            return {**lst, "home_village_id": closest["id"]}
    return None


# ─── Orchestrator ──────────────────────────────────────────────────────────


class FarmBuilderService:
    """Runs the full build pipeline, emitting phase messages via a callback.

    The send/stop callback pattern mirrors OasisRaiderService for consistency.
    """

    def __init__(self, session) -> None:
        self._session = session
        self._http = session.http_client
        self._scout_svc = session.scout_service
        self._farm_svc = session.farm_service
        self._military = session.military_service
        self._reports = session.reports_service

    # ── Helpers ──────────────────────────────────────────────────────

    async def _scan_all_home_villages(
        self,
        home_villages: List[Dict[str, Any]],
        radius: int,
        send_log: Callable[..., Awaitable[None]],
    ) -> List[MapTileInfo]:
        """Scan around each home village (with Euclidean overscan) and merge+dedupe."""
        # Chebyshev radius R → Euclidean scan radius ceil(sqrt(2) * R)
        import math as _m

        eu_radius = int(_m.ceil(_m.sqrt(2) * radius))
        seen: Dict[Tuple[int, int], MapTileInfo] = {}
        for home in home_villages:
            await send_log(
                "FB-SCAN",
                "🔍",
                f"Scanning around ({home['x']},{home['y']}) r={radius} (eu={eu_radius})...",
            )
            tiles = await self._scout_svc.scan_map(home["x"], home["y"], eu_radius)
            await send_log("FB-SCAN", "🔍", f"({home['x']},{home['y']}) raw tiles: {len(tiles)}")
            for t in tiles:
                key = (t.x, t.y)
                if key not in seen:
                    seen[key] = t
        # Filter to Chebyshev radius from at least one home
        merged: List[MapTileInfo] = []
        for t in seen.values():
            if any(chebyshev(t.x, t.y, h["x"], h["y"]) <= radius for h in home_villages):
                merged.append(t)
        await send_log("FB-SCAN", "🔍", f"Merged unique tiles (Chebyshev ≤{radius}): {len(merged)}")
        return merged

    # ── Public: preview (phases 1-2 only) ────────────────────────────

    async def preview(
        self,
        config: Dict[str, Any],
        send_log: Callable[..., Awaitable[None]],
        check_stop: Callable[[], Awaitable[bool]],
    ) -> Dict[str, Any]:
        """Scan + filter (+ classify into buckets for display). No farm list changes."""
        from .auto_scout_service import AutoScoutService

        home_villages: List[Dict[str, Any]] = config["home_villages"]
        radius: int = config["radius"]
        max_player_total_pop: int = config.get("max_player_total_pop", 220)
        exclude_alliance_tags: List[str] = config.get("exclude_alliance_tags", [])
        exclude_player_names: List[str] = config.get("exclude_player_names", [])

        await send_log(
            "FB-SCAN", "🔍", f"Preview start — {len(home_villages)} home village(s), r={radius}"
        )

        merged = await self._scan_all_home_villages(home_villages, radius, send_log)
        if await check_stop():
            return {"stopped": True}

        # Pre-filter BEFORE enrichment (drop oases/abandoned/no-player) to cut HTTP cost.
        pre = [
            t
            for t in merged
            if not t.is_oasis
            and not t.is_abandoned
            and t.player_id is not None
            and t.player_id > 0
            and t.village_id > 0
        ]
        await send_log(
            "FB-FILTER", "👥", f"Pre-filter (oasis/abandoned/no-player): {len(merged)} → {len(pre)}"
        )

        # Enrich survivors (population, player_name, alliance_name, tribe)
        await send_log("FB-FILTER", "👥", f"Enriching {len(pre)} tiles (sequential, throttled)...")
        enriched = await self._scout_svc.enrich_tiles(pre)
        if await check_stop():
            return {"stopped": True}

        # Own-village ids
        own_vids = {v.id for v in self._session.auth_state.villages}

        # Unique alive player ids → fetch true totals
        player_ids = {t.player_id for t in enriched if t.player_id}
        await send_log(
            "FB-FILTER", "👥", f"Fetching true population for {len(player_ids)} unique players..."
        )
        player_pops = await self._scout_svc.fetch_player_populations(player_ids)

        # Canonical filter
        within = [(h["x"], h["y"], radius) for h in home_villages]
        survivors, drop_counts = AutoScoutService.filter_canonical(
            enriched,
            exclude_oases=True,
            exclude_abandoned=True,
            exclude_natars_alliance_tag=True,
            exclude_own_village_ids=own_vids,
            exclude_alliance_tags_lower={t.lower() for t in exclude_alliance_tags},
            exclude_player_names_lower={n.lower() for n in exclude_player_names},
            player_total_pops=player_pops,
            max_player_total_pop=max_player_total_pop,
            within_chebyshev_of=within,
        )
        for reason, n in drop_counts.items():
            await send_log("FB-FILTER", "👥", f"[{reason}] dropped {n}")
        await send_log("FB-FILTER", "✅", f"FB-FILTER-DONE survivors: {len(survivors)}")

        # Classify into buckets
        bucket_counts: Dict[str, int] = {}
        survivor_records: List[Dict[str, Any]] = []
        home_priority = config.get("home_priority") or [h["id"] for h in home_villages]
        ctx = {"player_total_pops": player_pops, "home_villages": home_villages}

        # per_home_lists: {vid_str: [{name, rules}]} — closest-home-first mode
        raw_phl = config.get("per_home_lists") or {}
        # Convert string keys (from JSON) to int
        per_home_lists: Dict[int, List[Dict[str, Any]]] = {}
        for k, v in raw_phl.items():
            try:
                per_home_lists[int(k)] = v
            except (ValueError, TypeError):
                pass

        if per_home_lists:
            # Closest-home-first classification
            bucket_list: List[Dict[str, Any]] = []
            for vid, lists in per_home_lists.items():
                for lst in lists:
                    bucket_list.append({"name": lst["name"], "home_village_id": vid})
            for t in survivors:
                match = classify_by_closest_home(
                    t, per_home_lists, home_villages, home_priority, ctx
                )
                bname = match["name"] if match else "(unmatched)"
                bucket_counts[bname] = bucket_counts.get(bname, 0) + 1
                closest = find_closest_home(t, home_villages, home_priority)
                survivor_records.append(
                    {
                        "x": t.x,
                        "y": t.y,
                        "player_name": t.player_name,
                        "player_id": t.player_id,
                        "alliance_tag": t.alliance_name,
                        "target_village_pop": t.population,
                        "player_total_pop": player_pops.get(t.player_id, 0),
                        "assigned_bucket": bname,
                        "closest_home": closest["id"] if closest else None,
                    }
                )
        else:
            # Fallback: flat advanced_rows (legacy)
            spec_mode = config.get("spec_mode", "matrix")
            if spec_mode == "matrix":
                buckets = matrix_to_buckets(config.get("matrix_spec", {}), home_villages)
                for t in survivors:
                    b = classify_matrix(t, buckets, home_villages, home_priority)
                    bname = b["name"] if b else "(unclassified)"
                    bucket_counts[bname] = bucket_counts.get(bname, 0) + 1
                    survivor_records.append(
                        {
                            "x": t.x,
                            "y": t.y,
                            "player_name": t.player_name,
                            "player_id": t.player_id,
                            "alliance_tag": t.alliance_name,
                            "target_village_pop": t.population,
                            "player_total_pop": player_pops.get(t.player_id, 0),
                            "assigned_bucket": bname,
                        }
                    )
                bucket_list = [
                    {"name": b["name"], "home_village_id": b["home_village_id"]} for b in buckets
                ]
            else:
                rows = config.get("advanced_rows") or []
                for t in survivors:
                    row = classify_advanced(t, rows, ctx)
                    bname = row["name"] if row else "(unmatched)"
                    bucket_counts[bname] = bucket_counts.get(bname, 0) + 1
                    survivor_records.append(
                        {
                            "x": t.x,
                            "y": t.y,
                            "player_name": t.player_name,
                            "player_id": t.player_id,
                            "alliance_tag": t.alliance_name,
                            "target_village_pop": t.population,
                            "player_total_pop": player_pops.get(t.player_id, 0),
                            "assigned_bucket": bname,
                        }
                    )
                bucket_list = [
                    {"name": r["name"], "home_village_id": r.get("home_village_id")} for r in rows
                ]

        return {
            "stopped": False,
            "total_scanned": len(merged),
            "enriched": len(enriched),
            "survivors": len(survivors),
            "drop_counts": drop_counts,
            "bucket_counts": bucket_counts,
            "buckets": bucket_list,
            "records": survivor_records,
        }

    # ── Public: full run (phases 3-6) ────────────────────────────────

    async def run_full(
        self,
        config: Dict[str, Any],
        survivors: List[Dict[str, Any]],
        send_log: Callable[..., Awaitable[None]],
        check_stop: Callable[[], Awaitable[bool]],
    ) -> Dict[str, Any]:
        """Create farm lists, defense-scout every unique target, assign troops.

        `survivors` is the list returned by preview (dict records). We
        DO NOT accept raw filter params here — only a pre-filtered list —
        architecturally preventing filter-parity divergence.
        """
        t_start = time.monotonic()
        tribe_name = {1: "romans", 2: "teutons", 3: "gauls"}.get(
            self._session.tribe_id or 2, "teutons"
        )
        if tribe_name != "teutons":
            await send_log(
                "FB-ASSIGN", "❌", f"Tribe {tribe_name} not supported yet — aborting.", "error"
            )
            return {"error": f"Tribe {tribe_name} unsupported"}

        home_villages = config["home_villages"]
        _home_priority = config.get("home_priority") or [h["id"] for h in home_villages]
        spec_mode = config.get("spec_mode", "matrix")

        # ── Phase 3: re-classify using the record's pre-assigned bucket ──
        # (The frontend already knows the bucket from the preview; trust it.)
        # Build bucket → list of records map.
        bucket_records: Dict[str, List[Dict[str, Any]]] = {}
        for r in survivors:
            bname = r.get("assigned_bucket") or "(unmatched)"
            bucket_records.setdefault(bname, []).append(r)

        # Determine home_village_id per bucket name
        bucket_home: Dict[str, int] = {}
        raw_phl = config.get("per_home_lists") or {}
        if raw_phl:
            for k, lists in raw_phl.items():
                vid = int(k)
                for lst in lists or []:
                    bucket_home[lst["name"]] = vid
        elif spec_mode == "matrix":
            matrix_buckets = matrix_to_buckets(config.get("matrix_spec", {}), home_villages)
            for b in matrix_buckets:
                bucket_home[b["name"]] = b["home_village_id"]
        else:
            for row in config.get("advanced_rows") or []:
                bucket_home[row["name"]] = row["home_village_id"]

        # Skip unmatched/unclassified buckets
        real_buckets = {k: v for k, v in bucket_records.items() if k in bucket_home}
        unmatched = sum(len(v) for k, v in bucket_records.items() if k not in bucket_home)
        if unmatched:
            await send_log(
                "FB-ASSIGN", "⚠️", f"{unmatched} targets were unmatched — skipping.", "warning"
            )

        # ── Phase 4: create farm lists (primary only; overflow on demand) ─
        await send_log("FB-CREATE", "📝", f"Creating {len(real_buckets)} farm list(s)...")
        existing = await self._farm_svc.get_all_farm_lists()
        existing_names = {fl.name for fl in existing}

        # bucket_name → list of {list_id, final_name} (primary + overflows)
        bucket_lists: Dict[str, List[Dict[str, Any]]] = {}
        for bname in real_buckets:
            final = bname
            suffix = 2
            while final in existing_names:
                final = f"{bname}-{suffix}"
                suffix += 1
            lid = await self._farm_svc.create_farm_list(bucket_home[bname], final)
            existing_names.add(final)
            bucket_lists[bname] = [{"list_id": lid, "final_name": final}]
            await send_log("FB-CREATE", "📝", f"[{bname}] list_id={lid} name={final}")
            if await check_stop():
                return {"stopped": True, "bucket_lists": bucket_lists}
        await send_log("FB-CREATE", "✅", f"FB-CREATE-DONE {len(bucket_lists)} lists")

        # ── Phase 5: defense-scan every unique target ────────────────────
        # For each unique (x,y): first try existing report, else send scout + wait.
        all_targets: List[Dict[str, Any]] = []
        seen_coords: set = set()
        for bname, recs in real_buckets.items():
            for r in recs:
                key = (r["x"], r["y"])
                if key in seen_coords:
                    continue
                seen_coords.add(key)
                all_targets.append(
                    {
                        "x": r["x"],
                        "y": r["y"],
                        "bucket": bname,
                        "home_village_id": bucket_home[bname],
                    }
                )

        await send_log("FB-DEFENSE", "🛡️", f"FB-DEFENSE start: {len(all_targets)} unique coords")
        defense_data: Dict[Tuple[int, int], Dict[str, Any]] = {}
        defense_failed: Dict[Tuple[int, int], str] = {}

        # 5a: reuse existing reports
        for i, tgt in enumerate(all_targets):
            if await check_stop():
                return {"stopped": True, "bucket_lists": bucket_lists, "defense_data": defense_data}

            x, y = tgt["x"], tgt["y"]
            try:
                v = await self._reports.fetch_village_reports(x=x, y=y, fetch_details=False)
                battle = next(
                    (r for r in v.get("reports", []) if 1 <= r.get("icon_type", 0) <= 8),
                    None,
                )
                if battle:
                    rid = battle.get("report_id", "")
                    aid = battle.get("aid", "")
                    full = f"{rid}&aid={aid}" if aid else rid
                    detail = await self._reports.fetch_report_detail(full)
                    if detail and detail.get("type") == "battle":
                        d = detail.get("data")
                        if d:
                            dt = dict(d.defender_troops) if d.defender_troops else {}
                            cs = getattr(d, "defender_combat_strength", 0) or 0
                            defense_data[(x, y)] = {
                                "defender_combat_strength": cs,
                                "defender_troops": dt,
                                "source": "existing_report",
                                "report_id": rid,
                                "scouted_at": time.time(),
                            }
                            row = lookup_troop_row(cs, tribe_name)
                            row_desc = (
                                "SKIP:def_out_of_range"
                                if row is None
                                else f"{','.join(f'{k}={v}' for k, v in row.items())}"
                            )
                            await send_log(
                                "FB-DEFENSE",
                                "🛡️",
                                f"FB-DEFENSE [{i + 1}/{len(all_targets)}] ({x},{y}) EXISTING def={cs} row={row_desc}",
                            )
                            continue
            except Exception as exc:
                logger.debug("existing-report fetch failed (%s,%s): %s", x, y, exc)

        # 5b: send scouts for remaining
        remaining = [t for t in all_targets if (t["x"], t["y"]) not in defense_data]
        await send_log("FB-DEFENSE", "🛡️", f"{len(remaining)} targets need fresh scouts")

        scout_results: List[Dict[str, Any]] = []
        max_travel = 0
        for i, tgt in enumerate(remaining):
            if await check_stop():
                return {"stopped": True, "bucket_lists": bucket_lists, "defense_data": defense_data}

            x, y = tgt["x"], tgt["y"]
            success = False
            travel_s = 0
            travel_str = ""
            for attempt in range(3):
                try:
                    result = await self._military.send_scouts(
                        x=x,
                        y=y,
                        amount=1,
                        scout_type="defenses",
                        village_id=tgt["home_village_id"],
                    )
                    success = result.success
                    travel_str = result.travel_time or ""
                    if success:
                        break
                except Exception as exc:
                    logger.warning("scout send (%s,%s) attempt %d: %s", x, y, attempt + 1, exc)
                if attempt < 2:
                    await asyncio.sleep(5)
            if not success:
                defense_failed[(x, y)] = "scout_send_failed"
                await send_log("FB-DEFENSE", "⚠️", f"({x},{y}) scout_send_failed", "warning")
                continue
            # Parse travel time
            if travel_str:
                try:
                    parts = travel_str.split(":")
                    if len(parts) == 3:
                        travel_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        travel_s = int(parts[0]) * 60 + int(parts[1])
                except ValueError:
                    pass
            max_travel = max(max_travel, travel_s)
            scout_results.append({"x": x, "y": y, "travel_s": travel_s})
            await send_log("FB-DEFENSE", "🛡️", f"({x},{y}) scout sent, travel={travel_str}")

        # 5c: wait for scouts to arrive, then fetch reports
        if scout_results and not await check_stop():
            wait_s = max_travel + 15
            await send_log("FB-DEFENSE", "⏱️", f"Waiting {wait_s}s for scouts to arrive...")
            try:
                # Interruptible sleep in 10s chunks
                elapsed = 0
                while elapsed < wait_s:
                    if await check_stop():
                        break
                    chunk = min(10, wait_s - elapsed)
                    await asyncio.sleep(chunk)
                    elapsed += chunk
            except Exception:
                pass

            for i, sr in enumerate(scout_results):
                if await check_stop():
                    break

                x, y = sr["x"], sr["y"]
                got = None
                for attempt in range(3):
                    try:
                        v = await self._reports.fetch_village_reports(x=x, y=y, fetch_details=False)
                        battle = next(
                            (r for r in v.get("reports", []) if 1 <= r.get("icon_type", 0) <= 8),
                            None,
                        )
                        if battle:
                            rid = battle.get("report_id", "")
                            aid = battle.get("aid", "")
                            full = f"{rid}&aid={aid}" if aid else rid
                            detail = await self._reports.fetch_report_detail(full)
                            if detail and detail.get("type") == "battle":
                                d = detail.get("data")
                                if d:
                                    dt = dict(d.defender_troops) if d.defender_troops else {}
                                    cs = getattr(d, "defender_combat_strength", 0) or 0
                                    got = {
                                        "defender_combat_strength": cs,
                                        "defender_troops": dt,
                                        "source": "fresh_scout",
                                        "report_id": rid,
                                        "scouted_at": time.time(),
                                    }
                                    break
                    except Exception as exc:
                        logger.warning(
                            "report fetch (%s,%s) attempt %d: %s", x, y, attempt + 1, exc
                        )
                    if attempt < 2:
                        await asyncio.sleep(10)
                if got:
                    defense_data[(x, y)] = got
                    cs = got["defender_combat_strength"]
                    row = lookup_troop_row(cs, tribe_name)
                    row_desc = (
                        "SKIP:def_out_of_range"
                        if row is None
                        else f"{','.join(f'{k}={v}' for k, v in row.items())}"
                    )
                    await send_log(
                        "FB-DEFENSE", "🛡️", f"FB-DEFENSE ({x},{y}) def={cs} row={row_desc}"
                    )
                else:
                    defense_failed[(x, y)] = "report_fetch_failed"
                    await send_log("FB-DEFENSE", "⚠️", f"({x},{y}) report_fetch_failed", "warning")

        await send_log(
            "FB-DEFENSE",
            "✅",
            f"FB-DEFENSE-DONE data={len(defense_data)} failed={len(defense_failed)}",
        )

        # ── Phase 6: assign troops & add to lists ────────────────────────
        await send_log("FB-ASSIGN", "🎯", f"FB-ASSIGN start for {len(all_targets)} targets")
        per_list_count: Dict[int, int] = {}
        added: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []
        fail_add: List[Dict[str, Any]] = []

        async def _overflow_list(bname: str) -> Tuple[int, str]:
            base_home = bucket_home[bname]
            server_lists = await self._farm_svc.get_all_farm_lists()
            names = {fl.name for fl in server_lists}
            n = len(bucket_lists[bname]) + 1
            while True:
                cand = f"{bname}-{n}"
                if cand not in names:
                    break
                n += 1
            new_id = await self._farm_svc.create_farm_list(base_home, cand)
            entry = {"list_id": new_id, "final_name": cand}
            bucket_lists[bname].append(entry)
            await send_log("FB-CREATE", "📝", f"overflow created {cand} (id={new_id})")
            return new_id, cand

        def _current_list(bname: str) -> Tuple[Optional[int], Optional[str]]:
            for entry in bucket_lists[bname]:
                if per_list_count.get(entry["list_id"], 0) < SLOT_LIMIT:
                    return entry["list_id"], entry["final_name"]
            return None, None

        for bname, recs in real_buckets.items():
            for r in recs:
                if await check_stop():
                    break

                x, y = r["x"], r["y"]
                d = defense_data.get((x, y))

                if not d:
                    # No defense data → inactive placeholder (2 clubs min)
                    reason = defense_failed.get((x, y), "no_defense_data")
                    list_id, list_name = _current_list(bname)
                    if list_id is None:
                        list_id, list_name = await _overflow_list(bname)
                    units = {"t1": 2}
                    try:
                        await self._farm_svc.add_slot(list_id, x, y, units=units, active=False)
                        per_list_count[list_id] = per_list_count.get(list_id, 0) + 1
                        added.append(
                            {
                                "x": x,
                                "y": y,
                                "list_id": list_id,
                                "list_name": list_name,
                                "active": False,
                                "troops": units,
                                "def": 0,
                                "reason": reason,
                            }
                        )
                        await send_log(
                            "FB-ASSIGN",
                            "🎯",
                            f"FB-ASSIGN list={list_id} ({list_name}) ({x},{y}) INACTIVE troops={units} [{reason}]",
                        )
                    except Exception as exc:
                        err = str(exc)
                        if "errorRaidListSlotLimit" in err or "Farm list is full" in err:
                            per_list_count[list_id] = SLOT_LIMIT
                            list_id, list_name = await _overflow_list(bname)
                            try:
                                await self._farm_svc.add_slot(
                                    list_id, x, y, units=units, active=False
                                )
                                per_list_count[list_id] = per_list_count.get(list_id, 0) + 1
                                added.append(
                                    {
                                        "x": x,
                                        "y": y,
                                        "list_id": list_id,
                                        "list_name": list_name,
                                        "active": False,
                                        "troops": units,
                                        "def": 0,
                                        "reason": reason,
                                    }
                                )
                            except Exception as exc2:
                                fail_add.append(
                                    {"x": x, "y": y, "reason": f"add_slot_failed: {exc2}"}
                                )
                                await send_log(
                                    "FB-ASSIGN", "❌", f"({x},{y}) add_slot_failed: {exc2}", "error"
                                )
                        else:
                            fail_add.append({"x": x, "y": y, "reason": f"add_slot_failed: {err}"})
                            await send_log(
                                "FB-ASSIGN", "❌", f"({x},{y}) add_slot_failed: {err}", "error"
                            )
                    continue

                cs = d["defender_combat_strength"]
                if cs < DEF_MIN:
                    skipped.append({"x": x, "y": y, "reason": f"def_too_low ({cs})"})
                    await send_log(
                        "FB-ASSIGN",
                        "⚠️",
                        f"FB-ASSIGN SKIP ({x},{y}) def={cs} < {DEF_MIN}",
                        "warning",
                    )
                    continue
                if cs > DEF_MAX:
                    skipped.append({"x": x, "y": y, "reason": f"def_too_high ({cs})"})
                    await send_log(
                        "FB-ASSIGN",
                        "⚠️",
                        f"FB-ASSIGN SKIP ({x},{y}) def={cs} > {DEF_MAX}",
                        "warning",
                    )
                    continue

                row = lookup_troop_row(cs, tribe_name)
                if not row:
                    skipped.append({"x": x, "y": y, "reason": f"def_no_match ({cs})"})
                    await send_log(
                        "FB-ASSIGN", "⚠️", f"FB-ASSIGN SKIP ({x},{y}) def={cs} no_row", "warning"
                    )
                    continue

                list_id, list_name = _current_list(bname)
                if list_id is None:
                    list_id, list_name = await _overflow_list(bname)

                ok = False
                for attempt in range(3):
                    try:
                        await self._farm_svc.add_slot(list_id, x, y, units=row, active=True)
                        per_list_count[list_id] = per_list_count.get(list_id, 0) + 1
                        added.append(
                            {
                                "x": x,
                                "y": y,
                                "list_id": list_id,
                                "list_name": list_name,
                                "active": True,
                                "troops": row,
                                "def": cs,
                            }
                        )
                        await send_log(
                            "FB-ASSIGN",
                            "🎯",
                            f"FB-ASSIGN list={list_id} ({list_name}) ({x},{y}) active troops={row}",
                        )
                        ok = True
                        break
                    except Exception as exc:
                        err = str(exc)
                        if "errorRaidListSlotLimit" in err or "Farm list is full" in err:
                            per_list_count[list_id] = SLOT_LIMIT
                            list_id, list_name = await _overflow_list(bname)
                            continue
                        if attempt < 2:
                            await asyncio.sleep(5)
                        else:
                            fail_add.append({"x": x, "y": y, "reason": f"add_slot_failed: {err}"})
                            await send_log(
                                "FB-ASSIGN", "❌", f"({x},{y}) add_slot_failed: {err}", "error"
                            )
                if not ok and not fail_add or (fail_add and fail_add[-1]["x"] != x):
                    pass  # already handled above

        await send_log(
            "FB-ASSIGN",
            "✅",
            f"FB-ASSIGN-DONE added={len(added)} skipped={len(skipped)} failed={len(fail_add)}",
        )

        duration = round(time.monotonic() - t_start, 1)
        report = {
            "duration_s": duration,
            "total_targets": len(all_targets),
            "added": len(added),
            "skipped": len(skipped),
            "failed": len(fail_add),
            "defense_data_count": len(defense_data),
            "defense_failed_count": len(defense_failed),
            "bucket_lists": [
                {"bucket": bname, "lists": lists} for bname, lists in bucket_lists.items()
            ],
            "per_target": added,
            "skipped_targets": skipped,
            "failed_targets": fail_add,
        }
        return report
