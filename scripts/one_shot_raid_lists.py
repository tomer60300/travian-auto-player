"""One-shot script: build 4 raid-ready farm lists for villages (15,91) and (22,88)."""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

# Ensure src/ is on the path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from travian_api.config import Settings
from travian_api.clients.http_client import HttpClient
from travian_api.services.auth_service import AuthService
from travian_api.services.auto_scout_service import AutoScoutService
from travian_api.services.farm_list_service import FarmListService
from travian_api.services.military_service import MilitaryService
from travian_api.services.reports_service import ReportsService
from travian_api.services.target_resolver import TargetResolver

# ─── Constants ────────────────────────────────────────────────────────────────
SERVER = os.environ.get("TRAVIAN_BASE_URL", "https://ts2.x1.europe.travian.com")
USERNAME = os.environ.get("TRAVIAN_USERNAME", "")
PASSWORD = os.environ.get("TRAVIAN_PASSWORD", "")
if not USERNAME or not PASSWORD:
    raise SystemExit("Set TRAVIAN_USERNAME and TRAVIAN_PASSWORD env vars")

VILLAGE_01_COORDS = (15, 91)
VILLAGE_02_COORDS = (22, 88)

RADIUS = 30
PLAYER_TOTAL_POPULATION_MAX = 220
TARGET_VILLAGE_SMALL_CAP = 120
EXCLUDE_ALLIANCES = ["lr", "hm", "hm02"]  # pre-lowered

# Teuton verified mapping: t1=Clubswinger, t6=Teutonic Knight
# (prompt said t4 for TK — WRONG: t4=Scout, t6=TK per constants.py)
TROOP_TABLE = [
    # (min_def, max_def, t1_count, t6_count)
    (10, 38, 2, 0),
    (39, 41, 3, 0),
    (42, 43, 4, 0),
    (44, 46, 5, 0),
    (47, 48, 6, 0),
    (49, 149, 0, 1),
    (150, 189, 1, 1),
    (190, 194, 3, 3),
    (195, 196, 3, 4),
    (197, 207, 4, 4),
    (208, 210, 4, 5),
    (211, 219, 5, 5),
]

BUCKET_NAMES = [
    "Village01-S-30",
    "Village01-M-30",
    "Village02-S-30",
    "Village02-M-30",
]

CHECKPOINT_DIR = REPO_ROOT / ".one-shot"
CHECKPOINT_DIR.mkdir(exist_ok=True)

# ─── Logging ──────────────────────────────────────────────────────────────────
# Force UTF-8 on Windows to support emoji phase markers
import io
_utf8_stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(_utf8_stdout)],
)
log = logging.getLogger("one_shot")

# ─── Helpers ──────────────────────────────────────────────────────────────────

def chebyshev(x1, y1, x2, y2):
    return max(abs(x1 - x2), abs(y1 - y2))


def load_checkpoint(name):
    path = CHECKPOINT_DIR / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def save_checkpoint(name, data):
    path = CHECKPOINT_DIR / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def lookup_troop_row(def_total):
    for lo, hi, clubs, tks in TROOP_TABLE:
        if lo <= def_total <= hi:
            return {"t1": clubs, "t6": tks}
    return None


def make_units(t1=0, t6=0):
    return {
        "t1": t1, "t2": 0, "t3": 0, "t4": 0, "t5": 0,
        "t6": t6, "t7": 0, "t8": 0, "t9": 0, "t10": 0,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    run_start = time.monotonic()

    # ── Resume check ─────────────────────────────────────────────────
    findings_path = CHECKPOINT_DIR / "findings.md"
    if not findings_path.exists():
        log.error("findings.md missing — run investigation first")
        sys.exit(1)

    completed = []
    for i in range(1, 7):
        if load_checkpoint(f"phase{i}"):
            completed.append(i)
    if completed:
        log.info("RESUME: phases %s complete, starting phase %d", completed, max(completed) + 1)
    else:
        log.info("Fresh run — no checkpoints found")

    # ── Auth ──────────────────────────────────────────────────────────
    log.info("Authenticating to %s ...", SERVER)
    settings = Settings(base_url=SERVER, username=USERNAME, password=PASSWORD, stealth=True)
    http = HttpClient(settings)
    auth = AuthService(http, settings)
    auth_state = await auth.login()
    log.info("Logged in as %s (tribe=%d)", auth_state.player_name, auth_state.tribe_id)

    if auth_state.tribe_id != 2:
        log.error("Account is tribe %d, NOT Teuton (2). Troop table invalid. ABORTING.", auth_state.tribe_id)
        await http.close()
        sys.exit(1)

    # Resolve village IDs from auth_state.villages
    village_01_id = None
    village_02_id = None
    for v in auth_state.villages:
        if (v.x, v.y) == VILLAGE_01_COORDS:
            village_01_id = v.id
            log.info("Village01 (%d,%d) id=%d name=%s", v.x, v.y, v.id, v.name)
        if (v.x, v.y) == VILLAGE_02_COORDS:
            village_02_id = v.id
            log.info("Village02 (%d,%d) id=%d name=%s", v.x, v.y, v.id, v.name)

    if not village_01_id or not village_02_id:
        log.error("Could not find village IDs. Villages: %s", [(v.x, v.y, v.id) for v in auth_state.villages])
        await http.close()
        sys.exit(1)

    scout_svc = AutoScoutService(http)
    farm_svc = FarmListService(http)
    resolver = TargetResolver(http)
    military_svc = MilitaryService(http, resolver)
    reports_svc = ReportsService(http)

    # ══════════════════════════════════════════════════════════════════
    # PHASE 1 — Scan both radii
    # ══════════════════════════════════════════════════════════════════
    phase1 = load_checkpoint("phase1")
    if phase1:
        log.info("\u23ed\ufe0f  PHASE-1-SKIP (checkpoint found, %d tiles)", len(phase1))
    else:
        log.info("\U0001f50d PHASE-1-SCAN start")
        # Scan with Euclidean radius 43 to capture all Chebyshev-30 tiles
        euclidean_radius = 43

        log.info("\U0001f50d PHASE-1-SCAN scanning around %s r=%d (euclidean=%d)", VILLAGE_01_COORDS, RADIUS, euclidean_radius)
        tiles_01 = await scout_svc.scan_map(VILLAGE_01_COORDS[0], VILLAGE_01_COORDS[1], euclidean_radius)
        log.info("\U0001f50d PHASE-1-SCAN village01 raw tiles: %d", len(tiles_01))

        log.info("\U0001f50d PHASE-1-SCAN scanning around %s r=%d (euclidean=%d)", VILLAGE_02_COORDS, RADIUS, euclidean_radius)
        tiles_02 = await scout_svc.scan_map(VILLAGE_02_COORDS[0], VILLAGE_02_COORDS[1], euclidean_radius)
        log.info("\U0001f50d PHASE-1-SCAN village02 raw tiles: %d", len(tiles_02))

        # Merge and dedupe by (x, y)
        seen = {}
        for t in tiles_01 + tiles_02:
            key = (t.x, t.y)
            if key not in seen:
                seen[key] = t

        # Filter to Chebyshev radius 30 from either village
        merged = []
        for (x, y), t in seen.items():
            d1 = chebyshev(x, y, *VILLAGE_01_COORDS)
            d2 = chebyshev(x, y, *VILLAGE_02_COORDS)
            if d1 <= RADIUS or d2 <= RADIUS:
                merged.append(t)

        log.info("\U0001f50d PHASE-1-SCAN merged unique tiles in Chebyshev %d: %d", RADIUS, len(merged))

        # Pre-filter: drop oases, abandoned, and tiles with no player BEFORE expensive enrichment
        pre_filtered = [
            t for t in merged
            if not t.is_oasis
            and not t.is_abandoned
            and t.player_id is not None
            and t.player_id > 0
            and t.village_id > 0
        ]
        log.info("\U0001f50d PHASE-1-SCAN pre-filter (remove oases/abandoned/no-player): %d -> %d", len(merged), len(pre_filtered))

        # Enrich only candidate tiles to get population, player_name, alliance_name, tribe
        log.info("\U0001f50d PHASE-1-SCAN enriching %d tiles (sequential, throttled)...", len(pre_filtered))
        enriched = await scout_svc.enrich_tiles(pre_filtered)
        log.info("\U0001f50d PHASE-1-SCAN enrichment done: %d tiles", len(enriched))

        phase1 = []
        for t in enriched:
            phase1.append({
                "x": t.x, "y": t.y,
                "village_id": t.village_id,
                "player_id": t.player_id,
                "alliance_id": t.alliance_id,
                "alliance_name": t.alliance_name,
                "village_name": t.village_name,
                "player_name": t.player_name,
                "tribe": t.tribe,
                "population": t.population,
                "is_oasis": t.is_oasis,
                "is_abandoned": t.is_abandoned,
            })
        save_checkpoint("phase1", phase1)
        log.info("\u2705 PHASE-1-DONE total enriched tiles: %d", len(phase1))

    # ══════════════════════════════════════════════════════════════════
    # PHASE 2 — Apply filters
    # ══════════════════════════════════════════════════════════════════
    phase2 = load_checkpoint("phase2")
    if phase2:
        log.info("\u23ed\ufe0f  PHASE-2-SKIP (checkpoint found, %d tiles)", len(phase2))
    else:
        log.info("\U0001f465 PHASE-2-FILTER start with %d tiles", len(phase1))
        tiles = phase1[:]  # work on a copy

        # Filter 1: real player villages with population > 0
        # (oases/abandoned/no-player already removed in phase 1 pre-filter)
        before = len(tiles)
        tiles = [
            t for t in tiles
            if not t["is_oasis"]
            and not t["is_abandoned"]
            and t["player_id"] is not None
            and t["player_id"] > 0
            and t["village_id"] > 0
            and t["population"] > 0
        ]
        log.info("\U0001f465 PHASE-2-FILTER [real-player] %d -> %d (dropped %d)", before, len(tiles), before - len(tiles))

        # Filter out own villages
        own_ids = {v.id for v in auth_state.villages}
        before = len(tiles)
        tiles = [t for t in tiles if t["village_id"] not in own_ids]
        log.info("\U0001f465 PHASE-2-FILTER [own-villages] %d -> %d (dropped %d)", before, len(tiles), before - len(tiles))

        # Filter 2: alliance exclusion
        before = len(tiles)
        tiles = [
            t for t in tiles
            if (t["alliance_name"] or "").lower() not in EXCLUDE_ALLIANCES
        ]
        log.info("\U0001f465 PHASE-2-FILTER [alliance] %d -> %d (dropped %d)", before, len(tiles), before - len(tiles))

        # Filter 3: player total population <= 220
        # Collect unique player IDs
        unique_pids = {t["player_id"] for t in tiles if t["player_id"]}
        log.info("\U0001f465 PHASE-2-FILTER fetching true population for %d unique players...", len(unique_pids))
        player_pops = await scout_svc.fetch_player_populations(unique_pids)

        before = len(tiles)
        kept = []
        pop_unknown = 0
        for t in tiles:
            pid = t["player_id"]
            if not pid:
                # Abandoned village — no player, always valid target
                t["player_total_pop"] = 0
                kept.append(t)
                continue
            total_pop = player_pops.get(pid, 0)
            t["player_total_pop"] = total_pop
            if total_pop == 0 and pid in unique_pids:
                # Player exists but population fetch failed — exclude rather
                # than risk including a large player (real pop is always >= 1).
                pop_unknown += 1
                continue
            if total_pop <= PLAYER_TOTAL_POPULATION_MAX:
                kept.append(t)
        tiles = kept
        if pop_unknown:
            log.warning("\u26a0\ufe0f  PHASE-2-FILTER excluded %d tiles with unknown population (fetch failed)", pop_unknown)
        log.info("\U0001f465 PHASE-2-FILTER [player-pop<=%d] %d -> %d (dropped %d)",
                 PLAYER_TOTAL_POPULATION_MAX, before, len(tiles), before - len(tiles))

        # Re-verify Chebyshev radius (safety)
        before = len(tiles)
        tiles = [
            t for t in tiles
            if chebyshev(t["x"], t["y"], *VILLAGE_01_COORDS) <= RADIUS
            or chebyshev(t["x"], t["y"], *VILLAGE_02_COORDS) <= RADIUS
        ]
        log.info("\U0001f465 PHASE-2-FILTER [chebyshev-recheck] %d -> %d", before, len(tiles))

        phase2 = tiles
        save_checkpoint("phase2", phase2)
        log.info("\u2705 PHASE-2-DONE survivors: %d", len(phase2))

    # ══════════════════════════════════════════════════════════════════
    # PHASE 3 — Classify into 4 buckets
    # ══════════════════════════════════════════════════════════════════
    phase3 = load_checkpoint("phase3")
    if phase3:
        for bname, targets in phase3.items():
            log.info("\u23ed\ufe0f  PHASE-3-SKIP bucket %s: %d targets", bname, len(targets))
    else:
        log.info("\U0001f4e6 PHASE-3-CLASSIFY start with %d targets", len(phase2))
        buckets = {n: [] for n in BUCKET_NAMES}

        for t in phase2:
            d1 = chebyshev(t["x"], t["y"], *VILLAGE_01_COORDS)
            d2 = chebyshev(t["x"], t["y"], *VILLAGE_02_COORDS)
            # Tie breaks to Village01
            closer_home = "01" if d1 <= d2 else "02"
            size_bucket = "S" if t["population"] <= TARGET_VILLAGE_SMALL_CAP else "M"
            bucket_name = f"Village{closer_home}-{size_bucket}-30"
            buckets[bucket_name].append(t)

        for bname, targets in buckets.items():
            log.info("\U0001f4e6 PHASE-3-CLASSIFY %s: %d targets", bname, len(targets))

        phase3 = buckets
        save_checkpoint("phase3", phase3)
        log.info("\u2705 PHASE-3-DONE")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 4 — Create 4 farm lists (empty)
    # ══════════════════════════════════════════════════════════════════
    phase4 = load_checkpoint("phase4")
    if phase4:
        for bname, info in phase4.items():
            log.info("\u23ed\ufe0f  PHASE-4-SKIP %s -> list_id=%s name=%s", bname, info["list_id"], info["final_name"])
    else:
        log.info("\U0001f4dd PHASE-4-CREATE start")
        existing_lists = await farm_svc.get_all_farm_lists()
        existing_names = {fl.name for fl in existing_lists}

        home_village_map = {
            "Village01-S-30": village_01_id,
            "Village01-M-30": village_01_id,
            "Village02-S-30": village_02_id,
            "Village02-M-30": village_02_id,
        }

        phase4 = {}
        for bname in BUCKET_NAMES:
            # Find unique name
            final_name = bname
            suffix = 2
            while final_name in existing_names:
                final_name = f"{bname}-{suffix}"
                suffix += 1

            home_vid = home_village_map[bname]
            list_id = await farm_svc.create_farm_list(home_vid, final_name)
            existing_names.add(final_name)
            phase4[bname] = {"list_id": list_id, "final_name": final_name, "home_village_id": home_vid}
            log.info("\U0001f4dd PHASE-4-CREATE %s -> list_id=%d name=%s home=%d", bname, list_id, final_name, home_vid)

        save_checkpoint("phase4", phase4)
        log.info("\u2705 PHASE-4-DONE")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 5 — Defense-scan every unique target
    # ══════════════════════════════════════════════════════════════════
    phase5 = load_checkpoint("phase5") or {}
    phase5_failures = load_checkpoint("phase5_failures") or {}

    # Build flat unique coord list across all buckets
    all_coords = {}
    for bname, targets in phase3.items():
        home_vid = phase4[bname]["home_village_id"]
        for t in targets:
            key = f"{t['x']},{t['y']}"
            if key not in all_coords:
                all_coords[key] = {"x": t["x"], "y": t["y"], "home_village_id": home_vid}

    # On resume, ALL phase5 failures are retryable — scouts may have
    # returned, troops may have regenerated, network issues may be resolved.
    # Clear the failures list so they get re-processed.
    if phase5_failures:
        log.info("Phase-5 resume: clearing %d prior failures for retry", len(phase5_failures))
        phase5_failures.clear()
        save_checkpoint("phase5_failures", phase5_failures)

    already_done = set(phase5.keys()) | set(phase5_failures.keys())
    to_scout = {k: v for k, v in all_coords.items() if k not in already_done}

    if not to_scout:
        log.info("\u23ed\ufe0f  PHASE-5-SKIP all %d coords already scouted", len(all_coords))
    else:
        total_coords = len(all_coords)
        done_count = len(already_done)
        log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE start: %d total coords, %d already done, %d to scout",
                 total_coords, done_count, len(to_scout))

        # ── Step 5-PRE: reuse existing battle reports where available ─
        # For each target without cached defense data, try fetching an
        # existing battle report (from a prior raid/scout). If found,
        # parse it and save — no new scout needed.
        log.info("\U0001f6e1\ufe0f  PHASE-5-PRE checking existing reports for %d coords...", len(to_scout))
        pre_found = 0
        pre_checked_keys = list(to_scout.keys())
        for pre_idx, key in enumerate(pre_checked_keys):
            coord = to_scout[key]
            x, y = coord["x"], coord["y"]
            try:
                village_data = await reports_svc.fetch_village_reports(x=x, y=y, fetch_details=False)
                tile_reports = village_data.get("reports", [])
                battle_report = next(
                    (r for r in tile_reports if 1 <= r.get("icon_type", 0) <= 8),
                    None,
                )
                if battle_report:
                    report_id = battle_report.get("report_id", "")
                    aid = battle_report.get("aid", "")
                    rid = f"{report_id}&aid={aid}" if aid else report_id
                    detail = await reports_svc.fetch_report_detail(rid)
                    if detail and detail.get("type") == "battle":
                        battle = detail.get("data")
                        if battle:
                            defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
                            defense_data = {
                                "defender_troops": defender_troops,
                                "defender_total": sum(defender_troops.values()),
                                "defender_combat_strength": getattr(battle, "defender_combat_strength", 0) or 0,
                                "report_id": report_id,
                                "scouted_at": time.time(),
                                "source": "existing_report",
                            }
                            def_val = defense_data["defender_combat_strength"]
                            row = lookup_troop_row(def_val)
                            row_desc = f"t1={row['t1']} t6={row['t6']}" if row else "SKIP"
                            log.info("\U0001f6e1\ufe0f  PHASE-5-PRE [%d/%d] (%d,%d) EXISTING-REPORT def=%d row=%s",
                                     pre_idx + 1, len(pre_checked_keys), x, y, def_val, row_desc)
                            phase5[key] = defense_data
                            pre_found += 1
                            save_checkpoint("phase5", phase5)
                            continue
            except Exception as e:
                log.debug("PHASE-5-PRE (%d,%d) error checking existing: %s", x, y, e)

            # Progress log every 20
            if (pre_idx + 1) % 20 == 0:
                log.info("\U0001f6e1\ufe0f  PHASE-5-PRE progress %d/%d (found %d existing reports)",
                         pre_idx + 1, len(pre_checked_keys), pre_found)

        log.info("\U0001f6e1\ufe0f  PHASE-5-PRE done: %d existing reports reused", pre_found)

        # Rebuild to_scout to exclude coords now in phase5
        to_scout = {k: v for k, v in to_scout.items() if k not in phase5}
        log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE after pre-filter: %d coords still need scouting", len(to_scout))

        if not to_scout:
            log.info("\u2705 PHASE-5-DONE all targets covered by existing reports")

        # Step 5a: Send defense scouts to all pending coords
        from travian_api.models.farm_list import MapTileInfo
        scout_results = {}  # key -> {success, travel_time}
        scout_targets = list(to_scout.items())
        max_travel_seconds = 0

        for idx, (key, coord) in enumerate(scout_targets):
            x, y = coord["x"], coord["y"]
            home_vid = coord["home_village_id"]
            log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE [%d/%d] sending scout to (%d,%d) from village %d",
                     idx + 1, len(scout_targets), x, y, home_vid)

            retries = 0
            success = False
            travel_time_str = ""
            while retries < 3 and not success:
                try:
                    result = await military_svc.send_scouts(
                        x=x, y=y, amount=1,
                        scout_type="defenses",
                        village_id=home_vid,
                    )
                    success = result.success
                    travel_time_str = result.travel_time or ""
                    if success:
                        log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) scout sent, travel=%s", x, y, travel_time_str)
                    else:
                        retries += 1
                        log.warning("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) send failed (attempt %d): %s",
                                    x, y, retries, result.raw_response[:100])
                        if retries < 3:
                            await asyncio.sleep(5)
                except Exception as e:
                    retries += 1
                    log.warning("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) error (attempt %d): %s", x, y, retries, e)
                    if retries < 3:
                        await asyncio.sleep(5)

            if not success:
                log.error("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) FAILED after 3 attempts", x, y)
                phase5_failures[key] = {"reason": "scout_send_failed", "ts": time.time()}
                save_checkpoint("phase5_failures", phase5_failures)
                continue

            # Parse travel time to seconds for wait calculation
            travel_secs = 0
            if travel_time_str:
                parts = travel_time_str.split(":")
                try:
                    if len(parts) == 3:
                        travel_secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        travel_secs = int(parts[0]) * 60 + int(parts[1])
                except ValueError:
                    pass
            if travel_secs > max_travel_seconds:
                max_travel_seconds = travel_secs

            scout_results[key] = {"success": True, "travel_seconds": travel_secs, "x": x, "y": y}

            # Stealth delay between scouts (the send_scouts method has its own,
            # but add a small extra gap)
            if idx < len(scout_targets) - 1:
                await asyncio.sleep(2)

        # Step 5b: Wait for scouts to return
        # SKIP WAIT + FETCH: user will re-run later when scouts arrive.
        # Record all in-flight scouts as pending (no defense data yet).
        if scout_results:
            log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE skipping wait; %d scouts in flight (user will re-run)",
                     len(scout_results))
            for key, info in scout_results.items():
                phase5_failures.setdefault(key, {
                    "reason": "scout_in_flight_rerun_later",
                    "ts": time.time(),
                })
            save_checkpoint("phase5_failures", phase5_failures)
        # Skip remainder of phase 5 — proceed directly to phase 6
        scout_results = {}

        # Step 5c: Fetch defense reports for scouted coords
        # (no-op this run since scout_results was cleared above)
        for idx, (key, info) in enumerate(scout_results.items()):
            x, y = info["x"], info["y"]
            log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE [%d/%d] fetching report for (%d,%d)...",
                     idx + 1, len(scout_results), x, y)

            retries = 0
            defense_data = None
            while retries < 3 and defense_data is None:
                try:
                    village_data = await reports_svc.fetch_village_reports(x=x, y=y, fetch_details=False)
                    tile_reports = village_data.get("reports", [])
                    battle_report = next(
                        (r for r in tile_reports if 1 <= r.get("icon_type", 0) <= 8),
                        None,
                    )
                    if battle_report:
                        report_id = battle_report.get("report_id", "")
                        aid = battle_report.get("aid", "")
                        rid = f"{report_id}&aid={aid}" if aid else report_id
                        detail = await reports_svc.fetch_report_detail(rid)
                        if detail and detail.get("type") == "battle":
                            battle = detail.get("data")
                            if battle:
                                defender_troops = dict(battle.defender_troops) if battle.defender_troops else {}
                                defense_data = {
                                    "defender_troops": defender_troops,
                                    "defender_total": sum(defender_troops.values()),
                                    "defender_combat_strength": getattr(battle, "defender_combat_strength", 0) or 0,
                                    "report_id": report_id,
                                    "scouted_at": time.time(),
                                }
                    if defense_data is None:
                        retries += 1
                        if retries < 3:
                            log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) no battle report yet, retry %d in 10s", x, y, retries)
                            await asyncio.sleep(10)
                except Exception as e:
                    retries += 1
                    log.warning("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) fetch error (attempt %d): %s", x, y, retries, e)
                    if retries < 3:
                        await asyncio.sleep(10)

            if defense_data:
                def_val = defense_data["defender_combat_strength"]
                row = lookup_troop_row(def_val)
                row_desc = f"t1={row['t1']} t6={row['t6']}" if row else "SKIP"
                log.info("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) def=%d row=%s", x, y, def_val, row_desc)
                phase5[key] = defense_data
            else:
                log.warning("\U0001f6e1\ufe0f  PHASE-5-DEFENSE (%d,%d) no defense data after retries", x, y)
                phase5_failures[key] = {"reason": "report_fetch_failed", "ts": time.time()}

            # Save checkpoint after each coord (crash-safe)
            save_checkpoint("phase5", phase5)
            if phase5_failures:
                save_checkpoint("phase5_failures", phase5_failures)

        log.info("\u2705 PHASE-5-DONE scouted=%d, failed=%d", len(phase5), len(phase5_failures))

    # ══════════════════════════════════════════════════════════════════
    # PHASE 6-UPDATE — re-process inactive slots with newly-available data
    # For each phase6 entry that was added as inactive (no defense data
    # available at the time), check if phase5 now has real defense data.
    # If so, delete the placeholder slot and re-add with correct troops
    # as active. Do NOT create new lists — reuse existing.
    # ══════════════════════════════════════════════════════════════════
    phase6 = load_checkpoint("phase6") or []
    if phase6:
        # Build candidate updates: inactive entries where phase5 now has valid def data
        updates = []
        for idx, e in enumerate(phase6):
            if e.get("active", True):
                continue  # already active, skip
            key = f"{e['x']},{e['y']}"
            defense = phase5.get(key)
            if not defense:
                continue
            def_val = defense.get("defender_combat_strength", 0) or 0
            if def_val < 10 or def_val > 219:
                continue  # still out of range
            row = lookup_troop_row(def_val)
            if row is None:
                continue
            updates.append((idx, e, def_val, row))

        if updates:
            log.info("\U0001f504 PHASE-6-UPDATE found %d inactive slots that can be activated", len(updates))

            # Cache farm lists (one fetch per list_id) to find slot IDs
            list_cache = {}

            async def _get_list(lid):
                if lid not in list_cache:
                    list_cache[lid] = await farm_svc.get_farm_list(lid)
                return list_cache[lid]

            for idx, e, def_val, row in updates:
                x, y = e["x"], e["y"]
                lid = e["list_id"]
                try:
                    fl = await _get_list(lid)
                    slot = next((s for s in fl.slots if s.target.x == x and s.target.y == y), None)
                    if slot is None:
                        log.warning("\U0001f504 PHASE-6-UPDATE (%d,%d) slot not found in list %d", x, y, lid)
                        continue

                    # Delete the old placeholder slot
                    await farm_svc.delete_slots([slot.id])

                    # Re-add with correct troops as active
                    units = make_units(t1=row["t1"], t6=row["t6"])
                    await farm_svc.add_slot(lid, x, y, units=units, active=True)

                    # Update phase6 entry
                    phase6[idx]["troops"] = {"t1": row["t1"], "t6": row["t6"]}
                    phase6[idx]["def_total"] = def_val
                    phase6[idx]["active"] = True
                    save_checkpoint("phase6", phase6)

                    log.info("\U0001f504 PHASE-6-UPDATE list=%d (%d,%d) def=%d -> t1=%d t6=%d ACTIVATED",
                             lid, x, y, def_val, row["t1"], row["t6"])

                    # Invalidate cache for this list since slot IDs changed
                    list_cache.pop(lid, None)
                except Exception as exc:
                    log.warning("\U0001f504 PHASE-6-UPDATE (%d,%d) error: %s", x, y, str(exc)[:200])

            log.info("\u2705 PHASE-6-UPDATE-DONE processed %d updates", len(updates))
        else:
            log.info("\u23ed\ufe0f  PHASE-6-UPDATE no inactive slots to activate")

    # ══════════════════════════════════════════════════════════════════
    # PHASE 6 — Assign troops and add to lists
    # ══════════════════════════════════════════════════════════════════
    phase6 = load_checkpoint("phase6") or []
    phase6_skipped = load_checkpoint("phase6_skipped") or []
    phase6_fail = load_checkpoint("phase6_failures") or []

    # Check if already complete
    already_added = {(e["x"], e["y"]) for e in phase6}
    already_skipped = {(e["x"], e["y"]) for e in phase6_skipped}
    already_failed_add = {(e["x"], e["y"]) for e in phase6_fail}
    all_processed = already_added | already_skipped | already_failed_add

    pending_work = False
    for bname, targets in phase3.items():
        for t in targets:
            if (t["x"], t["y"]) not in all_processed:
                pending_work = True
                break
        if pending_work:
            break

    if not pending_work:
        log.info("\u23ed\ufe0f  PHASE-6-SKIP all targets already processed (added=%d skipped=%d failed=%d)",
                 len(phase6), len(phase6_skipped), len(phase6_fail))
    else:
        log.info("\U0001f3af PHASE-6-ASSIGN start")

        # Travian farm lists cap at 100 slots. When a list fills up, create
        # an overflow list with -2, -3 suffix etc. Track count per bucket
        # from existing phase6 checkpoint so resumes honor the real counts.
        SLOT_LIMIT = 100
        bucket_slot_counts: Dict[str, int] = {b: 0 for b in BUCKET_NAMES}
        # phase4 may already track overflow lists per bucket
        bucket_overflow_lists: Dict[str, List[Dict]] = {}
        for b in BUCKET_NAMES:
            info = phase4[b]
            # Primary list
            bucket_overflow_lists[b] = [{"list_id": info["list_id"], "final_name": info["final_name"]}]
            # Any overflow already created
            for ov in info.get("overflow", []):
                bucket_overflow_lists[b].append(ov)

        # Recompute current slot counts from phase6 checkpoint
        per_list_count: Dict[int, int] = {}
        for e in phase6:
            per_list_count[e["list_id"]] = per_list_count.get(e["list_id"], 0) + 1

        def _current_list_for_bucket(bname: str):
            """Return (list_id, list_name) for the current writable list in bucket."""
            lists = bucket_overflow_lists[bname]
            # Find the first one with < SLOT_LIMIT slots
            for entry in lists:
                lid = entry["list_id"]
                if per_list_count.get(lid, 0) < SLOT_LIMIT:
                    return lid, entry["final_name"]
            return None, None  # all full → need to create new

        async def _create_overflow_list(bname: str) -> tuple:
            """Create a new overflow farm list for bucket bname and register it."""
            home_vid = phase4[bname]["home_village_id"]
            existing_count = len(bucket_overflow_lists[bname])
            # Start suffix from -2 (existing list has no suffix or already has one)
            # Choose a unique name not in existing server list
            server_lists = await farm_svc.get_all_farm_lists()
            server_names = {fl.name for fl in server_lists}
            suffix = existing_count + 1
            while True:
                candidate = f"{bname}-{suffix}"
                if candidate not in server_names:
                    break
                suffix += 1
            new_id = await farm_svc.create_farm_list(home_vid, candidate)
            log.info("\U0001f4dd PHASE-6-OVERFLOW created %s -> list_id=%d home=%d", candidate, new_id, home_vid)
            entry = {"list_id": new_id, "final_name": candidate}
            bucket_overflow_lists[bname].append(entry)
            # Persist to phase4 checkpoint
            phase4[bname].setdefault("overflow", []).append(entry)
            save_checkpoint("phase4", phase4)
            return new_id, candidate

        for bname in BUCKET_NAMES:
            targets = phase3[bname]

            for t in targets:
                if (t["x"], t["y"]) in all_processed:
                    continue

                key = f"{t['x']},{t['y']}"
                defense = phase5.get(key)

                # Decide whether this slot is ACTIVE (has data → troops) or
                # INACTIVE placeholder (no data → user will re-run later).
                active = True
                row = None
                def_val = None
                skip_out_of_range = False
                skip_reason = ""

                if not defense:
                    # No defense data → add as inactive placeholder
                    # Travian API rejects zero-troop slots, so use minimum 2 clubs
                    # as a placeholder. User will re-run to update after scouts return.
                    active = False
                    row = {"t1": 2, "t6": 0}
                    reason = "no_defense_data"
                    if key in phase5_failures:
                        reason = phase5_failures[key].get("reason", "scout_failed")
                    log.info("\U0001f3af PHASE-6-ASSIGN INACTIVE (%d,%d) reason=%s", t["x"], t["y"], reason)
                else:
                    def_val = defense["defender_combat_strength"]
                    if def_val < 10:
                        skip_out_of_range = True
                        skip_reason = f"def_too_low ({def_val})"
                    elif def_val > 219:
                        skip_out_of_range = True
                        skip_reason = f"def_too_high ({def_val})"
                    else:
                        row = lookup_troop_row(def_val)

                if skip_out_of_range:
                    log.info("\U0001f3af PHASE-6-ASSIGN SKIP (%d,%d) %s", t["x"], t["y"], skip_reason)
                    phase6_skipped.append({"x": t["x"], "y": t["y"], "reason": skip_reason})
                    all_processed.add((t["x"], t["y"]))
                    save_checkpoint("phase6_skipped", phase6_skipped)
                    continue

                units = make_units(t1=row["t1"], t6=row["t6"])

                # Pick the right list (create overflow if needed)
                list_id, list_name = _current_list_for_bucket(bname)
                if list_id is None:
                    list_id, list_name = await _create_overflow_list(bname)

                retries = 0
                added = False
                while retries < 3 and not added:
                    try:
                        await farm_svc.add_slot(list_id, t["x"], t["y"], units=units, active=active)
                        added = True
                        state = "active" if active else "INACTIVE"
                        log.info("\U0001f3af PHASE-6-ASSIGN list=%d (%s) (%d,%d) %s troops={t1:%d, t6:%d}",
                                 list_id, list_name, t["x"], t["y"], state, row["t1"], row["t6"])
                    except Exception as e:
                        err_str = str(e)
                        if "errorRaidListSlotLimit" in err_str or "Farm list is full" in err_str:
                            # Current list is full — create a new overflow and retry
                            log.info("\U0001f3af PHASE-6-ASSIGN list %d full, creating overflow for %s", list_id, bname)
                            per_list_count[list_id] = SLOT_LIMIT  # mark as full
                            list_id, list_name = await _create_overflow_list(bname)
                            # Don't count this as a retry — it's a scaling operation
                            continue
                        retries += 1
                        log.warning("\U0001f3af PHASE-6-ASSIGN (%d,%d) add_slot error (attempt %d): %s",
                                    t["x"], t["y"], retries, err_str[:200])
                        if retries < 3:
                            await asyncio.sleep(5)

                if added:
                    per_list_count[list_id] = per_list_count.get(list_id, 0) + 1
                    phase6.append({
                        "x": t["x"], "y": t["y"],
                        "list_id": list_id, "list_name": list_name,
                        "troops": {"t1": row["t1"], "t6": row["t6"]},
                        "def_total": def_val if def_val is not None else 0,
                        "active": active,
                    })
                else:
                    phase6_fail.append({"x": t["x"], "y": t["y"], "reason": "add_slot_failed"})

                all_processed.add((t["x"], t["y"]))

                # Save checkpoints incrementally
                save_checkpoint("phase6", phase6)
                if phase6_skipped:
                    save_checkpoint("phase6_skipped", phase6_skipped)
                if phase6_fail:
                    save_checkpoint("phase6_failures", phase6_fail)

        log.info("\u2705 PHASE-6-DONE added=%d skipped=%d failed=%d",
                 len(phase6), len(phase6_skipped), len(phase6_fail))

    # ══════════════════════════════════════════════════════════════════
    # PHASE 7 — Final report
    # ══════════════════════════════════════════════════════════════════
    log.info("Writing final report...")
    total_duration = time.monotonic() - run_start

    # Build per-list stats (include overflow lists)
    list_stats = {}
    for bname in BUCKET_NAMES:
        info = phase4[bname]
        # Collect all list IDs for this bucket: primary + overflow
        all_list_ids = {info["list_id"]}
        for ov in info.get("overflow", []):
            all_list_ids.add(ov["list_id"])
        entries = [e for e in phase6 if e["list_id"] in all_list_ids]
        total_t1 = sum(e["troops"].get("t1", 0) for e in entries)
        total_t6 = sum(e["troops"].get("t6", 0) for e in entries)
        list_stats[bname] = {
            "list_ids": sorted(all_list_ids),
            "final_name": info["final_name"],
            "home_village_id": info["home_village_id"],
            "target_count": len(entries),
            "total_t1": total_t1,
            "total_t6": total_t6,
        }

    # Skipped breakdown
    skip_reasons = {}
    for s in phase6_skipped:
        r = s["reason"]
        skip_reasons[r] = skip_reasons.get(r, 0) + 1

    # Filter breakdown from phase 2 logs (reconstruct)
    total_scanned = len(phase1) if isinstance(phase1, list) else 0
    total_filtered = len(phase2) if isinstance(phase2, list) else 0

    report_lines = [
        "# One-Shot Raid Lists — Report",
        f"",
        f"**Generated:** {datetime.now(timezone.utc).isoformat()}",
        f"**Duration:** {total_duration:.0f}s ({total_duration/60:.1f} min)",
        f"",
        f"## Summary",
        f"",
        f"- Total tiles scanned & enriched: {total_scanned}",
        f"- Survivors after all filters: {total_filtered}",
        f"- Targets added to farm lists: {len(phase6)}",
        f"- Targets skipped: {len(phase6_skipped)}",
        f"- Targets failed to add: {len(phase6_fail)}",
        f"- Defense scout failures: {len(phase5_failures)}",
        f"",
        f"## Farm Lists",
        f"",
        f"| Bucket | List ID | Name | Home Village | Targets | Sum t1 (clubs) | Sum t6 (TK) |",
        f"|--------|---------|------|-------------|---------|---------------|------------|",
    ]
    for bname in BUCKET_NAMES:
        s = list_stats[bname]
        ids_str = ", ".join(str(lid) for lid in s["list_ids"])
        report_lines.append(
            f"| {bname} | {ids_str} | {s['final_name']} | {s['home_village_id']} | "
            f"{s['target_count']} | {s['total_t1']} | {s['total_t6']} |"
        )

    report_lines += [
        f"",
        f"## Skipped Targets",
        f"",
        f"| Reason | Count |",
        f"|--------|-------|",
    ]
    for reason, count in sorted(skip_reasons.items(), key=lambda x: -x[1]):
        report_lines.append(f"| {reason} | {count} |")

    if phase5_failures:
        report_lines += [
            f"",
            f"## Scout Failures",
            f"",
            f"| Coord | Reason |",
            f"|-------|--------|",
        ]
        for key, info in phase5_failures.items():
            report_lines.append(f"| {key} | {info.get('reason', 'unknown')} |")

    report_lines += [
        f"",
        f"## Per-Target Detail",
        f"",
        f"| Coord | List | Def Total | t1 | t6 |",
        f"|-------|------|-----------|----|----|",
    ]
    for e in sorted(phase6, key=lambda x: (x["list_name"], x["x"], x["y"])):
        report_lines.append(
            f"| ({e['x']},{e['y']}) | {e['list_name']} | {e['def_total']} | {e['troops']['t1']} | {e['troops']['t6']} |"
        )

    report_lines += [
        f"",
        f"## Next Steps",
        f"",
        f"1. Open the Travian Farm Lists UI and verify each list looks correct.",
        f"2. Spot-check a few targets — are the troop compositions sensible for the def values?",
        f"3. **Only after visual verification**, run `farm send` on each list.",
        f"4. Monitor the first raid cycle for unexpected losses.",
    ]

    report_text = "\n".join(report_lines) + "\n"
    report_path = CHECKPOINT_DIR / "report.md"
    report_path.write_text(report_text, encoding="utf-8")

    # ── Insights ──────────────────────────────────────────────────────
    insights_lines = [
        "# Insights — One-Shot Raid Lists",
        f"",
        f"**Date:** {datetime.now(timezone.utc).isoformat()}",
        f"",
        f"## Scan tile schema",
        f"- `scan_map()` returns MapTileInfo with bare data (x,y,did,uid,aid,is_oasis,is_abandoned)",
        f"- `enrich_tiles()` required for population, player_name, alliance_name, tribe",
        f"- Enrichment is sequential and throttled (~1-2 tiles/sec)",
        f"",
        f"## Player population",
        f"- `get_player_population(pid)` fetches true total from /profile/<pid> HTML",
        f"- GraphQL player.population returns 0 for other players — profile page is the only way",
        f"- {len(phase5_failures) if phase5_failures else 0} unique players in this run",
        f"",
        f"## Defense scouting",
        f"- Must send scout (military_service.send_scouts), wait travel time, then read report",
        f"- Reports fetched via reports_service.fetch_village_reports(x,y) + fetch_report_detail",
        f"- defender_total = sum(troop_counts), defender_combat_strength = game-calculated value",
        f"- Travel times within r=30 were typically a few minutes",
        f"",
        f"## Troop mapping (Teuton)",
        f"- t1=Clubswinger, t4=Scout, t6=Teutonic Knight (NOT t4 for TK as originally stated)",
        f"",
        f"## Distance",
        f"- No Chebyshev in codebase; Euclidean only. Chebyshev implemented inline: max(|dx|,|dy|)",
        f"- scan_map filters by Euclidean, so overscan (r=43) was needed to capture all Chebyshev-30 tiles",
    ]
    insights_path = CHECKPOINT_DIR / "insights.md"
    insights_path.write_text("\n".join(insights_lines) + "\n", encoding="utf-8")

    await http.close()

    report_abs = report_path.resolve()
    log.info("Report written to: %s", report_abs)
    log.info("Done. Total duration: %.0fs (%.1f min)", total_duration, total_duration / 60)


if __name__ == "__main__":
    asyncio.run(main())
