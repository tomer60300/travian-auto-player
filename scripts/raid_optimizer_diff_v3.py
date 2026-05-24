"""Raid-List Diff & Optimization v3 (Travian, Teuton, x1).

Read-only analysis that produces a prioritized diff against existing farm
lists. Optimized for one global Send-All trigger per cycle, respecting the
20,000 outgoing-troops account cap, the 100-slot per-list cap, and the
operator's stated tribe/role conventions.

Talks exclusively to the local FastAPI debug instance (default
``http://127.0.0.1:8002``); all outbound HTTP to Travian is therefore
funneled through ``src/travian_api/clients/http_client.py`` per the
stealth rules. The script never calls any write endpoint.

Outputs:
    ~/.travian/raid-optimizer/diff-{ISO_DATE}.md
    ~/.travian/raid-optimizer/diff-{ISO_DATE}.json
    ~/.travian/raid-optimizer/current-lists-{ISO_DATE}.json

and prints the markdown report to stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import math
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

# v3.4 — snapshot-history throughput tracking (Travian-bound traffic still
# only goes through clients/http_client.py; this service reads on-disk JSON only).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from travian_api.services.snapshot_history_service import (  # noqa: E402
    SlotThroughput,
    compute_throughput,
    load_recent_snapshots,
    throughput_dict,
)
from travian_api.services.target_aggregate_service import (  # noqa: E402
    TargetAggregate,
    build_target_inventory,
)
from travian_api.services.rebalance_planner import (  # noqa: E402
    DeadDecision,
    Placement,
    RebalancePlan,
    UNIT_DISPLAY_NAME as REBAL_UNIT_DISPLAY,
    VillagePosition,
    plan_rebalance,
)

# ─── Stdout UTF-8 (Windows-safe) ──────────────────────────────────────────
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

# ─── Constants ────────────────────────────────────────────────────────────
DEFAULT_API = os.environ.get("RAID_OPT_API", "http://127.0.0.1:8001")
SERVER_URL = os.environ.get("TRAVIAN_BASE_URL", "https://ts2.x1.europe.travian.com/")
# Each version's diffs go in its own subdirectory so older runs don't pollute
# the current view. Session files (.jwt, .script_creds) stay at OUT_ROOT.
VERSION = "v3.4"
OUT_ROOT = Path.home() / ".travian" / "raid-optimizer"
OUT_DIR = OUT_ROOT / VERSION
OUT_ROOT.mkdir(parents=True, exist_ok=True)
OUT_DIR.mkdir(parents=True, exist_ok=True)
ISO_DATE = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
TODAY = "2026-05-15"  # mission-stated current date
SERVER_START_DATE = datetime(2026, 3, 26)  # for day-of-server calc
HARD_TROOP_CAP = 20_000  # info-only; the 14k soft proxy was dropped in v3.1
PER_LIST_CAP = 100
SLOW_WAVE_CAP = 20
AUTO_SCOUT_CAP = 100
STALE_HOURS = 7 * 24  # >7d intel → DEACTIVATE candidate
INTEL_AGING_HOURS = 72  # >3d intel → auto-scout candidate even if not yet stale
MIN_SAMPLE_COUNT = 3
DEFENSE_HARD_LIMIT = 500
SLOW_WAVE_DEF_LIMIT = 100
MIN_SPACING_MINUTES = 10
# v3.1 r4: wave caps lifted from the mission's 3/2/1 spec. With the 20K cap
# no longer a planning concern and operator prioritizing stacking + cranny
# refill exploitation, more waves per profitable target consume more of the
# under-utilized V3 supply. The refill model still gates wave N≥2 on
# MIN_WAVE_LOOT, so a 4th/5th wave only fires if the cranny refills enough
# to justify the carry-overhead.
WAVE_CAPS = {"HIGH": 5, "MID": 3, "INACTIVE-low": 2}
BUCKET_PCTS = {"HIGH": 0.20, "MID": 0.60}  # rest go INACTIVE-low
# v3.1 — cranny refill model for wave 2+ (see Bug 2 / Methodology notes)
DEFAULT_FARM_REFILL_PER_HOUR = 60  # res/hour, all resources combined; service-level default
MIN_WAVE_LOOT = 10  # legacy reference; v3.2+ uses per-unit min_wave_loot_for_unit(uid)
ECONOMIC_FLOOR_RES = 30  # v3.3 Bug C: avg_loot below this with ≥3 samples → DEACTIVATE
# v3.4 — THROUGHPUT_DROP thresholds (raid-rate drop versus 7-14d baseline)
THROUGHPUT_DROP_THRESHOLD = -0.50      # 50% drop triggers emission
THROUGHPUT_TIER1_THRESHOLD = -0.70     # 70%+ drop → Tier 1
THROUGHPUT_MIN_SAMPLES = 3             # ≥3 recent pairs required
THROUGHPUT_RECENT_DAYS = 7             # "recent" = last 7 days
THROUGHPUT_BASELINE_DAYS = 14          # baseline pairs in (7d, 14d]
THROUGHPUT_SNAPSHOT_MAX_AGE_DAYS = 14  # don't load snapshots older than this
# v3.1 — comparator thresholds (Bug 1)
SAME_UNIT_BOOST_MULT = 1.20
SAME_UNIT_SHRINK_MULT = 0.80
OVER_ALLOC_MULT = 1.50         # existing_carry > avg_loot × 1.5 → SHRINK on existing unit
EXISTING_OK_MULT = 0.90        # existing_carry ≥ avg_loot × 0.9 → KEEP
CHANGE_UNIT_MULT = 1.30        # recommended_carry > existing_carry × 1.3 → CHANGE_UNIT
# v3.1 — tier 2 markdown cap + collapse (Bug 4)
TIER_2_VISIBLE_CAP = 50
COLLAPSE_BULK_THRESHOLD = 10

DEFENSE_PROXY = {"no_loss": 10, "some_loss": 200, "all_dead": 1000, "unknown": 0}

# Section 3 — Per-village planning budget (operator-authoritative supply).
# V4/V5/V6/V7 carry 100 Clubs each by operator design — micro-raider role
# (local cranny grabs during Start All cycles); see LOCAL_VILLAGES below.
BUDGET: dict[str, dict[str, int]] = {
    "V1": {"coords": (15, 91), "t1": 689,  "t2": 2,   "t3": 102,  "t4": 81, "t5": 35,  "t6": 92,   "t7": 0,   "t8": 0},
    "V2": {"coords": (22, 88), "t1": 258,  "t2": 104, "t3": 0,    "t4": 3,  "t5": 111, "t6": 49,   "t7": 0,   "t8": 0},
    "V3": {"coords": (42, 17), "t1": 2332, "t2": 0,   "t3": 2024, "t4": 1,  "t5": 0,   "t6": 2506, "t7": 165, "t8": 140},
    "V4": {"coords": (39, 87), "t1": 100,  "t2": 0,   "t3": 0,    "t4": 2,  "t5": 0,   "t6": 0,    "t7": 0,   "t8": 0},
    "V5": {"coords": (45, 90), "t1": 100,  "t2": 0,   "t3": 0,    "t4": 0,  "t5": 0,   "t6": 0,    "t7": 0,   "t8": 0},
    "V6": {"coords": (33, 83), "t1": 100,  "t2": 0,   "t3": 0,    "t4": 12, "t5": 0,   "t6": 0,    "t7": 0,   "t8": 0},
    "V7": {"coords": (30, 82), "t1": 100,  "t2": 0,   "t3": 0,    "t4": 0,  "t5": 0,   "t6": 0,    "t7": 0,   "t8": 0},
}
SOURCE_VILLAGES = ("V1", "V2", "V3", "V4", "V5", "V6", "V7")

# Live-vs-BUDGET diagnostic: warn when a (village, unit) supply has dropped
# below this fraction of its operator-stated ceiling. Informational only;
# BUDGET is the planning authority. See log_live_vs_budget().
LIVE_VS_BUDGET_WARN_FRACTION = 0.30

# Teuton unit profiles. t1=Clubs, t2=Spear, t3=Axe, t4=Scout, t5=Paladin,
# t6=TK, t7=Ram, t8=Catapult. Carry/speed/attack from raid_analyzer_service
# constants and game wiki.
UNIT_PROFILES: dict[str, dict[str, Any]] = {
    "t1": {"name": "Clubs",    "carry": 60,  "speed": 7,  "attack": 40,  "role": "strike-bulk"},
    "t2": {"name": "Spear",    "carry": 40,  "speed": 7,  "attack": 10,  "role": "defense-only"},
    "t3": {"name": "Axe",      "carry": 50,  "speed": 6,  "attack": 60,  "role": "strike-bulk"},
    "t4": {"name": "Scout",    "carry": 0,   "speed": 9,  "attack": 0,   "role": "scout-only"},
    "t5": {"name": "Paladin",  "carry": 110, "speed": 10, "attack": 50,  "role": "strike-premium"},
    "t6": {"name": "TK",       "carry": 80,  "speed": 9,  "attack": 150, "role": "strike-premium"},
    "t7": {"name": "Ram",      "carry": 0,   "speed": 4,  "attack": 65,  "role": "slow-wave"},
    "t8": {"name": "Catapult", "carry": 0,   "speed": 3,  "attack": 50,  "role": "slow-wave"},
}

# Per-village preferred strike units. Driven by Section 3 unit-by-unit table.
STRIKE_UNITS_BY_VILLAGE: dict[str, list[str]] = {
    "V1": ["t1", "t6"],          # Clubs bulk, TK premium
    "V2": ["t1", "t5"],          # Clubs bulk, Paladin premium
    "V3": ["t3", "t6"],          # Axe bulk, TK premium
    "V4": ["t1"],                # Clubs only (LOCAL raider)
    "V5": ["t1"],                # Clubs only (LOCAL raider)
    "V6": ["t1"],                # Clubs only (LOCAL raider)
    "V7": ["t1"],                # Clubs only (LOCAL raider)
}
PREMIUM_UNITS = {"t5", "t6"}

# Section 5 / Step F: slow-wave companions (mixed-unit slots).
SLOW_WAVE_AXE_COUNT = 3  # 3 Axes + 1 Ram (or 3 Axes + 1 Cata) per slot

# ─── Helpers ──────────────────────────────────────────────────────────────


def travian_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Euclidean distance on a 401×401 wrap-around map."""
    dx = abs(x1 - x2)
    dy = abs(y1 - y2)
    if dx > 200:
        dx = 401 - dx
    if dy > 200:
        dy = 401 - dy
    return math.sqrt(dx * dx + dy * dy)


def user_env(name: str) -> str:
    """Read a Windows User-scope env var via PowerShell (works without re-launching the shell)."""
    if sys.platform != "win32":
        return os.environ.get(name, "")
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
             f"[Environment]::GetEnvironmentVariable('{name}','User')"],
            capture_output=True, text=True, timeout=10,
        )
        return (out.stdout or "").strip()
    except Exception:
        return os.environ.get(name, "")


def day_of_server() -> int:
    return (datetime.now() - SERVER_START_DATE).days


def fmt_pct(x: float) -> str:
    return f"{x*100:.0f}%"


# ─── API client ───────────────────────────────────────────────────────────


class ApiClient:
    """Thin async wrapper around the FastAPI debug instance."""

    def __init__(self, base: str, token: str | None = None):
        self.base = base.rstrip("/")
        self.token = token
        self.client = httpx.AsyncClient(timeout=120.0)
        self.failures: dict[str, int] = defaultdict(int)

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def aclose(self) -> None:
        await self.client.aclose()

    async def get(self, path: str, **params: Any) -> Any:
        url = f"{self.base}{path}"
        try:
            r = await self.client.get(url, headers=self.auth_headers(), params=params or None)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            self.failures[f"GET {path}"] += 1
            raise

    async def post(self, path: str, body: dict | None = None) -> Any:
        url = f"{self.base}{path}"
        try:
            r = await self.client.post(url, headers=self.auth_headers(), json=body or {})
            r.raise_for_status()
            return r.json()
        except Exception:
            self.failures[f"POST {path}"] += 1
            raise


async def ensure_jwt(api: ApiClient) -> str:
    """Use cached JWT if present and valid, otherwise login/register the script user.

    Session files live at OUT_ROOT (not per-version) so re-runs across versions
    share auth state.
    """
    jwt_file = OUT_ROOT / ".jwt"
    creds_file = OUT_ROOT / ".script_creds"

    # Try cached JWT
    if jwt_file.exists():
        api.token = jwt_file.read_text().strip()
        try:
            r = await api.client.get(f"{api.base}/api/users/me", headers=api.auth_headers())
            if r.status_code == 200:
                return api.token
        except Exception:
            pass

    # Fall back to login (registering if no creds file)
    if creds_file.exists():
        lines = creds_file.read_text().splitlines()
        username, password = lines[0].strip(), lines[1].strip()
    else:
        import secrets
        username = "raid_optimizer_v3"
        password = secrets.token_urlsafe(16)
        creds_file.write_text(f"{username}\n{password}\n")

    # Try login first
    r = await api.client.post(
        f"{api.base}/api/users/login",
        json={"username": username, "password": password},
    )
    if r.status_code != 200:
        # Register
        r2 = await api.client.post(
            f"{api.base}/api/users/register",
            json={"username": username, "password": password},
        )
        if r2.status_code not in (200, 201):
            raise RuntimeError(f"Register failed: {r2.status_code} {r2.text}")
        token = r2.json()["access_token"]
    else:
        token = r.json()["access_token"]

    jwt_file.write_text(token)
    api.token = token
    return token


async def ensure_travian_session(api: ApiClient) -> dict[str, Any]:
    """Connect to Travian if not already connected. Returns status payload."""
    status = await api.get("/api/travian/status")
    if status.get("connected"):
        return status

    tu = user_env("TRAVIAN_USERNAME") or os.environ.get("TRAVIAN_USERNAME", "")
    tp = user_env("TRAVIAN_PASS") or os.environ.get("TRAVIAN_PASS", "")
    if not tu or not tp:
        raise RuntimeError("TRAVIAN_USERNAME/TRAVIAN_PASS not set in env")

    return await api.post(
        "/api/travian/connect",
        {"server_url": SERVER_URL, "username": tu, "password": tp},
    )


# ─── Step A — Inventory ───────────────────────────────────────────────────


async def fetch_villages(api: ApiClient) -> dict[str, Any]:
    """Build owner-village-id map keyed by name and coords."""
    payload = await api.get("/api/villages")
    villages = payload.get("villages", [])
    by_name: dict[str, dict[str, Any]] = {}
    by_coords: dict[tuple[int, int], dict[str, Any]] = {}
    for v in villages:
        by_name[v["name"]] = v
        by_coords[(v["x"], v["y"])] = v
    return {"all": villages, "by_name": by_name, "by_coords": by_coords}


def vid_for(label: str, villages: dict[str, Any]) -> int | None:
    """Resolve a logical name (V1/V2/..) to an owner_village_id via coords."""
    coords = BUDGET[label]["coords"]
    v = villages["by_coords"].get(coords)
    return v["id"] if v else None


async def fetch_live_troops(api: ApiClient, villages: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Get available (idle) troops per village. ``village_id`` accepted as query param."""
    out: dict[str, dict[str, int]] = {}
    for label in BUDGET:
        coords = BUDGET[label]["coords"]
        v = villages["by_coords"].get(coords)
        if v is None:
            out[label] = {"error": "village not found in account"}
            continue
        try:
            payload = await api.get("/api/military/troops", village_id=v["id"])
        except Exception as exc:
            out[label] = {"error": f"fetch failed: {exc}"}
            continue
        # Normalize: payload likely a dict like {"t1": N, ...}. Guard against shape variance.
        if isinstance(payload, dict):
            normalized: dict[str, int] = {}
            for k, val in payload.items():
                key = k.lower()
                if isinstance(val, int):
                    normalized[key] = val
                elif isinstance(val, dict) and "available" in val:
                    normalized[key] = int(val.get("available", 0))
            out[label] = normalized or {"raw": payload}  # type: ignore[dict-item]
        else:
            out[label] = {"raw": payload}  # type: ignore[dict-item]
    return out


def log_live_vs_budget(live_troops: dict[str, dict[str, int]]) -> None:
    """Print a per-(village, unit) BUDGET vs live diff and WARN on supply gaps.

    BUDGET is operator-authoritative (intended supply ceiling); live can drift
    below — usually because troops are in transit at fetch time. Cells below
    LIVE_VS_BUDGET_WARN_FRACTION emit a WARN to stderr. Read-only diagnostic;
    never adjusts BUDGET.
    """
    print("BUDGET vs live troops diagnostic:", file=sys.stderr)
    print(f"  {'village':>4}  {'unit':>6}  {'budget':>6}  {'live':>6}  {'delta%':>7}", file=sys.stderr)
    for label, cfg in BUDGET.items():
        live = live_troops.get(label) or {}
        for unit in ("t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"):
            budget_n = int(cfg.get(unit, 0) or 0)
            if budget_n <= 0:
                continue
            raw = live.get(unit, 0)
            live_n = int(raw) if isinstance(raw, int) else 0
            ratio = live_n / budget_n
            delta_pct = (ratio - 1.0) * 100.0
            warn = "  ⚠ WARN" if ratio < LIVE_VS_BUDGET_WARN_FRACTION else ""
            print(
                f"  {label:>4}  {unit:>6}  {budget_n:>6}  {live_n:>6}  {delta_pct:>6.1f}%{warn}",
                file=sys.stderr,
            )
            if ratio < LIVE_VS_BUDGET_WARN_FRACTION:
                print(
                    f"  WARN: live supply below {int(LIVE_VS_BUDGET_WARN_FRACTION*100)}% "
                    f"of planned for ({label}, {unit}): live={live_n} budget={budget_n}",
                    file=sys.stderr,
                )


async def fetch_all_lists(api: ApiClient) -> list[dict[str, Any]]:
    """GET /api/farm/lists summaries."""
    return await api.get("/api/farm/lists")


async def fetch_list_details(api: ApiClient, list_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Fetch /api/farm/lists/{id} sequentially (stealth-friendly)."""
    out: dict[int, dict[str, Any]] = {}
    for lid in list_ids:
        try:
            out[lid] = await api.get(f"/api/farm/lists/{lid}")
        except Exception:
            continue
    return out


# ─── Step B — Per-slot empirical metadata ─────────────────────────────────


def slot_metadata(slot: dict[str, Any], list_id: int, list_name: str, owner_vid: int | None) -> dict[str, Any]:
    last_raid = slot.get("last_raid") or {}
    icon = last_raid.get("icon", "unknown")
    total_raids = int(slot.get("total_raids") or 0)
    total_booty = int(slot.get("total_booty") or 0)
    avg_loot = (total_booty / total_raids) if total_raids > 0 else 0.0
    raid_time = last_raid.get("time")
    last_raid_dt = datetime.fromtimestamp(raid_time, tz=timezone.utc) if raid_time else None
    age_hours = ((datetime.now(timezone.utc) - last_raid_dt).total_seconds() / 3600.0) if last_raid_dt else None

    return {
        "list_id": list_id,
        "list_name": list_name,
        "owner_village_id": owner_vid,
        "slot_id": slot["id"],
        "coords": (slot["x"], slot["y"]),
        "name": slot.get("name", ""),
        "population": slot.get("population", 0),
        "is_active": bool(slot.get("is_active")),
        "is_running": bool(slot.get("is_running")),
        "running_attacks": int(slot.get("running_attacks") or 0),
        "distance_from_owner": float(slot.get("distance") or 0.0),
        "troops": dict(slot.get("troops") or {}),
        "troop_total": int(slot.get("troop_total") or 0),
        "last_raid_icon": icon,
        "last_raid_resources": last_raid.get("resources"),
        "last_raid_capacity": last_raid.get("capacity"),
        "last_raid_time": raid_time,
        "last_raid_age_h": age_hours,
        "total_raids": total_raids,
        "total_booty": total_booty,
        "avg_loot": round(avg_loot, 2),
        "defense_proxy": DEFENSE_PROXY.get(icon, 0),
        "sample_count": total_raids,
        "pushing_protection_suspect": (total_raids >= 3 and total_booty == 0),
    }


# ─── v3.1 helpers ─────────────────────────────────────────────────────────


def slot_primary_unit(troops: dict[str, int]) -> str | None:
    """Return the unit_id with the highest count in the slot, or None if empty."""
    nonzero = {uid: int(n) for uid, n in (troops or {}).items() if int(n) > 0}
    if not nonzero:
        return None
    return max(nonzero, key=nonzero.get)


def total_carry(troops: dict[str, int]) -> int:
    """Sum carry capacity across all units in a slot."""
    total = 0
    for uid, n in (troops or {}).items():
        profile = UNIT_PROFILES.get(uid)
        if profile:
            total += int(n) * int(profile["carry"])
    return total


def expected_loot_for_wave_n(
    avg_loot: float,
    wave_index: int,
    this_arrival_min: float,
    prior_arrival_min: float | None,
    prior_wave_carry_taken: int = 0,
    refill_per_hour: float = DEFAULT_FARM_REFILL_PER_HOUR,
) -> int:
    """Cranny-refill loot estimate with residual carry-over (v3.2, Bug 2).

    Wave 1 hauls the empirical avg_loot (cranny state on first arrival).
    Wave N (N >= 2) hauls:
        min(residual_after_prior + refill_during_interval, empirical_cranny_size)

    where:
        empirical_cranny_size ≈ avg_loot
        residual_after_prior  = max(0, avg_loot - prior_wave_carry_taken)
        refill_during_interval = (this_arrival - prior_arrival) hours × refill_rate

    The residual term matters when wave N-1 under-carried (its carry was below the
    cranny size). In that case wave N inherits the leftover plus the refill,
    so wave 2's haul approaches wave 1's haul instead of being a tiny top-up.
    """
    if wave_index <= 1 or prior_arrival_min is None:
        return int(round(avg_loot))
    interval_h = max(0.0, (this_arrival_min - prior_arrival_min) / 60.0)
    refilled = interval_h * refill_per_hour
    residual = max(0.0, avg_loot - max(0, prior_wave_carry_taken))
    available = min(residual + refilled, avg_loot)
    return int(round(max(0.0, available)))


def min_wave_loot_for_unit(unit_id: str) -> float:
    """v3.2 (Bug 2) — feasibility threshold = 50% of one full-carry unit.

    Below this, the wave's round-trip cost (troops out of rotation) exceeds the
    marginal loot returned. Per-unit so Clubs (carry 60) gets a 30-res threshold
    while TK (carry 80) gets 40 res.
    """
    profile = UNIT_PROFILES.get(unit_id, {})
    carry = profile.get("carry", 0)
    return max(20.0, carry * 0.5)


# ─── Step C — Scoring & bucketing ─────────────────────────────────────────


def score_for_unit(slot_m: dict[str, Any], source: str, unit_id: str) -> float:
    """Score a slot for raiding by (source_village, unit_id).

    Formula (documented in Methodology):
      base = expected_loot / round_trip_hours
      expected_loot = min(carry * troops_for_full_carry, eff_avg_loot)
      eff_avg_loot = avg_loot * confidence
      confidence = min(1, sample_count / 10)
      defense_penalty = 1 / (1 + defense_proxy / 100)
      score = base * defense_penalty
    """
    if slot_m["avg_loot"] <= 0:
        return 0.0
    if slot_m["sample_count"] < MIN_SAMPLE_COUNT:
        return 0.0
    profile = UNIT_PROFILES[unit_id]
    if profile["role"] not in ("strike-bulk", "strike-premium"):
        return 0.0
    if profile["carry"] <= 0:
        return 0.0
    src_x, src_y = BUDGET[source]["coords"]
    tx, ty = slot_m["coords"]
    d = travian_distance(src_x, src_y, tx, ty)
    if d <= 0:
        return 0.0
    round_trip_h = 2 * d / profile["speed"]
    if round_trip_h <= 0:
        return 0.0
    confidence = min(1.0, slot_m["sample_count"] / 10.0)
    eff_avg = slot_m["avg_loot"] * confidence
    troops_for_full = max(1, math.ceil(eff_avg / profile["carry"]))
    expected_loot = min(profile["carry"] * troops_for_full, eff_avg)
    defense_penalty = 1.0 / (1.0 + slot_m["defense_proxy"] / 100.0)
    return round((expected_loot / round_trip_h) * defense_penalty, 3)


def best_score(slot_m: dict[str, Any]) -> tuple[float, str | None, str | None]:
    """Return (best_score, best_source, best_unit) across all viable (source, unit) pairs."""
    best = (0.0, None, None)
    for source in SOURCE_VILLAGES:
        for unit in STRIKE_UNITS_BY_VILLAGE.get(source, []):
            s = score_for_unit(slot_m, source, unit)
            if s > best[0]:
                best = (s, source, unit)
    return best


def is_deactivated(slot_m: dict[str, Any]) -> tuple[bool, str]:
    if slot_m["defense_proxy"] > DEFENSE_HARD_LIMIT:
        return True, "def_over_500"
    if slot_m["last_raid_age_h"] is not None and slot_m["last_raid_age_h"] > STALE_HOURS:
        return True, "stale_over_7d"
    if slot_m["sample_count"] < MIN_SAMPLE_COUNT:
        return True, "insufficient_history"
    if slot_m["avg_loot"] <= 0 and not slot_m["pushing_protection_suspect"]:
        return True, "depleted"
    if slot_m["sample_count"] == 0:
        return True, "never_raided"
    # v3.3 Bug C — economic floor. With ≥3 samples and a sustained tiny avg_loot,
    # even a 1-unit raid is uneconomic (round-trip troop-time cost exceeds haul).
    # Threshold is service-level (documented in Methodology).
    if slot_m["sample_count"] >= MIN_SAMPLE_COUNT and slot_m["avg_loot"] < ECONOMIC_FLOOR_RES:
        return True, "below_economic_floor"
    return False, ""


def bucket_slots(slot_ms: list[dict[str, Any]]) -> None:
    """Assign 'bucket' field to each slot: HIGH/MID/INACTIVE-low/DEACTIVATED."""
    active: list[dict[str, Any]] = []
    for sm in slot_ms:
        deact, reason = is_deactivated(sm)
        if deact:
            sm["bucket"] = "DEACTIVATED"
            sm["deactivation_reason"] = reason
            sm["canonical_score"] = 0.0
            sm["best_source"] = None
            sm["best_unit"] = None
        else:
            score, src, unit = best_score(sm)
            sm["canonical_score"] = score
            sm["best_source"] = src
            sm["best_unit"] = unit
            if score <= 0:
                sm["bucket"] = "DEACTIVATED"
                sm["deactivation_reason"] = "unscorable"
            else:
                active.append(sm)

    active.sort(key=lambda x: x["canonical_score"], reverse=True)
    n = len(active)
    high_cut = max(1, int(round(n * BUCKET_PCTS["HIGH"]))) if n > 0 else 0
    mid_cut = high_cut + int(round(n * BUCKET_PCTS["MID"])) if n > 0 else 0
    for i, sm in enumerate(active):
        if i < high_cut:
            sm["bucket"] = "HIGH"
        elif i < mid_cut:
            sm["bucket"] = "MID"
        else:
            sm["bucket"] = "INACTIVE-low"


# ─── Step D — Strike-wave planning ────────────────────────────────────────


def waves_per_slot_default(bucket: str) -> int:
    return WAVE_CAPS.get(bucket, 1)


def troops_for_loot(loot: float, unit_id: str) -> int:
    """Smallest n where n × carry ≥ loot, with n ≥ 1."""
    profile = UNIT_PROFILES[unit_id]
    if profile["carry"] <= 0:
        return 0
    if loot <= 0:
        return 1
    return max(1, math.ceil(loot / profile["carry"]))


def plan_strike_waves(
    slot_ms: list[dict[str, Any]],
    village_label_by_vid: dict[int, str],
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """v3.3 — Cranny-refill wave planner with owner-village constraint (Bug A).

    Each slot belongs to a list bound to ONE owner village. Travian can only
    fire that village's troops from that list. So each slot's wave plan is
    restricted to its owner village.

    Returns ``(waves_by_v, cross_village_recommendations)``:
      - ``waves_by_v[V] = [waves...]`` — wave plans where every wave's source
        equals the slot's owner village ``V``. Operator-executable.
      - ``cross_village_recommendations`` — list of dicts describing top-2
        cross-village stacking opportunities per slot. These become ADD_NEW_SLOT
        diff actions: "add this coord to a V_other-owned list to enable a
        same-target wave from V_other."
    """
    waves_by_v: dict[str, list[dict[str, Any]]] = {v: [] for v in SOURCE_VILLAGES}
    cross_village_recommendations: list[dict[str, Any]] = []
    remaining: dict[tuple[str, str], int] = {}
    for label, vals in BUDGET.items():
        if label not in SOURCE_VILLAGES:
            continue
        for unit, count in vals.items():
            if unit in ("coords",):
                continue
            remaining[(label, unit)] = int(count)

    eligible = [sm for sm in slot_ms if sm["bucket"] in ("HIGH", "MID", "INACTIVE-low")]
    eligible.sort(key=lambda x: x["canonical_score"], reverse=True)

    def _enumerate_candidates_for_village(sm_local, source_label):
        """Build candidate (source, unit) wave entries for a single village
        sending to a single target slot."""
        tx_local, ty_local = sm_local["coords"]
        base_avg = sm_local["avg_loot"]
        out: list[dict[str, Any]] = []
        for unit in STRIKE_UNITS_BY_VILLAGE.get(source_label, []):
            profile = UNIT_PROFILES[unit]
            if profile["carry"] <= 0:
                continue
            if remaining.get((source_label, unit), 0) <= 0:
                continue
            src_x, src_y = BUDGET[source_label]["coords"]
            d = travian_distance(src_x, src_y, tx_local, ty_local)
            if d <= 0:
                continue
            arrival_min = d / profile["speed"] * 60.0
            round_trip_h = 2 * d / profile["speed"]
            base_loot = min(profile["carry"] * troops_for_loot(base_avg, unit), base_avg)
            base_efficiency = base_loot / max(round_trip_h, 0.01)
            out.append({
                "source": source_label,
                "unit": unit,
                "distance": d,
                "arrival_min": arrival_min,
                "round_trip_h": round_trip_h,
                "round_trip_min": int(round(2 * arrival_min)),
                "base_efficiency": base_efficiency,
            })
        return out

    for sm in eligible:
        bucket = sm["bucket"]
        max_waves = waves_per_slot_default(bucket)
        base_avg_loot = sm["avg_loot"]
        if base_avg_loot <= 0:
            continue

        # v3.3 Bug A — restrict wave-source to the slot's list-owner village.
        owner_label = village_label_by_vid.get(sm.get("owner_village_id") or -1)
        if owner_label not in SOURCE_VILLAGES:
            # Slot has no usable owner village (V5 or unmapped) — skip.
            continue

        # Owner-village candidates only.
        candidates = _enumerate_candidates_for_village(sm, owner_label)
        if not candidates:
            continue

        # Greedy wave selection: build the wave plan in arrival-time order,
        # but prioritize next candidates by *refill-adjusted* efficiency.
        # Wave 1 = highest base efficiency. Each subsequent wave: pick the
        # remaining candidate whose expected_loot / round_trip is best given
        # the prior arrival time(s), respecting the 10-min spacing rule.
        candidates.sort(key=lambda c: c["base_efficiency"], reverse=True)
        selected: list[dict[str, Any]] = []

        for wave_slot in range(max_waves):
            is_first = (wave_slot == 0)
            best_pick = None
            best_score_local = 0.0
            for cand in candidates:
                if cand in selected:
                    continue
                if remaining.get((cand["source"], cand["unit"]), 0) <= 0:
                    continue
                arrival = cand["arrival_min"]
                # 10-min spacing on the same target
                if any(abs(arrival - s["arrival_min"]) < MIN_SPACING_MINUTES for s in selected):
                    continue
                # Determine prior wave (the latest already-selected wave that lands
                # before this one). Used by the refill model — including its carry,
                # so wave 2 inherits residual when wave 1 under-carries.
                priors = sorted(
                    (s for s in selected if s["arrival_min"] < arrival),
                    key=lambda s: s["arrival_min"],
                )
                prior_arrival = priors[-1]["arrival_min"] if priors else None
                prior_carry_taken = (
                    priors[-1]["count"] * UNIT_PROFILES[priors[-1]["unit"]]["carry"]
                    if priors else 0
                )
                wave_index = len(priors) + 1
                expected = expected_loot_for_wave_n(
                    base_avg_loot,
                    wave_index,
                    this_arrival_min=arrival,
                    prior_arrival_min=prior_arrival,
                    prior_wave_carry_taken=prior_carry_taken,
                )
                # Wave 1 always allowed (avg_loot > 0 already checked). Wave 2+
                # enforces the per-unit feasibility threshold (carry × 0.5).
                if not is_first and expected < min_wave_loot_for_unit(cand["unit"]):
                    continue
                # Pick a tentative troop count for scoring
                n_tentative = troops_for_loot(expected, cand["unit"])
                supply_left = remaining.get((cand["source"], cand["unit"]), 0)
                n_tentative = min(n_tentative, supply_left)
                if n_tentative <= 0:
                    continue
                # Refill-adjusted efficiency. Abundance bonus removed in v3.3:
                # owner-village constraint already limits the candidate pool to a
                # single village, so cross-village load spreading is now handled
                # by cross-village ADD_NEW_SLOT recommendations instead.
                eff = expected / max(cand["round_trip_h"], 0.01)
                if eff > best_score_local:
                    best_score_local = eff
                    best_pick = (cand, expected, n_tentative)

            if best_pick is None:
                break

            cand, expected_loot, count = best_pick
            remaining[(cand["source"], cand["unit"])] -= count
            wave_entry = {
                "wave_index": 0,  # filled after sort
                "slot_id": sm["slot_id"],
                "list_id": sm["list_id"],
                "list_name": sm["list_name"],
                "coords": sm["coords"],
                "unit": cand["unit"],
                "count": count,
                "bucket": bucket,
                "round_trip_min": cand["round_trip_min"],
                "arrival_min": int(round(cand["arrival_min"])),
                "expected_loot": expected_loot,
                "source": cand["source"],
            }
            selected.append(wave_entry)

        # v3.3 Bug B — Chronological renumber + cumulative-carry recompute.
        # Sort waves by arrival_min ascending and recompute expected_loot using
        # `cumulative_carry_taken_so_far` across ALL prior waves, not just the
        # immediate predecessor. This produces a monotonically non-increasing
        # expected_loot sequence in wave_index order.
        selected.sort(key=lambda w: w["arrival_min"])
        cumulative_taken: int = 0
        last_arrival: float | None = None
        for idx, w in enumerate(selected, 1):
            w["wave_index"] = idx
            this_carry = w["count"] * UNIT_PROFILES[w["unit"]]["carry"]
            if idx == 1:
                recomputed = int(min(base_avg_loot, this_carry))
            else:
                interval_h = max(0.0, (w["arrival_min"] - last_arrival) / 60.0)
                refilled = interval_h * DEFAULT_FARM_REFILL_PER_HOUR
                residual = max(0.0, base_avg_loot - cumulative_taken)
                available = min(residual + refilled, base_avg_loot)
                recomputed = int(min(available, this_carry))
            # Resize the wave down if the chronological recompute reveals it's
            # over-allocated (e.g., we sized for residual+refill but available
            # turned out smaller because earlier waves drained more than expected).
            if recomputed < w["expected_loot"]:
                new_count = max(1, troops_for_loot(recomputed, w["unit"]))
                if new_count < w["count"]:
                    remaining[(w["source"], w["unit"])] += (w["count"] - new_count)
                    w["count"] = new_count
                    this_carry = w["count"] * UNIT_PROFILES[w["unit"]]["carry"]
                    recomputed = int(min(recomputed, this_carry))
            w["expected_loot"] = recomputed
            cumulative_taken += recomputed
            last_arrival = w["arrival_min"]
            waves_by_v[w["source"]].append(w)
        sm["planned_waves"] = len(selected)

        # v3.3 Bug A — Cross-village stacking recommendations. After committing
        # owner-village waves, look at other villages: if one could add a wave
        # with ≥10-min spacing and a usable expected_loot, recommend ADDing the
        # target to a list owned by that village. Cap at 2 per slot.
        if selected:
            other_villages = [v for v in SOURCE_VILLAGES if v != owner_label]
            other_candidates: list[dict[str, Any]] = []
            for other_v in other_villages:
                cands = _enumerate_candidates_for_village(sm, other_v)
                cands.sort(key=lambda c: c["base_efficiency"], reverse=True)
                for cand in cands:
                    # Spacing vs already-committed same-village waves
                    if any(
                        abs(cand["arrival_min"] - w["arrival_min"]) < MIN_SPACING_MINUTES
                        for w in selected
                    ):
                        continue
                    # Refill-aware expected loot using cumulative state
                    priors = [w for w in selected if w["arrival_min"] < cand["arrival_min"]]
                    if priors:
                        prior_arrival = max(p["arrival_min"] for p in priors)
                        cum_to_prior = sum(p["expected_loot"] for p in priors)
                    else:
                        prior_arrival = None
                        cum_to_prior = 0
                    if prior_arrival is None:
                        expected_x = int(base_avg_loot)
                    else:
                        interval_h = max(0.0, (cand["arrival_min"] - prior_arrival) / 60.0)
                        residual = max(0.0, base_avg_loot - cum_to_prior)
                        expected_x = int(min(residual + interval_h * DEFAULT_FARM_REFILL_PER_HOUR, base_avg_loot))
                    if expected_x < min_wave_loot_for_unit(cand["unit"]):
                        continue
                    rec_count = max(1, math.ceil(expected_x * 1.10 / UNIT_PROFILES[cand["unit"]]["carry"]))
                    other_candidates.append({
                        "slot": sm,
                        "target_coords": sm["coords"],
                        "primary_list_name": sm["list_name"],
                        "primary_owner_village": owner_label,
                        "stacking_with_owner_village": other_v,
                        "source": other_v,
                        "unit": cand["unit"],
                        "count": rec_count,
                        "arrival_min": int(round(cand["arrival_min"])),
                        "expected_loot": expected_x,
                        "score": cand["base_efficiency"],
                    })
                    break  # only the top candidate per village

            # Top 2 cross-village recs per target by expected_loot/round_trip
            other_candidates.sort(key=lambda r: -r["expected_loot"])
            for r in other_candidates[:2]:
                cross_village_recommendations.append(r)

    return waves_by_v, cross_village_recommendations


def total_strike_troops(waves_by_v: dict[str, list[dict[str, Any]]]) -> int:
    return sum(w["count"] for waves in waves_by_v.values() for w in waves)


# ─── Step E — Auto-scout list (Section 2.6) ───────────────────────────────


def build_auto_scout(
    slot_ms: list[dict[str, Any]],
    gap_targets: list[dict[str, Any]],
    villages: dict[str, Any],
) -> dict[str, Any]:
    """Build V1-AUTO-SCOUT proposal: 1 scout/slot for gaps + never-scouted + stale."""
    candidates: list[dict[str, Any]] = []
    seen_coords: set[tuple[int, int]] = set()

    # 1. Gap targets (from analyze, not in current lists)
    for gt in gap_targets:
        c = gt["coords"]
        if c in seen_coords:
            continue
        seen_coords.add(c)
        candidates.append({
            "coords": c,
            "reason": "gap_in_reports_not_in_list",
            "score": gt.get("score", 0.0),
        })

    # 2. Never-scouted slots (defense_proxy=0 and >=1 raid)
    for sm in slot_ms:
        if sm["coords"] in seen_coords:
            continue
        if sm["defense_proxy"] == 0 and sm["sample_count"] >= 1:
            candidates.append({
                "coords": sm["coords"],
                "reason": "never_scouted_but_raided",
                "score": sm["canonical_score"],
            })
            seen_coords.add(sm["coords"])

    # 3. Stale intel (last raid >7d, but still has history)
    for sm in slot_ms:
        if sm["coords"] in seen_coords:
            continue
        if sm["last_raid_age_h"] is not None and sm["last_raid_age_h"] > STALE_HOURS:
            candidates.append({
                "coords": sm["coords"],
                "reason": "stale_over_7d",
                "score": sm["canonical_score"],
            })
            seen_coords.add(sm["coords"])

    # 4. Intel aging (3-7d) — re-scout to refresh the picture before strikes commit.
    #    Helps fill the auto-scout list toward its 100-slot cap when stale/never-scouted
    #    pools are small (typical for an active operator with regular raids).
    for sm in slot_ms:
        if sm["coords"] in seen_coords:
            continue
        if (
            sm["last_raid_age_h"] is not None
            and INTEL_AGING_HOURS < sm["last_raid_age_h"] <= STALE_HOURS
            and sm["canonical_score"] > 0
        ):
            candidates.append({
                "coords": sm["coords"],
                "reason": "intel_aging_3_to_7d",
                "score": sm["canonical_score"],
            })
            seen_coords.add(sm["coords"])

    # Cap at AUTO_SCOUT_CAP, sorted by potential (score desc, then reason priority)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    capped = candidates[:AUTO_SCOUT_CAP]
    overflow = candidates[AUTO_SCOUT_CAP:]

    return {
        "list_name": "V1-AUTO-SCOUT",
        "source_village": "V1",
        "owner_village_id": vid_for("V1", villages),
        "slot_count": len(capped),
        "scouts_per_slot": 1,
        "total_scouts_committed": len(capped),
        "slots": capped,
        "overflow_for_re_scout_queue": overflow,
    }


# ─── Step F — Slow-wave lists ─────────────────────────────────────────────


def build_slow_waves(slot_ms: list[dict[str, Any]], villages: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Build V3-SLOW-RAM-CARRY and V3-SLOW-CATA-CARRY proposals.

    Eligibility: total_raids >= 10 AND last_raid.icon == 'no_loss' AND defense_proxy <= 100.
    Each slot = 3 Axes + 1 (Ram or Cata). Default inactive — operator opt-in.
    """
    src_coords = BUDGET["V3"]["coords"]
    v3_vid = vid_for("V3", villages)
    eligible = [
        sm for sm in slot_ms
        if sm["bucket"] in ("HIGH", "MID")
        and sm["sample_count"] >= 10
        and sm["last_raid_icon"] == "no_loss"
        and sm["defense_proxy"] <= SLOW_WAVE_DEF_LIMIT
    ]
    eligible.sort(key=lambda x: x["canonical_score"], reverse=True)
    ram_cap = min(SLOW_WAVE_CAP, BUDGET["V3"]["t7"])  # 20 max but bounded by ram supply
    cata_cap = min(SLOW_WAVE_CAP, BUDGET["V3"]["t8"])
    ram_capped = eligible[:ram_cap]
    cata_capped = eligible[:cata_cap]

    def slot_payload(sm: dict[str, Any], slow_unit: str) -> dict[str, Any]:
        tx, ty = sm["coords"]
        dist = travian_distance(src_coords[0], src_coords[1], tx, ty)
        slow_speed = UNIT_PROFILES[slow_unit]["speed"]
        rt_min = int(round(2 * dist / slow_speed * 60))
        return {
            "coords": sm["coords"],
            "list_name_ref": sm["list_name"],
            "total_raids": sm["sample_count"],
            "total_booty": sm["total_booty"],
            "avg_loot": sm["avg_loot"],
            "last_raid_icon": sm["last_raid_icon"],
            "defense_proxy": sm["defense_proxy"],
            "composition": {"t3": SLOW_WAVE_AXE_COUNT, slow_unit: 1},
            "wave_speed_fh": slow_speed,
            "round_trip_min": rt_min,
            "carry_per_send_res": SLOW_WAVE_AXE_COUNT * UNIT_PROFILES["t3"]["carry"],
        }

    return {
        "V3-SLOW-RAM-CARRY": {
            "list_name": "V3-SLOW-RAM-CARRY",
            "source_village": "V3",
            "owner_village_id": v3_vid,
            "default_active": False,
            "warning": "OPT-IN ONLY — DO NOT TRIGGER ALL by default",
            "slot_count": len(ram_capped),
            "rams_committed_if_all_active": len(ram_capped),
            "axes_committed_if_all_active": len(ram_capped) * SLOW_WAVE_AXE_COUNT,
            "slots": [slot_payload(sm, "t7") for sm in ram_capped],
        },
        "V3-SLOW-CATA-CARRY": {
            "list_name": "V3-SLOW-CATA-CARRY",
            "source_village": "V3",
            "owner_village_id": v3_vid,
            "default_active": False,
            "warning": "OPT-IN ONLY — DO NOT TRIGGER ALL by default",
            "slot_count": len(cata_capped),
            "catas_committed_if_all_active": len(cata_capped),
            "axes_committed_if_all_active": len(cata_capped) * SLOW_WAVE_AXE_COUNT,
            "slots": [slot_payload(sm, "t8") for sm in cata_capped],
        },
    }


# ─── Step G — Diff actions ────────────────────────────────────────────────


def diff_action(sm: dict[str, Any], waves_by_v: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """v3.1 — Diff comparator with correct existing-unit semantics.

    Per Bug 1: the comparator must operate on the slot's *existing primary unit*,
    not on the planner's "best" unit. Branches:

      1. def > 500 → DELETE
      2. CT2/CT3 suspect → FLAG_CT2_CT3
      3. DEACTIVATED bucket → DEACTIVATE (with reason from bucketing)
      4. No planned waves → DEACTIVATE (unscorable)
      5. ADD_WAVE if bucket wants more waves than currently planned
      6. Empty slot → BOOST (add the recommended unit to an empty slot)
      7. Same unit (existing == recommended) → BOOST / SHRINK / KEEP by count
      8. Existing over-allocated (carry > avg_loot × 1.5) → SHRINK on existing unit
         (catches the GoldMine-TK01 / GoldMine-Axe01 verification cases)
      9. Existing covers avg_loot × 0.9 → KEEP (no churn)
     10. Recommended carry > existing × 1.30 → CHANGE_UNIT
     11. Otherwise → KEEP (no meaningful lift)
    """
    # 1. Hard delete
    if sm["defense_proxy"] > DEFENSE_HARD_LIMIT:
        return {"action": "DELETE", "reason": "def_over_500"}

    # 2. CT2/CT3 suspect
    if sm["pushing_protection_suspect"]:
        return {"action": "FLAG_CT2_CT3", "reason": "total_raids>=3 and total_booty==0"}

    # 3. Deactivated bucket
    if sm["bucket"] == "DEACTIVATED":
        reason = sm.get("deactivation_reason", "deactivated")
        return {"action": "DEACTIVATE", "reason": reason}

    # Find planned waves for this slot
    planned_waves = [
        w for waves in waves_by_v.values() for w in waves if w["slot_id"] == sm["slot_id"]
    ]
    n_planned = len(planned_waves)
    if n_planned == 0:
        # v3.2 — distinguish active vs inactive empty slots.
        # Active slot with no plan → DEACTIVATE with 99,999 placeholder.
        # Inactive slot → KEEP (no action needed).
        if sm.get("is_active") and slot_primary_unit(sm["troops"]) is not None:
            return {
                "action": "DEACTIVATE",
                "reason": "planner_could_not_allocate_supply",
                "current_unit": slot_primary_unit(sm["troops"]),
                "current_count": int(sm["troops"].get(slot_primary_unit(sm["troops"]) or "", 0)),
            }
        return {"action": "KEEP", "reason": "unscorable_no_supply_inactive"}

    # 5. ADD_WAVE if the bucket wants more waves than were placed across lists
    desired_waves = waves_per_slot_default(sm["bucket"])
    if n_planned < desired_waves and sm["bucket"] in ("HIGH", "MID"):
        return {
            "action": "ADD_WAVE",
            "reason": f"bucket={sm['bucket']} wants {desired_waves}, currently {n_planned}",
            "additional_waves_needed": desired_waves - n_planned,
        }

    # Determine recommended (unit, count) from the slot's primary planned wave
    primary = next(
        (w for w in planned_waves if w["wave_index"] == 1),
        planned_waves[0] if planned_waves else None,
    )
    if primary is None:
        return {"action": "KEEP", "reason": "no_primary_wave"}
    recommended_uid = primary["unit"]
    recommended_count = primary["count"]
    rec_profile = UNIT_PROFILES[recommended_uid]
    rec_name = rec_profile["name"]

    # Slot's existing primary unit (not the planner's best — the operator's allocation)
    existing_uid = slot_primary_unit(sm["troops"])
    existing_count = int(sm["troops"].get(existing_uid, 0)) if existing_uid else 0
    existing_name = UNIT_PROFILES[existing_uid]["name"] if existing_uid else None
    avg_loot = sm["avg_loot"]

    # MOVE: HIGH-scoring target stuck in a list whose name screams inactive/low
    list_name_lower = sm["list_name"].lower()
    if sm["bucket"] == "HIGH" and ("inactive" in list_name_lower or "low" in list_name_lower):
        return {
            "action": "MOVE",
            "reason": f"HIGH-scoring target in list named '{sm['list_name']}'",
            "recommended_unit": recommended_uid,
            "recommended_count": recommended_count,
            "current_unit": existing_uid,
            "current_count": existing_count,
        }

    # 6. Empty slot → BOOST (add the recommended unit)
    if existing_uid is None:
        return {
            "action": "BOOST",
            "reason": f"empty slot; add {recommended_count}× {rec_name}",
            "recommended_unit": recommended_uid,
            "recommended_count": recommended_count,
            "current_unit": None,
            "current_count": 0,
        }

    # 7. Same unit on both sides → genuine BOOST/SHRINK/KEEP zone
    if existing_uid == recommended_uid:
        if recommended_count > existing_count * SAME_UNIT_BOOST_MULT:
            return {
                "action": "BOOST",
                "reason": f"recommended {recommended_count} > current {existing_count} (+{recommended_count - existing_count})",
                "recommended_unit": recommended_uid,
                "recommended_count": recommended_count,
                "current_unit": existing_uid,
                "current_count": existing_count,
            }
        if recommended_count < existing_count * SAME_UNIT_SHRINK_MULT:
            return {
                "action": "SHRINK",
                "reason": f"recommended {recommended_count} < current {existing_count} (-{existing_count - recommended_count})",
                "recommended_unit": recommended_uid,
                "recommended_count": recommended_count,
                "current_unit": existing_uid,
                "current_count": existing_count,
            }
        return {
            "action": "KEEP",
            "reason": "within ±20% of recommended",
            "recommended_unit": recommended_uid,
            "recommended_count": recommended_count,
            "current_unit": existing_uid,
            "current_count": existing_count,
        }

    # 8/9/10/11 — Different unit. Compare carry against avg_loot and against the recommended.
    existing_carry = existing_count * UNIT_PROFILES[existing_uid]["carry"]
    recommended_carry = recommended_count * rec_profile["carry"]

    # 8. Over-allocated on the existing unit → SHRINK the existing unit
    #    (Catches GoldMine-TK01 10× TK carry=800 vs avg_loot=480, GoldMine-Axe01 35× Axe.)
    #    v3.2: size with a 1.10 carry-safety multiplier so the slot covers slightly
    #    above-average refills without re-tuning every cycle.
    #    Only fires when the new size is materially smaller than existing,
    #    otherwise the "shrink" would be a no-op (ceil rounding).
    if avg_loot > 0 and existing_carry > avg_loot * OVER_ALLOC_MULT:
        new_existing_count = max(
            1,
            math.ceil(avg_loot * 1.10 / UNIT_PROFILES[existing_uid]["carry"]),
        )
        if new_existing_count < existing_count:
            return {
                "action": "SHRINK",
                "reason": (
                    f"existing carry {existing_carry} > avg_loot {avg_loot:.1f} × 1.5; "
                    f"shrink {existing_count}× {existing_name} → {new_existing_count}× {existing_name}"
                ),
                "recommended_unit": existing_uid,
                "recommended_count": new_existing_count,
                "current_unit": existing_uid,
                "current_count": existing_count,
            }
        # otherwise fall through (existing covers ≥ 0.9 OK branch handles it)

    # 9. Existing carry already covers ~90% of avg_loot → KEEP
    if avg_loot > 0 and existing_carry >= avg_loot * EXISTING_OK_MULT:
        return {
            "action": "KEEP",
            "reason": f"existing carry {existing_carry} ≥ avg_loot {avg_loot:.0f} × 0.9 (no churn)",
            "recommended_unit": existing_uid,
            "recommended_count": existing_count,
            "current_unit": existing_uid,
            "current_count": existing_count,
        }

    # 10. Recommended carry materially better → CHANGE_UNIT
    if existing_carry > 0 and recommended_carry > existing_carry * CHANGE_UNIT_MULT:
        return {
            "action": "CHANGE_UNIT",
            "reason": (
                f"swap {existing_count}× {existing_name} (carry {existing_carry}) → "
                f"{recommended_count}× {rec_name} (carry {recommended_carry})"
            ),
            "recommended_unit": recommended_uid,
            "recommended_count": recommended_count,
            "current_unit": existing_uid,
            "current_count": existing_count,
        }

    # 11. Not enough lift to justify a swap → KEEP
    return {
        "action": "KEEP",
        "reason": "different unit recommended but lift < 30%",
        "recommended_unit": existing_uid,
        "recommended_count": existing_count,
        "current_unit": existing_uid,
        "current_count": existing_count,
    }


def expected_daily_delta(sm: dict[str, Any], waves_by_v: dict[str, list[dict[str, Any]]]) -> float:
    """Rough daily booty delta proxy from this slot's planned waves.

    Assumes 8 cycles/day. avg_loot * waves * cycles, capped at avg_loot * 8 per wave
    (depletion not modelled — operator stacks waves to skirt this).
    """
    waves = [w for waves in waves_by_v.values() for w in waves if w["slot_id"] == sm["slot_id"]]
    if not waves:
        return 0.0
    per_cycle = sum(w["expected_loot"] for w in waves)
    return per_cycle * 4  # 4 cycles/day conservative (vs 24h / 30min avg roundtrip)


# ─── Per-target consolidation pass (Bug 3 — v3.1) ─────────────────────────


def _is_slow_wave_list_name(name: str) -> bool:
    """A list named like 'V3-SLOW-RAM-CARRY' / 'V3-SLOW-CATA-CARRY' / similar."""
    return "-SLOW-" in (name or "").upper()


def consolidate_per_target(
    actions: list[dict[str, Any]],
    slot_ms: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Group actions by (x,y); collapse duplicates into one primary + N DEACTIVATEs.

    Primary slot = highest total_raids → highest total_booty → alphabetical list name.
    Slow-wave lists are intentionally on the same coords as strike-wave slots
    and are skipped from consolidation.

    Returns (consolidated_actions, num_duplicate_deactivates).
    """
    slot_lookup_by_id: dict[int, dict[str, Any]] = {
        sm["slot_id"]: sm for sm in slot_ms if sm.get("slot_id") is not None
    }
    by_coord: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    bypass: list[dict[str, Any]] = []

    for a in actions:
        sm = a["slot"]
        coords = tuple(sm["coords"]) if sm.get("coords") else None
        list_name = sm.get("list_name", "")
        # Bypass consolidation for:
        #   ADD_NEW — synthetic gap slot, not in any existing list
        #   ADD_NEW_SLOT — cross-village stacking recommendation (intentionally
        #                  shares coord with primary slot; NOT a duplicate)
        #   SPLIT_LIST — list-level action with synthetic (0,0) coords
        #   REORDER — list-level action; coord is incidental
        #   slow-wave lists — same coords as strike lists by design
        if (
            coords is None
            or _is_slow_wave_list_name(list_name)
            or a["action"] in (
                "ADD_NEW", "ADD_NEW_SLOT", "SPLIT_LIST", "REORDER", "THROUGHPUT_DROP",
            )
        ):
            bypass.append(a)
            continue
        by_coord[coords].append(a)

    consolidated: list[dict[str, Any]] = list(bypass)
    duplicate_count = 0

    for coords, group in by_coord.items():
        if len(group) == 1:
            consolidated.append(group[0])
            continue
        # v3.2 — expanded tiebreak: (raids, booty, running_attacks, is_active, list_name)
        scored: list[tuple[int, int, int, int, str, dict[str, Any]]] = []
        for a in group:
            sm = a["slot"]
            scored.append(
                (
                    -int(sm.get("sample_count") or 0),
                    -int(sm.get("total_booty") or 0),
                    -int(sm.get("running_attacks") or 0),
                    0 if sm.get("is_active") else 1,  # active preferred → smaller key
                    sm.get("list_name", ""),
                    a,
                )
            )
        scored.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
        primary = scored[0][5]
        consolidated.append(primary)

        # Edge case (v3.2): if primary itself is DEACTIVATE / DELETE, every instance
        # of this target is bad. Duplicates inherit a different reason so the
        # operator sees that consolidation didn't rescue a usable slot.
        primary_action = primary.get("action")
        primary_is_dead = primary_action in ("DEACTIVATE", "DELETE")

        for _, _, _, _, list_name, dup in scored[1:]:
            duplicate_count += 1
            dup_sm = dup["slot"]
            if primary_is_dead:
                reason = (
                    f"duplicate_target — primary '{primary['slot']['list_name']}' "
                    f"is also {primary_action}"
                )
            else:
                reason = (
                    f"duplicate_target — consolidated to '{primary['slot']['list_name']}'"
                )
            consolidated.append({
                **dup,
                "action": "DEACTIVATE",
                "reason": reason,
                "current_str": dup.get("current_str") or f"in `{dup_sm['list_name']}`",
                "recommended_str": "—",
                "extra": {
                    **(dup.get("extra") or {}),
                    "consolidated_into": primary["slot"]["list_name"],
                    "consolidated_into_list_id": primary["slot"].get("list_id"),
                    "primary_action": primary_action,
                },
            })

    return consolidated, duplicate_count


# ─── Bug 5 — per-list sort recommendation + REORDER actions (v3.2) ────────


def recommend_sort_mode_for_list(slots_in_list: list[dict[str, Any]]) -> tuple[str, str]:
    """Return (sort_mode, rationale) for a list's slots.

    sort_mode is one of: 'total_bounty_desc', 'last_bounty_desc', 'next_attack_asc'.
    The operator clicks that column header once in the Travian UI to apply.
    """
    if not slots_in_list:
        return ("total_bounty_desc", "empty list")
    mature = sum(1 for s in slots_in_list if int(s.get("sample_count") or 0) >= 10)
    immature = sum(1 for s in slots_in_list if int(s.get("sample_count") or 0) < 3)
    total = len(slots_in_list)
    if mature >= total * 0.6:
        return (
            "total_bounty_desc",
            f"{mature}/{total} slots have ≥10 raids; cumulative bounty is the most reliable signal",
        )
    if immature >= total * 0.6:
        return (
            "next_attack_asc",
            f"{immature}/{total} slots have <3 raids; sort by next-attack so available slots fire first",
        )
    return (
        "last_bounty_desc",
        f"mixed maturity ({mature} mature, {immature} immature of {total}); most-recent bounty is the best blended signal",
    )


def _simulated_sort_position(
    slots: list[dict[str, Any]],
    sort_mode: str,
) -> dict[int, int]:
    """Return {slot_id: post-sort index} for a given sort mode.

    Uses fields captured in slot_metadata. ``next_attack_asc`` falls back to
    ``last_raid_time + 2*distance / 7h`` since the API doesn't expose
    next_attack_time directly.
    """
    def key_total_bounty(s: dict[str, Any]) -> float:
        return -float(s.get("total_booty") or 0)

    def key_last_bounty(s: dict[str, Any]) -> float:
        return -float(s.get("last_raid_resources") or 0)

    def key_next_attack(s: dict[str, Any]) -> float:
        # last_raid_time + 2 × distance / 7 f/h ≈ next available
        last_t = s.get("last_raid_time")
        dist = float(s.get("distance_from_owner") or 0.0)
        if last_t is None:
            return float("inf")
        return last_t + (2 * dist / 7.0) * 3600

    keyfn = {
        "total_bounty_desc": key_total_bounty,
        "last_bounty_desc": key_last_bounty,
        "next_attack_asc": key_next_attack,
    }.get(sort_mode, key_total_bounty)
    sorted_slots = sorted(slots, key=keyfn)
    return {s["slot_id"]: idx for idx, s in enumerate(sorted_slots) if s.get("slot_id") is not None}


def emit_reorder_actions(
    list_name: str,
    list_id: int,
    slots_in_list: list[dict[str, Any]],
    sort_mode: str,
) -> list[dict[str, Any]]:
    """Identify high-value slots whose position the recommended sort won't fix.

    Returns up to 5 REORDER actions per list. Skipped for lists with <10 slots.
    """
    n = len(slots_in_list)
    if n < 10:
        return []
    if sort_mode == "next_attack_asc":
        # next_attack_asc simulation is fragile (depends on travel timing per cycle).
        # Skip REORDER for this sort mode — operator applies the sort and observes.
        return []

    # Top quartile boundary by post-sort position (0-indexed cutoff)
    top_quartile_boundary = max(1, n // 4)
    post_positions = _simulated_sort_position(slots_in_list, sort_mode)

    # Top-10% score threshold for "high-value"
    scores = [float(s.get("canonical_score") or 0.0) for s in slots_in_list]
    sorted_scores = sorted(scores, reverse=True)
    cutoff_idx = max(0, int(len(sorted_scores) * 0.10) - 1)
    score_threshold = sorted_scores[cutoff_idx] if sorted_scores else 0.0
    if score_threshold <= 0:
        return []

    actions: list[dict[str, Any]] = []
    for slot in slots_in_list:
        post_idx = post_positions.get(slot["slot_id"], n)
        if post_idx <= top_quartile_boundary:
            continue
        if float(slot.get("canonical_score") or 0.0) < score_threshold:
            continue
        # Frequency-gain proxy: a tail slot fires ~half as often as a top slot.
        # Moving into the top quartile ≈ +2 raids/day on top of current avg.
        avg_loot = float(slot.get("avg_loot") or 0.0)
        delta = int(avg_loot * 2)
        actions.append({
            "slot": slot,
            "action": "REORDER",
            "reason": (
                f"high-value slot (score top 10% in list) lands at position {post_idx + 1}/{n} "
                f"after applying recommended sort `{sort_mode}`; manual drag into top quartile "
                f"~{top_quartile_boundary} adds est ~{delta} res/day"
            ),
            "tier": 2,
            "expected_daily_delta_booty": float(delta),
            "current_str": f"position ~{post_idx + 1}/{n} post-sort",
            "recommended_str": f"manually drag into top quartile (~{top_quartile_boundary})",
            "extra": {
                "list_name": list_name,
                "list_id": list_id,
                "post_sort_position": post_idx + 1,
                "target_position": top_quartile_boundary,
                "sort_mode_assumed": sort_mode,
            },
        })
        if len(actions) >= 5:
            break
    return actions


# ─── v3.4 — THROUGHPUT_DROP emission (snapshot-history-driven) ────────────


def _tier_for_throughput_drop(pct_change: float) -> int:
    """Map a per-slot drop magnitude to a diff tier.

    -1.00 .. -0.70  → Tier 1 (effectively dead / severe drop)
    -0.70 .. -0.50  → Tier 2 (significant drop, worth investigating)
    Anything above -0.50 should NOT have been considered for emission.
    """
    if pct_change <= THROUGHPUT_TIER1_THRESHOLD:
        return 1
    return 2


def _booty_loss_per_day(throughput: SlotThroughput) -> int:
    """Estimated booty lost per day from the rate drop.

    = (baseline_rate − recent_rate) × avg_loot_per_raid

    Used as the `expected_daily_delta_booty` for THROUGHPUT_DROP actions
    (the "delta" here represents booty being lost, not gained — operators
    still want it surfaced near the top of the report).

    Disappearance case (pct_change = -1.0 with no recent pairs): if
    `raids_per_24h_recent` is None, treat recent as 0. Still requires a
    baseline rate for the math.
    """
    baseline = throughput.raids_per_24h_baseline or 0.0
    recent = throughput.raids_per_24h_recent or 0.0
    return int(max(0.0, (baseline - recent) * throughput.avg_loot_per_raid))


def emit_throughput_drop_actions(
    slot_ms: list[dict[str, Any]],
    existing_actions_by_slot_id: dict[int | None, dict[str, Any]],
    throughput_by_slot_id: dict[int, SlotThroughput],
) -> list[dict[str, Any]]:
    """Produce THROUGHPUT_DROP actions for slots whose rate dropped ≥50%.

    Skipped when:
      * pct_change is None (insufficient history — silence is safer).
      * pct_change > THROUGHPUT_DROP_THRESHOLD (drop isn't significant).
      * samples_used < THROUGHPUT_MIN_SAMPLES.
      * The slot already has a DEACTIVATE / DELETE / FLAG_CT2_CT3 action —
        the slot is already being muted, no extra noise.
    """
    out: list[dict[str, Any]] = []
    silenced_actions = {"DEACTIVATE", "DELETE", "FLAG_CT2_CT3"}
    slot_by_id: dict[int, dict[str, Any]] = {
        sm["slot_id"]: sm for sm in slot_ms if sm.get("slot_id") is not None
    }

    for slot_id, throughput in throughput_by_slot_id.items():
        if throughput.pct_change is None:
            continue
        if throughput.pct_change > THROUGHPUT_DROP_THRESHOLD:
            continue
        if throughput.samples_used < THROUGHPUT_MIN_SAMPLES:
            continue

        existing = existing_actions_by_slot_id.get(slot_id)
        if existing and existing.get("action") in silenced_actions:
            continue

        sm = slot_by_id.get(slot_id)
        if sm is None:
            # Slot disappeared from current run's slot_ms — synthesize a minimal
            # slot dict so renderers don't crash.
            sm = {
                "list_id": None,
                "list_name": throughput.list_name,
                "slot_id": slot_id,
                "coords": throughput.coords,
                "bucket": "DEACTIVATED",
                "is_active": False,
                "is_running": False,
                "troops": {},
                "sample_count": 0,
                "avg_loot": throughput.avg_loot_per_raid,
                "canonical_score": 0.0,
                "defense_proxy": 0,
                "last_raid_icon": "unknown",
                "last_raid_age_h": None,
                "total_raids": 0,
                "total_booty": 0,
                "name": "",
                "population": 0,
                "planned_waves": 0,
                "pushing_protection_suspect": False,
            }

        tier = _tier_for_throughput_drop(throughput.pct_change)
        recent_str = (
            f"{throughput.raids_per_24h_recent:.2f}"
            if throughput.raids_per_24h_recent is not None
            else "0.00"
        )
        baseline_str = (
            f"{throughput.raids_per_24h_baseline:.2f}"
            if throughput.raids_per_24h_baseline is not None
            else "?"
        )
        pct_str = f"{throughput.pct_change * 100:.0f}%"
        booty_loss = _booty_loss_per_day(throughput)

        action = {
            "slot": sm,
            "action": "THROUGHPUT_DROP",
            "tier": tier,
            "reason": (
                f"throughput_drop: recent {recent_str}/24h is {pct_str} below "
                f"7-14d baseline {baseline_str}/24h "
                f"({throughput.samples_used} sample pairs)"
            ),
            "expected_daily_delta_booty": float(booty_loss),
            "current_str": (
                f"raids_per_24h recent {recent_str} vs baseline {baseline_str} "
                f"({pct_str} over 14d)"
            ),
            "recommended_str": (
                "investigate: chiefed target / cranny depleted / position drift"
            ),
            "extra": {
                "slot_id": slot_id,
                "raids_per_24h_recent": throughput.raids_per_24h_recent,
                "raids_per_24h_baseline": throughput.raids_per_24h_baseline,
                "pct_change": throughput.pct_change,
                "samples_used": throughput.samples_used,
                "last_seen_active_at": (
                    throughput.last_seen_active_at.isoformat()
                    if throughput.last_seen_active_at
                    else None
                ),
                "booty_loss_per_day": booty_loss,
            },
        }
        out.append(action)

    return out


# ─── Bug 6 — SPLIT_LIST recommendation (v3.2) ─────────────────────────────


def emit_split_list_action(
    list_summary: dict[str, Any],
    slots_in_list: list[dict[str, Any]],
    consolidated_deactivate_slot_ids: set[int],
    server_age_days: int,
) -> dict[str, Any] | None:
    """Recommend splitting a tail-starved list into Top + Tail.

    Conditions (must all hold):
      - active slot count (post-consolidation) >= 30
      - lifetime raids/slot < 8
      - top-20 slots by canonical_score carry ≥ 50% of the list's total score

    Returns the SPLIT_LIST action dict, or None.
    """
    active_slots = [
        s for s in slots_in_list
        if s.get("slot_id") not in consolidated_deactivate_slot_ids
    ]
    active_count = len(active_slots)
    if active_count < 30:
        return None
    total_raids = int(list_summary.get("total_raids") or 0)
    slots_amount = int(list_summary.get("slots_amount") or 1)
    rps = total_raids / max(slots_amount, 1)
    if rps >= 8:
        return None

    sorted_by_score = sorted(active_slots, key=lambda s: float(s.get("canonical_score") or 0.0), reverse=True)
    top_20 = sorted_by_score[:20]
    tail = sorted_by_score[20:]
    top_score = sum(float(s.get("canonical_score") or 0.0) for s in top_20)
    tail_score = sum(float(s.get("canonical_score") or 0.0) for s in tail)
    total_score = top_score + tail_score
    if total_score <= 0 or top_score < total_score * 0.5:
        return None

    list_name = list_summary.get("name") or f"list-{list_summary.get('id')}"
    new_top_name = f"{list_name}-Top"
    new_tail_name = f"{list_name}-Tail"

    # Frequency-gain proxy: top-20 slots currently fire at list average.
    # Isolating them roughly doubles their cycle frequency.
    top_current_daily = sum(
        (float(s.get("total_booty") or 0) / max(int(s.get("sample_count") or 1), 1))
        * (int(s.get("sample_count") or 0) / max(server_age_days, 1))
        for s in top_20
    )
    expected_gain = int(top_current_daily)  # ~same again as gain

    # Build a synthetic "list" slot for tier/render compatibility
    synth_slot = {
        "list_id": list_summary.get("id"),
        "list_name": list_name,
        "slot_id": None,
        "coords": (0, 0),
        "name": list_name,
        "bucket": "SPLIT",
        "is_active": True,
        "is_running": False,
        "troops": {},
        "sample_count": 0,
        "avg_loot": 0,
        "canonical_score": top_score,
        "defense_proxy": 0,
        "last_raid_age_h": None,
    }
    return {
        "slot": synth_slot,
        "action": "SPLIT_LIST",
        "reason": (
            f"tail starvation: {active_count} active slots, {rps:.1f} raids/slot lifetime. "
            f"Top 20 carry {top_score / total_score * 100:.0f}% of list value; isolating them "
            f"makes every cycle fire them while the tail fires opportunistically."
        ),
        "tier": 1,
        "expected_daily_delta_booty": float(expected_gain),
        "current_str": f"{active_count} active slots in `{list_name}` ({rps:.1f} R/slot)",
        "recommended_str": f"split → `{new_top_name}` (top 20) + `{new_tail_name}` (remaining)",
        "extra": {
            "list_name": list_name,
            "list_id": list_summary.get("id"),
            "active_slot_count": active_count,
            "raids_per_slot": round(rps, 2),
            "top_share_pct": round(top_score / total_score * 100, 1),
            "top_20_coords": [list(s["coords"]) for s in top_20],
            "tail_coords": [list(s["coords"]) for s in tail],
            "new_top_name": new_top_name,
            "new_tail_name": new_tail_name,
        },
    }


def prune_reorder_actions_for_split_lists(
    actions: list[dict[str, Any]],
    split_list_names: set[str],
) -> list[dict[str, Any]]:
    """Drop REORDER actions whose target list is being split — the split
    relocates the high-value slots so reordering within the tail is moot."""
    return [
        a for a in actions
        if not (
            a.get("action") == "REORDER"
            and (a.get("extra") or {}).get("list_name") in split_list_names
        )
    ]


# ─── Tier-2 cap and bulk-action collapse (Bug 4 — v3.2 order: collapse then cap)


def cap_and_collapse_tier_2(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """v3.2 — Collapse FIRST, then cap. Order matters: collapsing 143 identical
    DEACTIVATE rows BEFORE the 50-row cap leaves room for distinct actions.

    Steps:
      4a. Group all actions by (action, normalized_reason). For Tier 2 or Tier 3
          groups with ≥COLLAPSE_BULK_THRESHOLD members, emit one summary row
          and mark members as collapsed (they keep their tier, stay in JSON).
      4b. Cap Tier 2 visible distinct rows at TIER_2_VISIBLE_CAP. Demote overflow
          to Tier 3.
      4c. Cap Tier 3 visible distinct rows at the same cap.

    Returns (actions_with_updated_tiers, collapsed_summaries). The renderer
    omits collapsed rows from the markdown tables; the JSON keeps everything.
    """
    def _norm_reason(r: str) -> str:
        return (r or "").split(" — ")[0].split(";")[0].strip().lower()

    # Group across Tier 2 and Tier 3 by (action, reason_key) for collapse detection
    candidates = [a for a in actions if a["tier"] in (2, 3)]
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for a in candidates:
        by_key[(a["action"], _norm_reason(a.get("reason") or ""))].append(a)

    collapsed_summaries: list[dict[str, Any]] = []
    for (action_name, reason_key), group in by_key.items():
        if len(group) < COLLAPSE_BULK_THRESHOLD:
            continue
        collapsed_summaries.append({
            "action": action_name,
            "reason_key": reason_key,
            "count": len(group),
            "tier_majority": 2 if sum(1 for a in group if a["tier"] == 2) >= len(group) / 2 else 3,
            "sample_coords": [tuple(a["slot"]["coords"]) for a in group[:5] if a["slot"].get("coords")],
            "total_daily_delta": sum(a.get("expected_daily_delta_booty", 0.0) for a in group),
        })
        for a in group:
            a["collapsed_into_summary"] = True

    # 4b — Cap Tier 2 visible distinct rows (excluding collapsed-into-summary)
    tier2_distinct = [a for a in actions if a["tier"] == 2 and not a.get("collapsed_into_summary")]
    visible_t2 = tier2_distinct[:TIER_2_VISIBLE_CAP]
    overflow_t2 = tier2_distinct[TIER_2_VISIBLE_CAP:]
    for a in overflow_t2:
        a["tier"] = 3

    # 4c — Cap Tier 3 visible distinct rows at the same threshold
    # (re-fetch after overflow demotion)
    tier3_distinct = [a for a in actions if a["tier"] == 3 and not a.get("collapsed_into_summary")]
    # The cap controls how many we *render*; the renderer is responsible for slicing.
    # We don't demote past Tier 3 — there's no Tier 4. Just mark overflow with a hint.
    for a in tier3_distinct[TIER_2_VISIBLE_CAP:]:
        a["tier3_overflow"] = True

    return actions, collapsed_summaries


# ─── Step H — Tier prioritization ─────────────────────────────────────────


def tier_for(action: str, slot_m: dict[str, Any], delta: float) -> int:
    a = action
    if a in ("DELETE", "FLAG_CT2_CT3"):
        return 1
    if a == "BOOST" and slot_m["bucket"] == "HIGH":
        return 1
    if a == "ADD_NEW" and slot_m.get("source_bucket") == "HIGH":
        return 1
    if a == "BOOST":
        return 2
    if a == "ADD_WAVE" and slot_m["bucket"] == "HIGH":
        return 1
    if a == "ADD_WAVE":
        return 2
    if a == "SHRINK" and slot_m["bucket"] == "INACTIVE-low":
        return 2
    if a == "CHANGE_UNIT":
        # CHANGE_UNIT is a real allocation move; goes to Tier 2 unless the slot is HIGH.
        return 1 if slot_m.get("bucket") == "HIGH" else 2
    if a == "MOVE":
        return 2
    if a == "DEACTIVATE":
        return 2
    if delta > 50:
        return 2
    return 3


# ─── Step I — Verification ────────────────────────────────────────────────


def verify(
    strike_total: int,
    slow_total_if_active: int,
    auto_scout_total: int,
    auto_scout: dict[str, Any],
    slow_waves: dict[str, dict[str, Any]],
    slot_ms: list[dict[str, Any]],
) -> tuple[bool, list[str]]:
    errs: list[str] = []
    # v3.1 — 14k soft proxy removed. Only the 20K hard cap is enforced.
    if strike_total > HARD_TROOP_CAP:
        errs.append(f"strike troops {strike_total} > {HARD_TROOP_CAP} hard cap — must trim")
    if auto_scout["slot_count"] > AUTO_SCOUT_CAP:
        errs.append(f"auto-scout has {auto_scout['slot_count']} > {AUTO_SCOUT_CAP}")
    for name, prop in slow_waves.items():
        if prop["slot_count"] > SLOW_WAVE_CAP:
            errs.append(f"{name} has {prop['slot_count']} > {SLOW_WAVE_CAP}")
        if prop["default_active"]:
            errs.append(f"{name} default_active != False")
        for slot in prop["slots"]:
            if slot["defense_proxy"] > SLOW_WAVE_DEF_LIMIT:
                errs.append(f"{name} slot {slot['coords']} def>{SLOW_WAVE_DEF_LIMIT}")
            if slot["total_raids"] < 10:
                errs.append(f"{name} slot {slot['coords']} sample<10")
            if slot["last_raid_icon"] != "no_loss":
                errs.append(f"{name} slot {slot['coords']} icon!=no_loss")
    # def>500 not in any active strike slot — anything with def>500 should be DELETE-marked
    for sm in slot_ms:
        if sm["defense_proxy"] > DEFENSE_HARD_LIMIT and sm.get("planned_waves", 0) > 0:
            errs.append(f"def>500 slot {sm['coords']} still has planned waves")
    return (len(errs) == 0, errs)


# ─── Step J — Render and write ────────────────────────────────────────────


def render_markdown(ctx: dict[str, Any]) -> str:
    L: list[str] = []
    add = L.append

    draft = "DRAFT — " if not ctx["verify_ok"] else ""

    add(f"# {draft}Raid-List Diff & Optimization v3.1 — {ctx['timestamp']}")
    add("")
    add(f"**Server:** Europe 2, x1 · **Tribe:** Teuton · **Day of server:** {ctx['day_of_server']}")
    add(f"**Player:** {ctx['player_name']}  · **API:** {ctx['api_base']}")
    add("")
    add("> _v3.1: cranny-refill wave-stacker, primary-unit-aware comparator, "
        "per-target consolidation, Tier-2 visible cap. 14K soft ceiling removed — "
        "the 20K hard cap is info-only in the current army stage._")
    add("")
    if not ctx["verify_ok"]:
        add("> **DRAFT BANNER:** verification failed. See errors near end of report.")
        add("")
    add("## Summary")
    add("")
    add(f"- Total existing lists: {ctx['existing_lists_count']}")
    add(f"- Total existing slots analyzed: {ctx['slot_count_total']}")
    add(f"- Tier 1 actions: {ctx['tier_counts'][1]}")
    add(f"- Tier 2 actions: {ctx['tier_counts'][2]}")
    add(f"- Tier 3 actions: {ctx['tier_counts'][3]}")
    add(f"- Strike troops/cycle: **{ctx['strike_total']}** (20K hard cap — info only)")
    add(f"- Estimated daily-booty improvement from Tier 1+2: **{ctx['est_daily_delta_t1_t2']:.0f}** resources/day")
    if ctx.get("duplicate_deactivates"):
        add(f"- Per-target consolidation: {ctx['duplicate_deactivates']} duplicate slots demoted to DEACTIVATE")
    if ctx.get("collapsed_summaries"):
        add(f"- Tier-2 bulk collapses: {len(ctx['collapsed_summaries'])} summary rows (full detail in JSON)")
    add("")

    # 2. Live state vs budget
    add("## 2. Live state vs planning budget vs 20K cap")
    add("")
    add("| Village | Live (Clubs/Spear/Axe/Scout/Pal/TK/Ram/Cata) | Budget | Gap explanation |")
    add("|---------|-----------------------------------------------|--------|-----------------|")
    for label in ("V1", "V2", "V3", "V4", "V5", "V6"):
        live = ctx["live_troops"].get(label, {})
        b = BUDGET[label]
        live_str = "/".join(str(live.get(u, "?")) for u in ("t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"))
        budget_str = "/".join(str(b.get(u, 0)) for u in ("t1", "t2", "t3", "t4", "t5", "t6", "t7", "t8"))
        gap_parts: list[str] = []
        for u in ("t1", "t3", "t6", "t7", "t8"):
            lv = live.get(u)
            bv = b.get(u, 0)
            if isinstance(lv, int) and bv > 0 and (bv - lv) > max(5, bv * 0.10):
                gap_parts.append(f"{UNIT_PROFILES[u]['name']}: live {lv} < budget {bv} (likely in-flight)")
        gap_text = "; ".join(gap_parts) if gap_parts else "OK"
        add(f"| {label} | {live_str} | {budget_str} | {gap_text} |")
    add("")
    add("**Cap forecast (info-only — 14K soft proxy removed in v3.1):**")
    add("")
    add("| Metric | Value | 20K Hard cap |")
    add("|--------|------:|-------------:|")
    add(f"| Total strike troops/cycle | {ctx['strike_total']} | {HARD_TROOP_CAP} |")
    add(f"| Slow-wave troops if all active | {ctx['slow_total_if_active']} | {HARD_TROOP_CAP} |")
    add(f"| Auto-scout troops/cycle | {ctx['auto_scout']['slot_count']} | {HARD_TROOP_CAP} |")
    grand = ctx['strike_total'] + ctx['slow_total_if_active'] + ctx['auto_scout']['slot_count']
    add(f"| **Grand total/cycle (if everything fires)** | **{grand}** | {HARD_TROOP_CAP} |")
    add("")
    if grand > HARD_TROOP_CAP * 0.7:
        add(f"> ⚠️ Grand total at {grand}/{HARD_TROOP_CAP} — approaching the 20K cap; reintroduce a soft proxy next run.")
        add("")

    # 3. Existing-lists snapshot
    add("## 3. Existing-lists snapshot")
    add("")
    add("| List | Owner-V | Slots | Active | Running | Total raids | Total booty |")
    add("|------|---------|------:|-------:|--------:|------------:|------------:|")
    for fl in ctx["existing_lists"]:
        owner_label = ctx["village_label_by_vid"].get(fl["owner_village_id"], f"vid={fl['owner_village_id']}")
        add(f"| {fl['name']} | {owner_label} | {fl['slots_amount']} | {fl['active_slots']} | "
            f"{fl['running_raids']} | {fl.get('total_raids', 0)} | {fl.get('total_booty', 0)} |")
    if ctx.get("abandoned_lists"):
        add("")
        add(f"⚠️  **Abandoned lists detected:** {len(ctx['abandoned_lists'])}. Operator should "
            "either revive them (move to another village) or delete via the UI.")
    add("")

    # 4. Trimming report — v3.1 removed the trim step; this section only appears if
    # a future caller re-introduces it. Otherwise the section is skipped entirely.
    if ctx["trim_actions"]:
        add("## 4. Trimming report (cap enforcement)")
        add("")
        add(f"Dropped {len(ctx['trim_actions'])} waves to stay under the {HARD_TROOP_CAP}-troop cap. Worst loot-per-troop first; INACTIVE-low → MID → HIGH order.")
        add("")
        add("| # | Source | Coords | Unit | Count | Bucket | loot/troop | Reason |")
        add("|--:|--------|--------|------|------:|--------|-----------:|--------|")
        for i, ta in enumerate(ctx["trim_actions"][:30], 1):
            w = ta["removed_wave"]
            lpt = w["expected_loot"] / max(1, w["count"])
            add(f"| {i} | {ta['source']} | ({w['coords'][0]},{w['coords'][1]}) | "
                f"{UNIT_PROFILES[w['unit']]['name']} | {w['count']} | {w['bucket']} | "
                f"{lpt:.2f} | {ta['reason']} |")
        if len(ctx["trim_actions"]) > 30:
            add(f"| … | (+{len(ctx['trim_actions']) - 30} more in JSON appendix) | | | | | | |")
        add("")

    # 5. Score distribution
    add("## 5. Score distribution")
    add("")
    scored = [sm for sm in ctx["slot_ms"] if sm["bucket"] != "DEACTIVATED"]
    if scored:
        scores = sorted([sm["canonical_score"] for sm in scored])
        avg_loots = sorted([sm["avg_loot"] for sm in scored])
        def pct(p, arr): return arr[int(len(arr) * p / 100)] if arr else 0
        add(f"**Canonical-score histogram** ({len(scored)} scored slots):")
        add("")
        buckets_h = [(0, 1), (1, 5), (5, 20), (20, 100), (100, math.inf)]
        for lo, hi in buckets_h:
            n = sum(1 for s in scores if lo <= s < hi)
            label = f"{lo}-{int(hi)}" if hi != math.inf else f">= {lo}"
            add(f"- {label}: {n}")
        add("")
        add(f"**avg_loot distribution:** p25={pct(25, avg_loots):.0f} · p50={pct(50, avg_loots):.0f} · "
            f"p75={pct(75, avg_loots):.0f} · p90={pct(90, avg_loots):.0f} · max={max(avg_loots):.0f}")
        add("")
        top10 = sorted(scored, key=lambda x: x["canonical_score"], reverse=True)[:10]
        add("**Top 10 scored targets:**")
        add("")
        add("| # | Coords | Bucket | Score | avg_loot | raids | Source | Unit |")
        add("|--:|--------|--------|------:|---------:|------:|--------|------|")
        for i, sm in enumerate(top10, 1):
            add(f"| {i} | ({sm['coords'][0]},{sm['coords'][1]}) | {sm['bucket']} | "
                f"{sm['canonical_score']:.2f} | {sm['avg_loot']:.0f} | {sm['sample_count']} | "
                f"{sm['best_source']} | {UNIT_PROFILES[sm['best_unit']]['name'] if sm['best_unit'] else '-'} |")
    add("")

    # 6. Tier 1 — Do now
    add("## 6. Tier 1 — Do now")
    add("")
    actions_t1 = [
        a for a in ctx["actions"]
        if a["tier"] == 1 and not a.get("collapsed_into_summary")
    ][:20]
    if not actions_t1:
        add("_No Tier-1 actions identified._")
    else:
        add("| # | Action | Coords | List | Current | Recommended | Reason | Daily Δ |")
        add("|--:|--------|--------|------|---------|-------------|--------|--------:|")
        for i, a in enumerate(actions_t1, 1):
            sm = a["slot"]
            cur = a.get("current_str", "—")
            rec = a.get("recommended_str", "—")
            # SPLIT_LIST has no per-coord target; show the list name in the coords cell.
            coords_cell = (
                "(list-level)" if a["action"] == "SPLIT_LIST"
                else f"({sm['coords'][0]},{sm['coords'][1]})"
            )
            add(f"| {i} | {a['action']} | {coords_cell} | "
                f"{sm['list_name']} | {cur} | {rec} | {a['reason']} | "
                f"{a['expected_daily_delta_booty']:.0f} |")
    add("")

    # 7. Tier 2 — v3.3: distinct rows only (collapsed_into_summary excluded)
    add("## 7. Tier 2 — Worthwhile cleanup")
    add("")
    actions_t2 = [
        a for a in ctx["actions"]
        if a["tier"] == 2 and not a.get("collapsed_into_summary")
    ][:TIER_2_VISIBLE_CAP]
    if ctx.get("collapsed_summaries"):
        add("**Bulk-action collapses** (10+ identical actions grouped):")
        add("")
        for cs in ctx["collapsed_summaries"]:
            sample = ", ".join(f"({c[0]},{c[1]})" for c in cs["sample_coords"])
            add(f"- **{cs['action']} × {cs['count']}** — reason: `{cs['reason_key']}`. "
                f"Sample coords: {sample}{' …' if cs['count'] > 5 else ''}")
        add("")
    if not actions_t2:
        add("_No Tier-2 actions identified (after collapse)._")
    else:
        add("| # | Action | Coords | List | Current | Recommended | Reason | Daily Δ |")
        add("|--:|--------|--------|------|---------|-------------|--------|--------:|")
        for i, a in enumerate(actions_t2, 1):
            sm = a["slot"]
            cur = a.get("current_str", "—")
            rec = a.get("recommended_str", "—")
            # SPLIT_LIST has no per-coord target; show the list name in the coords cell.
            coords_cell = (
                "(list-level)" if a["action"] == "SPLIT_LIST"
                else f"({sm['coords'][0]},{sm['coords'][1]})"
            )
            add(f"| {i} | {a['action']} | {coords_cell} | "
                f"{sm['list_name']} | {cur} | {rec} | {a['reason']} | "
                f"{a['expected_daily_delta_booty']:.0f} |")
    add("")

    # 8. Tier 3
    add("## 8. Tier 3 — Optional polish")
    add("")
    actions_t3 = [
        a for a in ctx["actions"]
        if a["tier"] == 3 and not a.get("collapsed_into_summary")
    ]
    add(f"_{len(actions_t3)} optional actions identified — first {min(50, len(actions_t3))} below; full list in JSON._")
    add("")
    for a in actions_t3[:50]:
        sm = a["slot"]
        add(f"- {a['action']} ({sm['coords'][0]},{sm['coords'][1]}) in `{sm['list_name']}` — {a['reason']}")
    add("")

    # 9. Per-village summary
    # v3.4 — precompute decaying-slot counts per village owner.
    decaying_per_village: dict[str, int] = defaultdict(int)
    for a in (ctx.get("throughput_drop_actions") or []):
        sm = a["slot"]
        owner_vid = sm.get("list_id")
        # Resolve owner village from the list owner_village_id via summary lookup
        for fl in ctx.get("existing_lists", []):
            if fl.get("id") == sm.get("list_id"):
                owner_v_label = ctx["village_label_by_vid"].get(fl.get("owner_village_id"), "?")
                decaying_per_village[owner_v_label] += 1
                break
    add("## 9. Per-village summary")
    add("")
    for label in ("V1", "V2", "V3", "V4", "V6"):
        b = BUDGET[label]
        coords = b["coords"]
        waves_here = ctx["waves_by_v"].get(label, [])
        # Committed troops per unit
        committed: dict[str, int] = defaultdict(int)
        for w in waves_here:
            committed[w["unit"]] += w["count"]
        add(f"### {label} ({coords[0]},{coords[1]})")
        add("")
        add("| Unit | Budget | Committed | Idle | %idle |")
        add("|------|------:|----------:|-----:|------:|")
        for u in ("t1", "t3", "t5", "t6", "t7", "t8"):
            bv = b.get(u, 0)
            if bv == 0:
                continue
            cv = committed.get(u, 0)
            idle = max(0, bv - cv)
            pct = (idle / bv * 100) if bv else 0
            warn = " ⚠️ >20% idle" if pct > 20 and bv >= 50 else ""
            add(f"| {UNIT_PROFILES[u]['name']} | {bv} | {cv} | {idle} | {pct:.0f}% |{warn}")
        # v3.4 — decaying-slots count for this village
        decay_count = decaying_per_village.get(label, 0)
        if decay_count > 0:
            add("")
            add(f"_Decaying slots (drop ≥{abs(THROUGHPUT_DROP_THRESHOLD)*100:.0f}%): **{decay_count}** — see Section 9b._")
        # Top 5 slots by daily delta for waves from this village
        candidates = [
            (a["expected_daily_delta_booty"], a)
            for a in ctx["actions"]
            if any(w["slot_id"] == a["slot"]["slot_id"] for w in waves_here)
        ]
        candidates.sort(key=lambda pair: pair[0], reverse=True)
        top5 = candidates[:5]
        if top5:
            add("")
            add("**Top 5 slots by daily Δ for this village:**")
            add("")
            for delta, a in top5:
                sm = a["slot"]
                add(f"- ({sm['coords'][0]},{sm['coords'][1]}) — {a['action']} — Δ ≈ {delta:.0f}/day")
        add("")
    add("### V5 (45,90)")
    add("")
    add("- No actions, excluded from sourcing — live counts in Section 2 are diagnostics only.")
    add("")

    # 9b. Throughput-dropping slots (v3.4)
    throughput_drops = ctx.get("throughput_drop_actions") or []
    historical_count = ctx.get("historical_snapshot_count", 0)
    if throughput_drops:
        add("## 9b. Throughput-dropping slots (last 14d)")
        add("")
        add(f"From {historical_count} historical snapshots loaded. "
            f"Slots whose 7-day raid rate dropped ≥{abs(THROUGHPUT_DROP_THRESHOLD)*100:.0f}% "
            f"below their 7-14d baseline (≥{THROUGHPUT_MIN_SAMPLES} sample pairs required).")
        add("")
        add("| # | List | Coords | Recent/24h | Baseline/24h | Drop % | Booty lost/day | Suggested action |")
        add("|--:|------|--------|-----------:|-------------:|-------:|---------------:|------------------|")
        sorted_drops = sorted(
            throughput_drops,
            key=lambda a: -a["expected_daily_delta_booty"],
        )[:20]
        for i, a in enumerate(sorted_drops, 1):
            sm = a["slot"]
            e = a.get("extra") or {}
            recent = e.get("raids_per_24h_recent")
            baseline = e.get("raids_per_24h_baseline")
            pct = e.get("pct_change")
            recent_s = f"{recent:.2f}" if recent is not None else "0.00"
            baseline_s = f"{baseline:.2f}" if baseline is not None else "—"
            pct_s = f"{pct*100:.0f}%" if pct is not None else "—"
            add(f"| {i} | {sm.get('list_name','?')} | "
                f"({sm['coords'][0]},{sm['coords'][1]}) | {recent_s} | {baseline_s} | "
                f"{pct_s} | {a['expected_daily_delta_booty']:.0f} | "
                f"investigate (chiefed / depleted / drift) |")
        if len(throughput_drops) > 20:
            add(f"\n_+{len(throughput_drops) - 20} more in JSON appendix._")
        add("")

    # 10. Stacked targets
    add("## 10. Stacked-target plans")
    add("")
    stacked = ctx["stacked_targets"]
    if not stacked:
        add("_No multi-wave stacks above the 50-loot threshold._")
    else:
        add("| Target | Wave 1 | Wave 2 | Wave 3 | Slow ram? | Slow cata? | Cycle loot |")
        add("|--------|--------|--------|--------|-----------|------------|-----------:|")
        for st in stacked[:40]:
            w1 = st["waves"][0] if len(st["waves"]) >= 1 else None
            # Waves already sorted by arrival_min in plan_strike_waves
            sorted_waves = sorted(st["waves"], key=lambda w: w.get("arrival_min", w.get("round_trip_min", 0)))
            w1 = sorted_waves[0] if len(sorted_waves) >= 1 else None
            w2 = sorted_waves[1] if len(sorted_waves) >= 2 else None
            w3 = sorted_waves[2] if len(sorted_waves) >= 3 else None
            def fmt_w(w):
                if not w:
                    return "—"
                src = w.get("source") or next(
                    (k for k in ctx['waves_by_v'] if w in ctx['waves_by_v'][k]), "?"
                )
                arr = w.get("arrival_min", 0)
                return (f"{w['count']}× {UNIT_PROFILES[w['unit']]['name']} from {src} "
                        f"(arr {arr}m, loot {w['expected_loot']})")
            add(f"| ({st['coords'][0]},{st['coords'][1]}) | {fmt_w(w1)} | {fmt_w(w2)} | {fmt_w(w3)} | "
                f"{'yes' if st['has_ram'] else 'no'} | {'yes' if st['has_cata'] else 'no'} | "
                f"{st['total_loot']:.0f} |")
    add("")

    # 11. Auto-scout list
    add("## 11. Auto-scout list proposal")
    add("")
    a_s = ctx["auto_scout"]
    add(f"- **List name:** `{a_s['list_name']}`")
    add(f"- **Source village:** {a_s['source_village']} (vid={a_s['owner_village_id']})")
    add(f"- **Slots:** {a_s['slot_count']} of {AUTO_SCOUT_CAP} cap")
    add(f"- **Scouts/slot:** {a_s['scouts_per_slot']} · **Total committed:** {a_s['total_scouts_committed']}")
    add(f"- **Reserve:** V1 has {BUDGET['V1']['t4']} scouts, {BUDGET['V1']['t4'] - a_s['total_scouts_committed']} in reserve after commitment")
    add("")
    add("**Top 10 targets to scout:**")
    add("")
    add("| # | Coords | Reason | Score proxy |")
    add("|--:|--------|--------|------------:|")
    for i, slot in enumerate(a_s["slots"][:10], 1):
        c = slot["coords"]
        add(f"| {i} | ({c[0]},{c[1]}) | {slot['reason']} | {slot['score']:.2f} |")
    add("")
    add("_Activate this list and click Send All — Travian auto-converts pure-scout slots to scout missions. Results feed the next run's defense intel._")
    add("")

    # 12. Slow-wave lists
    add("## 12. Slow-wave list proposals — **OPT-IN ONLY**")
    add("")
    add("> ⚠️ **WARNING:** these lists default to **all slots INACTIVE**. Trigger All will NOT fire them. Activate individual slots manually when you specifically want to slow a target's rebuild.")
    add("")
    for name in ("V3-SLOW-RAM-CARRY", "V3-SLOW-CATA-CARRY"):
        prop = ctx["slow_waves"][name]
        slow_unit = "Ram" if "RAM" in name else "Catapult"
        add(f"### `{name}`")
        add("")
        add(f"- **Source:** V3 · **Owner-vid:** {prop['owner_village_id']}")
        add(f"- **Slots:** {prop['slot_count']} of {SLOW_WAVE_CAP} cap")
        add(f"- **Composition per slot:** {SLOW_WAVE_AXE_COUNT}× Axe + 1× {slow_unit}")
        if "rams_committed_if_all_active" in prop:
            add(f"- **Rams committed if all active:** {prop['rams_committed_if_all_active']} of {BUDGET['V3']['t7']} budget")
        if "catas_committed_if_all_active" in prop:
            add(f"- **Catapults committed if all active:** {prop['catas_committed_if_all_active']} of {BUDGET['V3']['t8']} budget")
        add(f"- **Axes committed if all active:** {prop['axes_committed_if_all_active']}")
        add("")
        if not prop["slots"]:
            add("_No eligible verified-history targets at this time._")
        else:
            add("| # | Coords | raids | booty | icon | def_proxy | round-trip |")
            add("|--:|--------|------:|------:|------|----------:|-----------:|")
            for i, slot in enumerate(prop["slots"], 1):
                c = slot["coords"]
                add(f"| {i} | ({c[0]},{c[1]}) | {slot['total_raids']} | "
                    f"{slot['total_booty']} | {slot['last_raid_icon']} | "
                    f"{slot['defense_proxy']} | {slot['round_trip_min']}m |")
        add("")

    # 13. Excluded high-defense targets
    add("## 13. Excluded high-defense targets (def > 500)")
    add("")
    excluded = [sm for sm in ctx["slot_ms"] if sm["defense_proxy"] > DEFENSE_HARD_LIMIT]
    if not excluded:
        add("_None._")
    else:
        add("| Coords | def_proxy | last_icon | intel age (h) | current list |")
        add("|--------|----------:|-----------|-------------:|-------------|")
        for sm in excluded:
            age = f"{sm['last_raid_age_h']:.1f}" if sm["last_raid_age_h"] is not None else "—"
            add(f"| ({sm['coords'][0]},{sm['coords'][1]}) | {sm['defense_proxy']} | "
                f"{sm['last_raid_icon']} | {age} | {sm['list_name']} |")
    add("")

    # 14. Re-scout queue (overflow from auto-scout)
    add("## 14. Re-scout queue (overflow not in auto-scout list)")
    add("")
    overflow = ctx["auto_scout"]["overflow_for_re_scout_queue"]
    if not overflow:
        add("_No overflow — auto-scout list captured all candidates._")
    else:
        for i, slot in enumerate(overflow[:20], 1):
            c = slot["coords"]
            add(f"- ({c[0]},{c[1]}) — {slot['reason']} — score proxy {slot['score']:.2f}")
        if len(overflow) > 20:
            add(f"- _+{len(overflow) - 20} more in JSON appendix._")
    add("")

    # 15. Fresh-list appendix
    add("## 15. Fresh-list appendix")
    add("")
    fresh = ctx["fresh_lists"]
    if not fresh:
        add("_No new lists needed beyond the auto-scout and slow-wave proposals above._")
    else:
        for fl in fresh:
            add(f"- `{fl['name']}` — source {fl['source']} — unit {fl['unit']} — {fl['slot_count']} slots — {fl['rationale']}")
    add("")

    # 16. Cross-list trigger order
    add("## 16. Cross-list trigger order recommendation")
    add("")
    add("Send-All has no documented cross-list ordering. Manually trigger in this order:")
    add("")
    add("1. **All `V*-HIGH-*` lists** (fire every cycle; wave spacing is internal to each plan)")
    add("2. **All `V*-MID-*` lists**")
    add("3. **`V*-AUTO-SCOUT`** (refresh intel after primary strikes commit)")
    add("4. **All `V*-INACTIVE-*` lists** (incl. `*-Top` from any SPLIT_LIST)")
    add("5. **SKIP: `V*-DEAD`, `V*-SLOW-*`** — deactivated by design")
    add("")

    # 16b. Per-list sort recommendations (Bug 5 Part A)
    sort_recs = ctx.get("list_sort_recommendations") or {}
    if sort_recs:
        add("## 16b. Within-list sort recommendation (Bug 5, v3.2)")
        add("")
        add("Travian fires slots in displayed order, top-to-bottom. The right within-list sort puts "
            "high-throughput slots at the top so they fire every cycle. Click the column header "
            "once per list per session to apply.")
        add("")
        add("| List | Slots | Recommended sort | Rationale |")
        add("|------|------:|------------------|-----------|")
        sorted_recs = sorted(sort_recs.values(), key=lambda r: (-r["slot_count"], r["list_name"]))
        sort_label = {
            "total_bounty_desc": "Total bounty ↓",
            "last_bounty_desc": "Last bounty ↓",
            "next_attack_asc": "Next attack ↑",
        }
        for r in sorted_recs:
            add(f"| {r['list_name']} | {r['slot_count']} | "
                f"{sort_label.get(r['recommended_sort'], r['recommended_sort'])} | "
                f"{r['rationale']} |")
        add("")

    # 16c. SPLIT_LIST proposals (Bug 6)
    split_props = ctx.get("split_actions") or []
    if split_props:
        add("## 16c. List splits recommended (Bug 6, v3.2)")
        add("")
        add("Tail-starved lists (≥30 slots, <8 raids/slot lifetime, top-20 carrying ≥50% of value) "
            "are split so the highest-value 20 slots fire every cycle instead of starving in the tail.")
        add("")
        for sp in split_props:
            ex = sp["extra"]
            add(f"### `{ex['list_name']}` → `{ex['new_top_name']}` + `{ex['new_tail_name']}`")
            add("")
            add(f"- **Active slots now:** {ex['active_slot_count']} ({ex['raids_per_slot']:.1f} raids/slot lifetime)")
            add(f"- **Top-20 share of value:** {ex['top_share_pct']:.0f}%")
            add(f"- **Estimated daily gain:** ~{sp['expected_daily_delta_booty']:.0f} res/day from doubled top-20 fire frequency")
            add("")
            add("**Operator steps:**")
            add(f"1. Create new list `{ex['new_top_name']}` in the same owner village.")
            add(f"2. Duplicate (Travian: duplicate-entry feature) the top-20 coords into the new list:")
            preview = ", ".join(f"({c[0]},{c[1]})" for c in ex['top_20_coords'][:10])
            tail_more = "" if len(ex['top_20_coords']) <= 10 else f" … (+{len(ex['top_20_coords']) - 10} more)"
            add(f"   {preview}{tail_more}")
            add(f"3. Rename original list to `{ex['new_tail_name']}`, OR delete the duplicated top-20 "
                f"entries from the original, leaving the tail.")
            add(f"4. In Section 16's trigger order, the `*-Top` list now fires before the `*-Tail`.")
            add("")
    add("")

    # 17. Action checklist
    add("## 17. Action checklist (grouped by list)")
    add("")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in ctx["actions"]:
        if a["tier"] > 2:
            continue
        if a.get("collapsed_into_summary"):
            continue  # bulk-collapsed rows go into the summary block, not the checklist
        grouped[a["slot"]["list_name"]].append(a)
    for list_name in sorted(grouped):
        add(f"### `{list_name}`")
        add("")
        for a in grouped[list_name]:
            sm = a["slot"]
            add(f"- **{a['action']}** ({sm['coords'][0]},{sm['coords'][1]}) — {a['reason']}")
        add("")

    # 18. Methodology
    add("## 18. Methodology notes")
    add("")
    add("### Scoring formula (`score_for_unit`)")
    add("")
    add("Custom — the POC additions described in the brief (`UnitProfile`, `score_for_unit`, "
        "`select_waves_for_target`, `_synth_state_from_slot`) were **not present** in "
        "`raid_analyzer_service.py` at run time, so a per-unit scoring function was implemented "
        "from scratch and is documented here.")
    add("")
    add("```")
    add("base = expected_loot / round_trip_hours")
    add("expected_loot = min(carry × ceil(eff_avg_loot / carry), eff_avg_loot)")
    add("eff_avg_loot  = avg_loot × min(1, sample_count / 10)")
    add("round_trip_h  = 2 × distance / speed")
    add("defense_penalty = 1 / (1 + defense_proxy / 100)")
    add("score = base × defense_penalty")
    add("```")
    add("")
    add("**Why this over `calculate_score_v2`:** the existing scorer is Clubs-only and assumes "
        "live scout data (`TargetVillageState`). The diff needs per-unit, per-source comparison "
        "from farm-list slot history alone — `last_raid.icon`, `total_booty`, `total_raids`. "
        "A simpler loot-per-hour metric with a defense penalty is appropriate and stays close "
        "to the spirit of the existing scorer's loot/round-trip core.")
    add("")
    add("### Defense proxy")
    add("")
    add("`no_loss → 10`, `some_loss → 200`, `all_dead → 1000`, `unknown → 0`. Used both for "
        "scoring (defense penalty) and for hard cutoffs (DELETE if > 500; slow-wave eligibility "
        "if ≤ 100).")
    add("")
    add("### Owner-village constraint (v3.3, Bug A)")
    add("")
    add("Each farm list is bound to a single owner village in Travian — only that village's "
        "troops can fire from the list. The v3.3 wave planner now enforces this strictly: every "
        "wave in `stacked_targets` has `source == owner_village_label(list_id)`. Cross-village "
        "stacking is surfaced as **ADD_NEW_SLOT** diff actions that propose adding the target to "
        "additional lists owned by other villages (the operator manually creates those slots).")
    add("")
    add(f"- HIGH: {WAVE_CAPS['HIGH']} waves max within the owner village's unit pool.")
    add(f"- MID: {WAVE_CAPS['MID']} waves max.")
    add(f"- INACTIVE-low: {WAVE_CAPS['INACTIVE-low']} waves max.")
    add(f"- Premium units (TK / Paladin) allowed in **all buckets**: with zero-defense farms, "
        f"premium has no marginal combat cost.")
    add(f"- Per-wave troop count: `troops = max(1, ceil(expected_loot × 1.10 / carry))` — 10% "
        f"safety margin so a one-percent above-average refill is covered without re-tuning.")
    add("- Single-unit villages (V4, V6 — Clubs only) can only do 1-wave plans within their own "
        "lists; multi-wave stacks for V4/V6 slots show up as ADD_NEW_SLOT recommendations for "
        "other villages.")
    add("")
    add("**Implications for V3 idle %:** the V3 budget (2,250 Axes + 1,600 TK) far exceeds what "
        f"its own list-slots can consume at empirical loot levels (p50 avg_loot ≈ 19 → most waves "
        f"are 1 troop). Reducing V3 idle requires the operator to act on ADD_NEW_SLOT "
        f"recommendations — i.e. add V3 slots for high-value coords currently in V1/V2/V4/V6 lists.")
    add("")
    add("### Economic floor (v3.3, Bug C)")
    add("")
    add(f"Slots with `sample_count ≥ 3 AND avg_loot < {ECONOMIC_FLOOR_RES} res` are marked "
        f"DEACTIVATE with reason `below_economic_floor`. Sending even 1 troop on a sustained-low-haul "
        f"farm ties up troop-time for negligible return. The {ECONOMIC_FLOOR_RES}-res threshold is "
        f"a service-level default chosen to roughly equal the cost of one round-trip-hour of one "
        f"strike unit at typical farm distances. Document in Methodology if changed.")
    add("")
    add("### Chronological wave numbering (v3.3, Bug B)")
    add("")
    add("Waves are picked in efficiency order but renumbered by arrival time after selection: "
        "`wave_index=1` is always the earliest-arriving wave on the target. Expected loot is "
        "then recomputed chronologically using **cumulative carry taken** across all prior "
        "waves (not just the immediate predecessor), guaranteeing a monotonically non-increasing "
        "loot sequence: each subsequent wave can only haul what the cranny has refilled minus "
        "what every prior wave took.")
    add("")
    add("### Wave-stacker — cranny refill model (v3.1+, Bugs 2 & B)")
    add("")
    add(f"Wave 1 hauls the empirical `avg_loot` of the slot (cranny state on first arrival). "
        f"Wave N (N ≥ 2) hauls only what the cranny refilled between the prior wave's arrival "
        f"and this wave's arrival, capped at `avg_loot` (the cranny size). The refill rate is "
        f"a service-level default — **{DEFAULT_FARM_REFILL_PER_HOUR} res/hour combined** — chosen "
        f"as a conservative estimate for a typical farm village at this server stage. A wave "
        f"is skipped when its expected loot falls below **carry × 0.5 res** (Clubs 30, Axe 25, "
        f"TK 40, Paladin 55) — per-unit threshold ensures the round-trip cost doesn't exceed return.")
    add("")
    add("```")
    add(f"DEFAULT_FARM_REFILL_PER_HOUR = {DEFAULT_FARM_REFILL_PER_HOUR}")
    add("")
    add("def expected_loot_for_wave_n(avg_loot, wave_index, prior_arrival_min, this_arrival_min):")
    add("    if wave_index == 1: return avg_loot")
    add(f"    return min((this_arrival_min - prior_arrival_min)/60 * {DEFAULT_FARM_REFILL_PER_HOUR}, avg_loot)")
    add("```")
    add("")
    add(f"Candidate enumeration: for every slot, every (source_village, strike_unit) tuple is "
        f"evaluated — all five raid sources (V1, V2, V3, V4, V6), each with its allowed units. "
        f"Selection is greedy by *refill-adjusted* efficiency (`expected_loot / round_trip_hours`), "
        f"respecting the {MIN_SPACING_MINUTES}-minute spacing rule by arrival time. This lets "
        f"V3's bulk supply cascade onto targets where V1/V2 already fired the early wave, and "
        f"naturally produces wave-2/wave-3 entries from longer-trip villages.")
    add("")
    add("### Comparator — primary-unit semantics (v3.1, Bug 1 fix)")
    add("")
    add("The previous comparator read `slot.troops[recommended_unit]` to compute the BOOST/SHRINK "
        "diff, which was zero for any slot whose existing allocation used a different unit. The "
        "v3.1 comparator operates on the slot's **primary existing unit** (highest count) and "
        "branches into five distinct cases. Thresholds:")
    add("")
    add(f"- Same unit BOOST: `recommended > existing × {SAME_UNIT_BOOST_MULT}`")
    add(f"- Same unit SHRINK: `recommended < existing × {SAME_UNIT_SHRINK_MULT}`")
    add(f"- Over-allocation SHRINK: `existing_carry > avg_loot × {OVER_ALLOC_MULT}` "
        f"→ shrink the existing unit to `ceil(avg_loot / carry)`")
    add(f"- Different-unit KEEP: `existing_carry ≥ avg_loot × {EXISTING_OK_MULT}`")
    add(f"- CHANGE_UNIT: `recommended_carry > existing_carry × {CHANGE_UNIT_MULT}` "
        f"AND `existing_carry < avg_loot × {EXISTING_OK_MULT}`")
    add(f"- Otherwise → KEEP (no churn unless there's clear lift)")
    add("")
    add("### Per-target consolidation (v3.1, Bug 3 fix)")
    add("")
    add("Yesterday's run produced contradictory recommendations for targets that appeared in "
        "multiple lists (e.g., (31,83) in 11 different slots). The v3.1 pass groups actions by "
        "(x, y) and picks the slot with the longest history as the primary; duplicates become "
        "DEACTIVATEs with `reason='duplicate_target — consolidated to <primary list>'`. "
        "Slow-wave lists (`*-SLOW-*`) are intentionally on the same coords as strike-wave slots "
        "and are skipped from consolidation. Genuine multi-wave intent now lives in the "
        "Stacked-target plans section, not in scattered list duplicates.")
    add("")
    add("### Tier-2 cap and bulk collapse (v3.1, Bug 4 fix)")
    add("")
    add(f"Tier 2 visible rows are capped at **{TIER_2_VISIBLE_CAP}**; overflow demotes to Tier 3. "
        f"Groups of ≥{COLLAPSE_BULK_THRESHOLD} same-(action, reason) rows collapse into a single "
        f"summary entry with a count and sample coords. Full details remain in the JSON appendix.")
    add("")
    add(f"### 14K soft proxy — removed in v3.1")
    add("")
    add(f"The prior 14,000-troop soft ceiling and bottom-up trimming were removed. Operator's "
        f"current army is nowhere near the {HARD_TROOP_CAP} hard cap, and stacking waves is the "
        f"explicit priority. The cap-check display in Section 2 is now info-only; if grand-total "
        f"exceeds 70% of the hard cap the script emits a one-line nudge to reintroduce a soft "
        f"proxy next run.")
    add("")
    add("### Bucket cut-offs (HIGH 20%, MID 60%, INACTIVE-low 20%)")
    add("")
    add("After DEACTIVATED slots are removed (def>500, stale>7d, sample<3, depleted-not-CT2/3, "
        "unscorable), the remaining slots are sorted by canonical score and split by absolute "
        "rank — top 20% → HIGH, next 60% → MID, bottom 20% → INACTIVE-low.")
    add("")
    add("### Slow-wave list composition")
    add("")
    add(f"Each slot in `V3-SLOW-RAM-CARRY` and `V3-SLOW-CATA-CARRY` is **{SLOW_WAVE_AXE_COUNT}× Axe + 1× Ram/Cata**. "
        f"The ram/cata throttles the wave speed (Ram 4 f/h, Cata 3 f/h) so it lands hours after the "
        f"strike wave; the Axes haul the loot of a (presumably refilled) cranny. Eligibility is strict: "
        f"sample ≥ 10, last_raid.icon == no_loss, defense_proxy ≤ {SLOW_WAVE_DEF_LIMIT}.")
    add("")
    add("### CT2/CT3 detection")
    add("")
    add("A slot with `total_raids ≥ 3 AND total_booty == 0` is flagged `FLAG_CT2_CT3` rather than "
        "auto-DELETE'd. Real depletion produces some non-zero history before drying up; persistent "
        "zero is more often pushing-protection. The operator must verify in the latest battle report "
        "(Reports → Farm Lists) for the pushing-protection icon/text.")
    add("")
    add(f"### Existing analyzer methods reused")
    add("")
    add("- `travian_distance` (mathematical formula — re-implemented locally to avoid importing the service module from a script under stealth constraints)")
    add("- `DEFENSE_PROXY` mapping — matches the prior POC run's convention")
    add(f"- Slot data shape from `GET /api/farm/lists/{{id}}` — the only source for empirical "
        f"per-slot history")
    add("- `POST /api/reports/analyze` for gap-target discovery (per source village)")
    add("")
    add("### Rebalance pass — Path 3 (v4.0, opt-in via `--rebalance`)")
    add("")
    add("Target-centric replanner that ignores the existing farm-list structure and ")
    add("re-decides every coord's home from scratch. Objective:")
    add("")
    add("    0.80 × normalised(expected_raids_per_day)  +  0.20 × normalised(expected_daily_booty)")
    add("")
    add("Frequency dominates because the operator runs Start All at irregular cadences ")
    add("(roughly 25 min to 3 hours). `expected_raids_per_day` is a weighted sum across ")
    add("the operator-stated cadence distribution:")
    add("")
    add("    (32.5 min, weight 0.60)  →  bucket A — fast cadence")
    add("    (82.5 min, weight 0.30)  →  bucket B — medium cadence")
    add("    (150  min, weight 0.10)  →  bucket C — slow cadence")
    add("")
    add("At each bucket the slot fires once per `max(round_trip, cadence)` minutes. ")
    add("Changing these constants reshapes which (village, unit) pairing wins for a ")
    add("given coord; surface any change here when tuned.")
    add("")
    add("Dead-farm verdict (any condition triggers `RELOCATE_TO_DEAD` → `V{n}-DEAD`):")
    add("")
    add("- `avg_loot < 30` with `total_raids ≥ 5`")
    add("- last raid > 14 days ago AND `avg_loot < 50`")
    add("- `max_def_proxy > 500` (defended)")
    add("- pushing-protection (CT2/CT3) suspect")
    add("")
    add("DEAD lists are per-owner-village (V1-DEAD, V2-DEAD, …) so the operator's mental ")
    add("model of which village owns a slot survives the rebalance. The optimizer never ")
    add("DELETEs dead farms — it only proposes moves; the operator manually creates the ")
    add("DEAD list and leaves it deactivated.")
    add("")

    # 19. Rebalance plan — opt-in (--rebalance)
    if ctx.get("rebalance_enabled"):
        _render_rebalance_section(add, ctx)

    # Verification errors
    if ctx["verify_errors"]:
        add("## Verification errors (DRAFT)")
        add("")
        for err in ctx["verify_errors"]:
            add(f"- {err}")
        add("")

    add("---")
    add(f"_Generated by `scripts/raid_optimizer_diff_v3.py` at {ctx['timestamp']}. "
        f"JSON appendix: `{ctx['json_path']}`._")
    return "\n".join(L) + "\n"


# ─── Step A wrapper — Gap target discovery via /api/reports/analyze ───────


async def fetch_gap_targets(api: ApiClient, villages: dict[str, Any], existing_coords: set[tuple[int, int]]) -> list[dict[str, Any]]:
    """Run /api/reports/analyze for each source village; return scored targets not in any list."""
    gaps: dict[tuple[int, int], dict[str, Any]] = {}
    for label in SOURCE_VILLAGES:
        v = villages["by_coords"].get(BUDGET[label]["coords"])
        if v is None:
            continue
        try:
            payload = await api.post("/api/reports/analyze", {
                "village_id": v["id"],
                "min_resources": 100,
                "max_report_age_hours": 168,  # 7 days
                "max_pages": 5,
                "radius": 30,
                "stale_hours": 168,
            })
        except Exception:
            continue
        for t in payload.get("targets", []):
            state = t.get("state") or {}
            rec = t.get("recommendation") or {}
            x = state.get("x")
            y = state.get("y")
            if x is None or y is None:
                continue
            coords = (int(x), int(y))
            if coords in existing_coords:
                continue
            if coords in gaps:
                # keep highest-score variant
                if rec.get("score", 0) <= gaps[coords].get("score", 0):
                    continue
            gaps[coords] = {
                "coords": coords,
                "score": float(rec.get("score") or 0.0),
                "discovered_from_village": label,
                "est_loot": rec.get("est_loot"),
                "village_name": state.get("village_name", ""),
                "player_name": state.get("player_name", ""),
            }
    return list(gaps.values())


# ─── Path 3 rebalance helpers (v4.0) ─────────────────────────────────────


def _render_rebalance_section(add: Any, ctx: dict[str, Any]) -> None:
    """Render `## 19. Rebalance plan (Path 3 full rewrite)` and its 19a/19b/19c subsections.

    Spec section number 17 is already used by the v3.4 Action checklist; this
    section is placed at the end of the report as 19 so the existing numbering
    survives.
    """
    summary = ctx.get("rebalance_summary") or {}
    actions: list[dict[str, Any]] = ctx.get("rebalance_actions") or []
    post_structure: dict[str, dict[str, Any]] = ctx.get("rebalance_post_structure") or {}

    add("## 19. Rebalance plan (Path 3 full rewrite)")
    add("")
    add("Opt-in pass (gated by `--rebalance`). Re-decides every target's home list,")
    add("village, unit and count under the objective:")
    add("")
    add("    0.80 × normalised(expected_raids_per_day)")
    add("    + 0.20 × normalised(expected_daily_booty)")
    add("")
    add("Dead farms are routed to per-owner-village `V{n}-DEAD` lists that the")
    add("operator manually creates and leaves deactivated.")
    add("")

    # 19a — Summary
    delta = (
        float(summary.get("post_rebalance_estimated_daily_booty", 0.0))
        - float(summary.get("current_estimated_daily_booty", 0.0))
    )
    add("### 19a. Summary")
    add("")
    add(f"- Targets analyzed: **{summary.get('targets_analyzed', 0)}**")
    add(f"- Dead-farm relocations: **{summary.get('relocated_to_dead', 0)}** "
        f"(to `V{{n}}-DEAD` lists)")
    add(f"- Active relocations: **{summary.get('moved_to_active', 0)}** "
        f"(to optimal HIGH/MID/INACTIVE lists)")
    new_lists = summary.get("new_lists_to_create") or []
    add(f"- New lists to create: **{len(new_lists)}**")
    if new_lists:
        add(f"  - {', '.join(f'`{n}`' for n in new_lists)}")
    add(f"- Current estimated daily booty: **{summary.get('current_estimated_daily_booty', 0):.0f} res**")
    add(f"- Post-rebalance estimated: **{summary.get('post_rebalance_estimated_daily_booty', 0):.0f} res** "
        f"(delta **{delta:+.0f}**, **{summary.get('expected_lift_pct', 0):+.1f}%**)")
    if summary.get("unplaceable_as_dead"):
        add(f"- Unplaceable targets routed to DEAD (no feasible village/unit fit): "
            f"**{summary['unplaceable_as_dead']}**")
    add("")

    # 19b — Phased execution
    dead_actions = [a for a in actions if a["action"] == "RELOCATE_TO_DEAD"]
    move_actions = [a for a in actions if a["action"] == "MOVE_SLOT"]
    high_mid = [
        a for a in move_actions
        if (a.get("extra") or {}).get("target_list_role") in ("HIGH", "MID")
    ]
    inactive_moves = [
        a for a in move_actions
        if (a.get("extra") or {}).get("target_list_role") == "INACTIVE"
    ]

    add("### 19b. Phased execution plan")
    add("")
    add("Execute in three phases across multiple sessions. Each phase groups its")
    add("actions by destination list so the operator opens each list once.")
    add("")

    _render_rebalance_phase(
        add,
        phase_title="Phase A — Dead-farm cleanup (largest, lowest stakes)",
        time_estimate_minutes=max(30, int(round(len(dead_actions) * 0.5))) if dead_actions else 0,
        actions=dead_actions,
        group_by_destination=True,
    )
    _render_rebalance_phase(
        add,
        phase_title="Phase B — Build HIGH / MID lists (highest value)",
        time_estimate_minutes=max(90, int(round(len(high_mid) * 1.0))) if high_mid else 0,
        actions=high_mid,
        group_by_destination=True,
    )
    _render_rebalance_phase(
        add,
        phase_title="Phase C — Build INACTIVE / Tail lists (long tail, opportunistic)",
        time_estimate_minutes=max(45, int(round(len(inactive_moves) * 0.5))) if inactive_moves else 0,
        actions=inactive_moves,
        group_by_destination=True,
    )

    # 19c — Post-rebalance structure preview
    add("### 19c. Final structure preview")
    add("")
    if not post_structure:
        add("_(no placements; see Phase A for dead routing)_")
        add("")
    else:
        for v_label in sorted(post_structure):
            entry = post_structure[v_label]
            add(f"**{v_label}**")
            add("")
            for lst in entry.get("lists") or []:
                add(f"- `{lst['name']}` — role {lst['role']}, {lst['slot_count']} slots")
            add("")


def _render_rebalance_phase(
    add: Any,
    *,
    phase_title: str,
    time_estimate_minutes: int,
    actions: list[dict[str, Any]],
    group_by_destination: bool,
) -> None:
    """Render one Phase A/B/C block with a destination-grouped action table."""
    add(f"#### {phase_title}")
    add("")
    if not actions:
        add("_(no actions in this phase)_")
        add("")
        return
    add(f"- Actions: **{len(actions)}**")
    add(f"- Time estimate: ~**{time_estimate_minutes} min**")
    add("")

    if group_by_destination:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in actions:
            dest = (a.get("extra") or {}).get("recommended_list_name") or "?"
            grouped[dest].append(a)
        for dest in sorted(grouped):
            add(f"##### → `{dest}`")
            add("")
            add("| Coord | Target | Current list | Reason / composition |")
            add("|---|---|---|---|")
            for a in grouped[dest]:
                slot = a.get("slot") or {}
                coord = slot.get("coords") or ("?", "?")
                extra = a.get("extra") or {}
                tname = extra.get("target_name") or "?"
                cur = extra.get("current_list_name") or "?"
                if a["action"] == "MOVE_SLOT":
                    composition = (
                        f"{extra.get('recommended_count', '?')}× "
                        f"{extra.get('recommended_unit_display', '?')} "
                        f"(rt {extra.get('round_trip_min', '?')}m, "
                        f"score {extra.get('objective_score', 0):.2f})"
                    )
                else:
                    composition = f"DEAD: {a.get('reason') or ''}"
                add(f"| ({coord[0]},{coord[1]}) | {tname} | `{cur}` | {composition} |")
            add("")


def _run_rebalance_pass(
    slot_ms: list[dict[str, Any]],
    village_label_by_vid: dict[int, str],
    now_unix: float,
) -> tuple[
    RebalancePlan,
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    """Execute the full Path 3 pass and return (plan, actions, summary, post_structure).

    Builds village positions and troop supplies from the global BUDGET dict
    (live counts at mission time, the same data the rest of the script uses),
    runs ``plan_rebalance``, then converts placements/dead-decisions into the
    MOVE_SLOT / RELOCATE_TO_DEAD action dicts the rest of the pipeline expects.
    """
    village_positions: list[VillagePosition] = []
    troop_supplies: dict[tuple[str, str], int] = {}
    for label in SOURCE_VILLAGES:
        cfg = BUDGET.get(label)
        if not cfg:
            continue
        coords = cfg.get("coords")
        if not coords:
            continue
        village_positions.append(VillagePosition(name=label, x=coords[0], y=coords[1]))
        for unit_id in ("t1", "t3", "t5", "t6"):
            troop_supplies[(label, unit_id)] = int(cfg.get(unit_id) or 0)

    inventory = build_target_inventory(slot_ms, village_label_by_vid)
    plan = plan_rebalance(
        inventory, village_positions, troop_supplies, now_unix=now_unix
    )

    # Index slot_ms by slot_id so we can attach the "primary" slot dict to each
    # action — the pipeline expects actions[i]["slot"] to exist.
    slot_by_id: dict[int, dict[str, Any]] = {
        s["slot_id"]: s for s in slot_ms if s.get("slot_id") is not None
    }

    # Compute current daily-booty estimate for a coord: avg_loot × 4 cycles/day
    # (matches the proxy in expected_daily_delta). Used as the subtraction
    # baseline so the action's delta is a comparable lift number.
    def _current_daily_booty(coord: tuple[int, int]) -> float:
        agg = inventory.get(coord)
        if agg is None:
            return 0.0
        return float(agg.avg_loot or 0.0) * 4.0

    actions: list[dict[str, Any]] = []

    for placement in plan.placements:
        primary_slot = _primary_slot_for_coord(
            placement.target_coord, placement.slot_instances, slot_by_id
        )
        if primary_slot is None:
            continue  # planner produced a placement for a coord with no usable slot
        current_list = primary_slot.get("list_name") or ""
        current_owner = village_label_by_vid.get(
            primary_slot.get("owner_village_id"), ""
        )
        # Don't emit MOVE_SLOT if the slot is already in its optimal list (the
        # spec's "anti-pattern" guard).
        if current_list == placement.target_list_name:
            continue
        unit_display = REBAL_UNIT_DISPLAY.get(placement.optimal_unit, placement.optimal_unit)
        delta_booty = max(
            0.0, placement.expected_daily_booty - _current_daily_booty(placement.target_coord)
        )
        action = {
            "slot": primary_slot,
            "action": "MOVE_SLOT",
            "reason": (
                f"rebalance: optimal home is {placement.target_list_name} "
                f"(score {placement.objective_score:.2f}; "
                f"rt {placement.round_trip_min:.0f}m; "
                f"~{placement.expected_raids_per_day:.1f} raids/day)"
            ),
            "tier": 1,
            "expected_daily_delta_booty": float(round(delta_booty)),
            "current_str": f"{current_list or '?'} ({current_owner or '?'})",
            "recommended_str": (
                f"{placement.optimal_count}× {unit_display} in {placement.target_list_name}"
            ),
            "extra": {
                "rebalance": True,
                "target_name": placement.target_name,
                "current_list_name": current_list,
                "current_list_owner": current_owner,
                "recommended_list_name": placement.target_list_name,
                "recommended_list_owner": placement.optimal_village,
                "recommended_unit": placement.optimal_unit,
                "recommended_unit_display": unit_display,
                "recommended_count": placement.optimal_count,
                "round_trip_min": round(placement.round_trip_min, 1),
                "expected_raids_per_day": round(placement.expected_raids_per_day, 2),
                "expected_daily_booty": round(placement.expected_daily_booty, 1),
                "objective_score": round(placement.objective_score, 3),
                "target_list_role": placement.target_list_role,
                "duplicate_slot_instances": [
                    {"list_name": ln, "slot_id": sid}
                    for ln, sid in placement.slot_instances
                    if sid != primary_slot.get("slot_id")
                ],
                "manual_steps": [
                    f"Open {current_list or '<source list>'}, find slot at "
                    f"{placement.target_coord} (target: {placement.target_name or '?'})",
                    f"If {placement.target_list_name} doesn't exist: create it "
                    f"(owner = {placement.optimal_village})",
                    f"Duplicate the entry to {placement.target_list_name} with composition: "
                    f"{placement.optimal_count}× {unit_display}",
                    f"Delete the original entry from {current_list or '<source list>'} "
                    f"and any duplicate entries in other lists for this coord",
                ],
            },
        }
        actions.append(action)

    for dead in plan.dead_decisions:
        primary_slot = _primary_slot_for_coord(
            dead.target_coord, dead.slot_instances, slot_by_id
        )
        if primary_slot is None:
            continue
        current_list = primary_slot.get("list_name") or ""
        current_owner = village_label_by_vid.get(
            primary_slot.get("owner_village_id"), ""
        )
        action = {
            "slot": primary_slot,
            "action": "RELOCATE_TO_DEAD",
            "reason": dead.reason,
            "tier": 2,
            "expected_daily_delta_booty": 0.0,
            "current_str": f"{current_list or '?'} ({current_owner or '?'})",
            "recommended_str": f"move to {dead.target_list_name} (deactivated)",
            "extra": {
                "rebalance": True,
                "target_name": dead.target_name,
                "current_list_name": current_list,
                "current_list_owner": current_owner,
                "recommended_list_name": dead.target_list_name,
                "recommended_list_owner": dead.primary_owner_village,
                "avg_loot": dead.avg_loot,
                "total_raids": dead.total_raids,
                "last_raid_days_ago": (
                    round(dead.last_raid_days_ago, 2)
                    if dead.last_raid_days_ago is not None
                    else None
                ),
                "duplicate_slot_instances": [
                    {"list_name": ln, "slot_id": sid}
                    for ln, sid in dead.slot_instances
                    if sid != primary_slot.get("slot_id")
                ],
                "manual_steps": [
                    f"If {dead.target_list_name} doesn't exist: create it "
                    f"(owner = {dead.primary_owner_village}) and leave it deactivated",
                    f"Duplicate the slot at {dead.target_coord} into {dead.target_list_name}",
                    f"Delete the original slot from {current_list or '<source list>'} "
                    f"and any duplicate entries in other lists for this coord",
                    f"Ensure {dead.target_list_name} is deactivated in Send All rotation",
                ],
            },
        }
        actions.append(action)

    # Summary + post-rebalance structure.
    current_total_daily = sum(_current_daily_booty(c) for c in inventory)
    post_total_daily = sum(p.expected_daily_booty for p in plan.placements)
    new_lists_needed = sorted({
        a["extra"]["recommended_list_name"] for a in actions
    })
    summary = {
        "targets_analyzed": len(inventory),
        "moved_to_active": sum(1 for a in actions if a["action"] == "MOVE_SLOT"),
        "relocated_to_dead": sum(1 for a in actions if a["action"] == "RELOCATE_TO_DEAD"),
        "new_lists_to_create": new_lists_needed,
        "current_estimated_daily_booty": float(round(current_total_daily, 1)),
        "post_rebalance_estimated_daily_booty": float(round(post_total_daily, 1)),
        "expected_lift_pct": (
            round((post_total_daily - current_total_daily) / current_total_daily * 100.0, 1)
            if current_total_daily > 0
            else 0.0
        ),
        "unplaceable_as_dead": plan.unplaceable_as_dead,
    }

    post_structure: dict[str, dict[str, Any]] = {}
    for p in plan.placements:
        v_entry = post_structure.setdefault(
            p.optimal_village, {"lists": {}}
        )
        l_entry = v_entry["lists"].setdefault(
            p.target_list_name,
            {"name": p.target_list_name, "role": p.target_list_role, "slot_count": 0},
        )
        l_entry["slot_count"] += 1
    for dead in plan.dead_decisions:
        if not dead.primary_owner_village:
            continue
        v_entry = post_structure.setdefault(dead.primary_owner_village, {"lists": {}})
        l_entry = v_entry["lists"].setdefault(
            dead.target_list_name,
            {"name": dead.target_list_name, "role": "DEAD", "slot_count": 0},
        )
        l_entry["slot_count"] += 1
    # Flatten the per-village list dicts into ordered lists for stable output.
    post_structure_flat: dict[str, dict[str, Any]] = {
        v: {"lists": sorted(entry["lists"].values(), key=lambda lst: lst["name"])}
        for v, entry in post_structure.items()
    }

    return plan, actions, summary, post_structure_flat


def _primary_slot_for_coord(
    coord: tuple[int, int],
    slot_instances: list[tuple[str, int]],
    slot_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Pick the slot-instance with the highest total_raids for a coord."""
    candidates: list[dict[str, Any]] = []
    for _list_name, sid in slot_instances:
        sm = slot_by_id.get(sid)
        if sm is not None and tuple(sm.get("coords") or ()) == coord:
            candidates.append(sm)
    if not candidates:
        return None
    return max(candidates, key=lambda s: int(s.get("total_raids") or 0))


def _build_rebalance_json(
    *,
    enabled: bool,
    summary: dict[str, Any] | None,
    actions: list[dict[str, Any]],
    post_structure: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    """Serialise the rebalance pass into the top-level rebalance_plan JSON key."""
    if not enabled or summary is None:
        return {"enabled": False}

    def _serialise(a: dict[str, Any]) -> dict[str, Any]:
        slot = a.get("slot") or {}
        coord = slot.get("coords")
        return {
            "tier": a.get("tier"),
            "action": a.get("action"),
            "coords": list(coord) if coord else None,
            "target_name": (a.get("extra") or {}).get("target_name"),
            "current_list_name": (a.get("extra") or {}).get("current_list_name"),
            "current_list_owner": (a.get("extra") or {}).get("current_list_owner"),
            "recommended_list_name": (a.get("extra") or {}).get("recommended_list_name"),
            "recommended_list_owner": (a.get("extra") or {}).get("recommended_list_owner"),
            "reason": a.get("reason"),
            "expected_daily_delta_booty": a.get("expected_daily_delta_booty"),
            "extra": a.get("extra"),
        }

    phase_a = [_serialise(a) for a in actions if a["action"] == "RELOCATE_TO_DEAD"]
    # LOCAL slots (V4/V5/V6/V7 short-range micro-raiders) fire every cycle,
    # same priority class as HIGH and MID; they go in Phase B alongside them.
    phase_b = [
        _serialise(a)
        for a in actions
        if a["action"] == "MOVE_SLOT"
        and (a.get("extra") or {}).get("target_list_role") in ("HIGH", "MID", "LOCAL")
    ]
    phase_c = [
        _serialise(a)
        for a in actions
        if a["action"] == "MOVE_SLOT"
        and (a.get("extra") or {}).get("target_list_role") == "INACTIVE"
    ]

    return {
        "enabled": True,
        "summary": summary,
        "phase_a_dead_relocations": phase_a,
        "phase_b_high_mid_moves": phase_b,
        "phase_c_inactive_moves": phase_c,
        "post_rebalance_structure": post_structure or {},
    }


def _print_rebalance_summary(
    summary: dict[str, Any],
    *,
    file: Any = sys.stderr,
) -> None:
    """Emit the operator-facing summary printout when --rebalance is on."""
    new_lists: list[str] = summary.get("new_lists_to_create") or []
    dead_lists = [name for name in new_lists if name.endswith("-DEAD")]
    other_lists = [name for name in new_lists if not name.endswith("-DEAD")]
    lift = summary.get("expected_lift_pct") or 0.0
    delta = (
        summary.get("post_rebalance_estimated_daily_booty", 0.0)
        - summary.get("current_estimated_daily_booty", 0.0)
    )
    n_dead = summary.get("relocated_to_dead", 0)
    n_active = summary.get("moved_to_active", 0)

    def _phase_minutes(n: int, per_action_min: float, floor_min: int) -> int:
        return max(floor_min, int(round(n * per_action_min)))

    # Rough operator-workload estimates: dead moves ~30s each (single-list cleanup),
    # active HIGH/MID moves ~60s each (cross-list duplication), INACTIVE ~30s.
    phase_a_min = _phase_minutes(n_dead, 0.5, 30) if n_dead else 0
    phase_b_min = _phase_minutes(int(round(n_active * 0.7)), 1.0, 90) if n_active else 0
    phase_c_min = _phase_minutes(int(round(n_active * 0.3)), 0.5, 45) if n_active else 0

    print("=== REBALANCE PLAN SUMMARY ===", file=file)
    print(f"Targets analyzed:                 {summary.get('targets_analyzed', 0)}", file=file)
    print(f"Relocated to V*-DEAD:             {n_dead}", file=file)
    print(f"Moved to optimal active list:     {n_active}", file=file)
    print(f"New lists to create:              {len(new_lists)}", file=file)
    if dead_lists:
        print(f"  - {', '.join(dead_lists)} (DEAD pool)", file=file)
    if other_lists:
        for nm in other_lists:
            print(f"  - {nm}", file=file)
    print("", file=file)
    print(
        f"Current estimated daily booty:    {summary.get('current_estimated_daily_booty', 0):.0f}",
        file=file,
    )
    print(
        f"Post-rebalance estimated:         {summary.get('post_rebalance_estimated_daily_booty', 0):.0f}",
        file=file,
    )
    print(f"Expected lift:                    +{delta:.0f} ({lift:+.1f}%)", file=file)
    print("", file=file)
    total_min = phase_a_min + phase_b_min + phase_c_min
    print("Operator workload (manual UI):", file=file)
    print(f"  Phase A (DEAD cleanup):         ~{phase_a_min} min", file=file)
    print(f"  Phase B (HIGH/MID build):       ~{phase_b_min} min", file=file)
    print(f"  Phase C (INACTIVE build):       ~{phase_c_min} min", file=file)
    print(f"  Total:                          ~{total_min/60.0:.2f} hours across multiple sessions", file=file)
    print("=== END REBALANCE SUMMARY ===", file=file)


def _suppress_actions_for_moved_coords(
    all_actions: list[dict[str, Any]],
    rebalance_actions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop conflicting per-slot actions for coords being moved/relocated.

    Keeps the rebalance actions themselves, DELETE def>500 (game-state safety),
    FLAG_CT2_CT3 (legal-risk surface), and all SPLIT_LIST/REORDER (per-list
    structural actions independent of the coord-level move).
    """
    moved_coords: set[tuple[int, int]] = set()
    for a in rebalance_actions:
        slot = a.get("slot") or {}
        coord = slot.get("coords")
        if coord:
            moved_coords.add(tuple(coord))
    if not moved_coords:
        return all_actions

    keep_action_types = {
        "DELETE",
        "FLAG_CT2_CT3",
        "MOVE_SLOT",
        "RELOCATE_TO_DEAD",
        "SPLIT_LIST",
        "REORDER",
    }
    pruned: list[dict[str, Any]] = []
    for a in all_actions:
        slot = a.get("slot") or {}
        coord = slot.get("coords")
        if coord and tuple(coord) in moved_coords and a["action"] not in keep_action_types:
            continue
        pruned.append(a)
    return pruned


# ─── Main ────────────────────────────────────────────────────────────────


async def main_async(api_base: str, *, rebalance: bool = False) -> int:
    api = ApiClient(api_base)
    try:
        await ensure_jwt(api)
        status = await ensure_travian_session(api)
        if not status.get("connected"):
            print(f"ERROR: Travian session not connected: {status}", file=sys.stderr)
            return 2

        print(f"Connected as {status.get('player_name')} (tribe={status.get('tribe_id')}, "
              f"villages={len(status.get('villages', []))})", file=sys.stderr)

        # Step A: villages
        villages = await fetch_villages(api)
        village_label_by_vid: dict[int, str] = {}
        for label, vals in BUDGET.items():
            v = villages["by_coords"].get(vals["coords"])
            if v:
                village_label_by_vid[v["id"]] = label

        # Live troops
        print("Fetching live troops per village...", file=sys.stderr)
        live_troops = await fetch_live_troops(api, villages)
        log_live_vs_budget(live_troops)

        # Existing lists
        print("Fetching existing farm lists...", file=sys.stderr)
        all_lists = await fetch_all_lists(api)
        print(f"  {len(all_lists)} lists", file=sys.stderr)
        details = await fetch_list_details(api, [fl["id"] for fl in all_lists])

        # Snapshot current lists
        snapshot_path = OUT_DIR / f"current-lists-{ISO_DATE}.json"
        snapshot_path.write_text(json.dumps({
            "summaries": all_lists,
            "details": details,
        }, indent=2), encoding="utf-8")

        # Per-slot metadata
        slot_ms: list[dict[str, Any]] = []
        for lid, det in details.items():
            list_name = det["name"]
            owner_vid = det.get("owner_village_id")
            for s in det.get("slots", []):
                slot_ms.append(slot_metadata(s, lid, list_name, owner_vid))

        # Step C: score & bucket
        print(f"Scoring {len(slot_ms)} slots...", file=sys.stderr)
        bucket_slots(slot_ms)

        # Step D: strike-wave planning (v3.1 — cranny-refill model, no trim)
        print("Planning strike waves (v3.1: refill model, no soft cap)...", file=sys.stderr)
        waves_by_v, cross_village_recommendations = plan_strike_waves(slot_ms, village_label_by_vid)
        strike_total = total_strike_troops(waves_by_v)
        trim_actions: list[dict[str, Any]] = []  # kept for JSON schema compat; always empty in v3.1
        print(f"  strike troops committed: {strike_total} ({HARD_TROOP_CAP} hard cap, info-only)", file=sys.stderr)

        # Step E: gap targets + auto-scout
        print("Fetching gap targets via /api/reports/analyze (per source village)...", file=sys.stderr)
        existing_coords = {sm["coords"] for sm in slot_ms}
        gap_targets = await fetch_gap_targets(api, villages, existing_coords)
        print(f"  {len(gap_targets)} gap targets", file=sys.stderr)
        auto_scout = build_auto_scout(slot_ms, gap_targets, villages)

        # Step F: slow-wave proposals
        slow_waves = build_slow_waves(slot_ms, villages)
        slow_total_if_active = sum(
            (prop["axes_committed_if_all_active"]
             + prop.get("rams_committed_if_all_active", 0)
             + prop.get("catas_committed_if_all_active", 0))
            for prop in slow_waves.values()
        )

        # Step G + H: diff actions and tiering
        actions: list[dict[str, Any]] = []
        for sm in slot_ms:
            da = diff_action(sm, waves_by_v)
            delta = expected_daily_delta(sm, waves_by_v)
            tier = tier_for(da["action"], sm, delta)
            cur_str: str = "—"
            rec_str: str = "—"
            if "current_count" in da and "recommended_unit" in da:
                cur_str = f"{da['current_count']}× {UNIT_PROFILES[da['recommended_unit']]['name']}"
                rec_str = f"{da['recommended_count']}× {UNIT_PROFILES[da['recommended_unit']]['name']}"
            actions.append({
                "slot": sm,
                "action": da["action"],
                "reason": da["reason"],
                "tier": tier,
                "expected_daily_delta_booty": delta,
                "current_str": cur_str,
                "recommended_str": rec_str,
                "extra": {k: v for k, v in da.items() if k not in ("action", "reason")},
            })
        # ADD_NEW gap targets — synthetic slots
        for gt in gap_targets:
            synth_slot = {
                "list_id": None,
                "list_name": "(gap — not in any list)",
                "slot_id": None,
                "coords": gt["coords"],
                "bucket": "HIGH" if gt["score"] >= 50 else "MID" if gt["score"] >= 10 else "INACTIVE-low",
                "best_source": gt["discovered_from_village"],
                "best_unit": "t1",  # default bulk
                "canonical_score": gt["score"],
                "avg_loot": gt.get("est_loot") or 0,
                "sample_count": 0,
                "defense_proxy": 0,
                "last_raid_icon": "unknown",
                "last_raid_age_h": None,
                "total_raids": 0,
                "total_booty": 0,
                "troops": {},
                "pushing_protection_suspect": False,
                "is_active": False,
                "is_running": False,
                "name": gt.get("village_name", ""),
                "population": 0,
                "source_bucket": "HIGH" if gt["score"] >= 50 else "MID",
                "planned_waves": 0,
            }
            tier = tier_for("ADD_NEW", synth_slot, gt["score"])
            actions.append({
                "slot": synth_slot,
                "action": "ADD_NEW",
                "reason": f"gap target discovered from {gt['discovered_from_village']} analyze",
                "tier": tier,
                "expected_daily_delta_booty": gt.get("est_loot") or 0,
                "current_str": "(not in any list)",
                "recommended_str": f"add to a {gt['discovered_from_village']}-owned list",
                "extra": {"score": gt["score"], "est_loot": gt.get("est_loot")},
            })

        # v3.3 Bug A — cross-village stacking recommendations as ADD_NEW_SLOT actions
        for rec in cross_village_recommendations:
            other_v = rec["stacking_with_owner_village"]
            primary_list = rec["primary_list_name"]
            unit_name = UNIT_PROFILES[rec["unit"]]["name"]
            tier_cv = 1 if rec["expected_loot"] >= 100 else 2
            actions.append({
                "slot": rec["slot"],
                "action": "ADD_NEW_SLOT",
                "reason": (
                    f"cross-village stacking: target is already in `{primary_list}` (owner {rec['primary_owner_village']}); "
                    f"adding it to any {other_v}-owned list enables a wave from {other_v} arriving ~{rec['arrival_min']}m "
                    f"after Send All with expected loot {rec['expected_loot']} res"
                ),
                "tier": tier_cv,
                "expected_daily_delta_booty": float(rec["expected_loot"] * 4),  # 4 cycles/day proxy
                "current_str": f"not in any {other_v}-owned list",
                "recommended_str": f"{rec['count']}× {unit_name} from {other_v} (arr ~{rec['arrival_min']}m)",
                "extra": {
                    "primary_list_name": primary_list,
                    "primary_owner_village": rec["primary_owner_village"],
                    "stacking_with_owner_village": other_v,
                    "unit": rec["unit"],
                    "count": rec["count"],
                    "arrival_min": rec["arrival_min"],
                    "expected_loot": rec["expected_loot"],
                },
            })

        # v3.4 — Load historical snapshots and compute per-slot throughput
        #         BEFORE consolidation so existing-action filtering can reference
        #         the un-mutated per-slot diff_action list.
        now_utc = datetime.now(timezone.utc)
        historical = load_recent_snapshots(
            OUT_ROOT, max_age_days=THROUGHPUT_SNAPSHOT_MAX_AGE_DAYS, now=now_utc,
        )
        print(f"  history: {len(historical)} historical snapshots loaded", file=sys.stderr)
        throughput_by_slot_id = compute_throughput(
            historical, now=now_utc,
            recent_window_days=THROUGHPUT_RECENT_DAYS,
            baseline_window_days=THROUGHPUT_BASELINE_DAYS,
        )
        print(f"  throughput: {len(throughput_by_slot_id)} slots have history", file=sys.stderr)

        existing_by_slot_id: dict[int | None, dict[str, Any]] = {
            a["slot"].get("slot_id"): a for a in actions
        }
        throughput_drop_actions_only = emit_throughput_drop_actions(
            slot_ms=slot_ms,
            existing_actions_by_slot_id=existing_by_slot_id,
            throughput_by_slot_id=throughput_by_slot_id,
        )
        actions.extend(throughput_drop_actions_only)
        print(
            f"  throughput_drop: {len(throughput_drop_actions_only)} THROUGHPUT_DROP actions emitted",
            file=sys.stderr,
        )

        # v3.3 — pipeline order (Bug 3 → Bug 5A → Bug 5B → Bug 6 → tier → Bug 4)

        # Bug 3: consolidate same-target duplicates across lists
        actions, duplicate_deactivates = consolidate_per_target(actions, slot_ms)
        print(f"  consolidation: {duplicate_deactivates} duplicate-target DEACTIVATEs created", file=sys.stderr)

        # Bug 5 Part A: per-list recommended sort mode
        slots_by_list: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for sm in slot_ms:
            if sm.get("list_id") is not None:
                slots_by_list[sm["list_id"]].append(sm)
        list_sort_recommendations: dict[int, dict[str, Any]] = {}
        for fl in all_lists:
            lid = fl["id"]
            if _is_slow_wave_list_name(fl.get("name", "")):
                continue  # slow-wave lists don't get sort recommendations
            slots_in_list = slots_by_list.get(lid, [])
            if not slots_in_list:
                continue
            sort_mode, rationale = recommend_sort_mode_for_list(slots_in_list)
            list_sort_recommendations[lid] = {
                "list_id": lid,
                "list_name": fl.get("name", ""),
                "owner_village_id": fl.get("owner_village_id"),
                "recommended_sort": sort_mode,
                "rationale": rationale,
                "slot_count": len(slots_in_list),
            }

        # Bug 5 Part B: REORDER actions per list (uses Part A's sort_mode)
        reorder_actions: list[dict[str, Any]] = []
        consolidated_dup_slot_ids: set[int] = {
            a["slot"]["slot_id"]
            for a in actions
            if a.get("action") == "DEACTIVATE"
            and "duplicate_target" in (a.get("reason") or "")
            and a["slot"].get("slot_id") is not None
        }
        for lid, rec in list_sort_recommendations.items():
            slots_in_list = [
                s for s in slots_by_list.get(lid, [])
                if s.get("slot_id") not in consolidated_dup_slot_ids
            ]
            reorder_actions.extend(
                emit_reorder_actions(
                    list_name=rec["list_name"],
                    list_id=lid,
                    slots_in_list=slots_in_list,
                    sort_mode=rec["recommended_sort"],
                )
            )
        actions.extend(reorder_actions)
        print(f"  reorder: {len(reorder_actions)} REORDER actions emitted", file=sys.stderr)

        # Bug 6: SPLIT_LIST actions + REORDER pruning
        split_actions: list[dict[str, Any]] = []
        split_list_names: set[str] = set()
        server_age = day_of_server()
        for fl in all_lists:
            if _is_slow_wave_list_name(fl.get("name", "")):
                continue
            split = emit_split_list_action(
                list_summary=fl,
                slots_in_list=slots_by_list.get(fl["id"], []),
                consolidated_deactivate_slot_ids=consolidated_dup_slot_ids,
                server_age_days=server_age,
            )
            if split is not None:
                split_actions.append(split)
                split_list_names.add(fl.get("name", ""))
        actions.extend(split_actions)
        actions = prune_reorder_actions_for_split_lists(actions, split_list_names)
        print(f"  split: {len(split_actions)} SPLIT_LIST actions emitted "
              f"(REORDER actions pruned for: {sorted(split_list_names) or 'none'})", file=sys.stderr)

        # v4.0 — Path 3 full rebalance pass (opt-in via --rebalance)
        rebalance_plan: RebalancePlan | None = None
        rebalance_actions: list[dict[str, Any]] = []
        rebalance_summary: dict[str, Any] | None = None
        rebalance_post_structure: dict[str, dict[str, Any]] | None = None
        if rebalance:
            print("Running Path 3 rebalance pass...", file=sys.stderr)
            rebalance_plan, rebalance_actions, rebalance_summary, rebalance_post_structure = (
                _run_rebalance_pass(
                    slot_ms=slot_ms,
                    village_label_by_vid=village_label_by_vid,
                    now_unix=datetime.now(timezone.utc).timestamp(),
                )
            )
            actions.extend(rebalance_actions)
            # Suppress upstream per-slot actions for coords being moved/relocated.
            actions = _suppress_actions_for_moved_coords(actions, rebalance_actions)
            print(
                f"  rebalance: {rebalance_summary['moved_to_active']} MOVE_SLOT, "
                f"{rebalance_summary['relocated_to_dead']} RELOCATE_TO_DEAD",
                file=sys.stderr,
            )

        # Tier assignment + ordering
        ACTION_PRIORITY = {
            "SPLIT_LIST": 0,    # surface first — most impactful structural change
            "DELETE": 1,
            "FLAG_CT2_CT3": 2,
            "THROUGHPUT_DROP": 2,  # v3.4: dying slot → high operator visibility
            "MOVE_SLOT": 3,     # v4.0: Path 3 rebalance — relocate to optimal home
            "CHANGE_UNIT": 3,
            "ADD_NEW_SLOT": 3,  # v3.3: cross-village stacking (creates a new slot)
            "BOOST": 4,
            "ADD_NEW": 4,       # gap target (creates a new slot, low confidence)
            "ADD_WAVE": 5,
            "MOVE": 6,
            "SHRINK": 7,
            "REORDER": 8,
            "RELOCATE_TO_DEAD": 8,  # v4.0: Path 3 rebalance — bulk DEAD migration
            "DEACTIVATE": 9,
            "KEEP": 10,
        }
        actions.sort(key=lambda a: (
            a["tier"],
            ACTION_PRIORITY.get(a["action"], 11),
            -a["expected_daily_delta_booty"],
        ))

        # Bug 4: collapse first (≥10 same-reason groups), then cap Tier 2 visible at 50
        actions, collapsed_summaries = cap_and_collapse_tier_2(actions)
        # Re-sort after potential tier reassignments
        actions.sort(key=lambda a: (
            a["tier"],
            ACTION_PRIORITY.get(a["action"], 11),
            -a["expected_daily_delta_booty"],
        ))

        # v3.3 — tier counts reflect *distinct* visible rows, not collapsed-into-summary
        # rows. Total counts including collapsed are in the JSON appendix.
        tier_counts = {1: 0, 2: 0, 3: 0}
        tier_counts_total = {1: 0, 2: 0, 3: 0}
        for a in actions:
            t = a["tier"]
            tier_counts_total[t] = tier_counts_total.get(t, 0) + 1
            if not a.get("collapsed_into_summary"):
                tier_counts[t] = tier_counts.get(t, 0) + 1

        est_t1_t2_delta = sum(a["expected_daily_delta_booty"] for a in actions if a["tier"] in (1, 2))

        # Stacked targets (Section 10)
        stacked: list[dict[str, Any]] = []
        slot_id_to_waves: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for waves in waves_by_v.values():
            for w in waves:
                slot_id_to_waves[w["slot_id"]].append(w)
        for sm in slot_ms:
            ws = slot_id_to_waves.get(sm["slot_id"], [])
            total = sum(w["expected_loot"] for w in ws)
            if len(ws) < 2 or total < 50:
                continue
            has_ram = any(
                slot["coords"] == sm["coords"] for slot in slow_waves["V3-SLOW-RAM-CARRY"]["slots"]
            )
            has_cata = any(
                slot["coords"] == sm["coords"] for slot in slow_waves["V3-SLOW-CATA-CARRY"]["slots"]
            )
            stacked.append({
                "coords": sm["coords"],
                "waves": ws,
                "total_loot": total,
                "has_ram": has_ram,
                "has_cata": has_cata,
            })
        stacked.sort(key=lambda x: x["total_loot"], reverse=True)

        # Step I: verify
        verify_ok, verify_errors = verify(
            strike_total=strike_total,
            slow_total_if_active=slow_total_if_active,
            auto_scout_total=auto_scout["slot_count"],
            auto_scout=auto_scout,
            slow_waves=slow_waves,
            slot_ms=slot_ms,
        )

        # Step J: assemble context, render, write
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        json_path = OUT_DIR / f"diff-{ISO_DATE}.json"
        md_path = OUT_DIR / f"diff-{ISO_DATE}.md"

        # Identify abandoned lists (heuristic: name contains "abandoned" or owner_vid unknown to account)
        own_vids = {v["id"] for v in villages["all"]}
        abandoned_lists = [fl for fl in all_lists if fl.get("owner_village_id") not in own_vids]

        ctx: dict[str, Any] = {
            "timestamp": timestamp,
            "day_of_server": day_of_server(),
            "player_name": status.get("player_name"),
            "api_base": api_base,
            "existing_lists": all_lists,
            "existing_lists_count": len(all_lists),
            "abandoned_lists": abandoned_lists,
            "village_label_by_vid": village_label_by_vid,
            "live_troops": live_troops,
            "slot_count_total": len(slot_ms),
            "slot_ms": slot_ms,
            "waves_by_v": waves_by_v,
            "strike_total": strike_total,
            "trim_actions": trim_actions,
            "auto_scout": auto_scout,
            "slow_waves": slow_waves,
            "slow_total_if_active": slow_total_if_active,
            "stacked_targets": stacked,
            "gap_targets": gap_targets,
            "actions": actions,
            "tier_counts": tier_counts,
            "est_daily_delta_t1_t2": est_t1_t2_delta,
            "verify_ok": verify_ok,
            "verify_errors": verify_errors,
            "json_path": str(json_path),
            "fresh_lists": [],
            "collapsed_summaries": collapsed_summaries,
            "duplicate_deactivates": duplicate_deactivates,
            "list_sort_recommendations": list_sort_recommendations,
            "split_actions": split_actions,
            "reorder_actions": [a for a in actions if a["action"] == "REORDER"],
            "throughput_by_slot_id": throughput_by_slot_id,  # v3.4
            "throughput_drop_actions": throughput_drop_actions_only,
            "historical_snapshot_count": len(historical),
            "rebalance_enabled": rebalance,
            "rebalance_summary": rebalance_summary,
            "rebalance_actions": rebalance_actions,
            "rebalance_post_structure": rebalance_post_structure,
        }

        md = render_markdown(ctx)

        # JSON appendix
        def _json_safe(o: Any) -> Any:
            if isinstance(o, tuple):
                return list(o)
            if isinstance(o, set):
                return list(o)
            return str(o)

        json_payload = {
            "run": {
                "timestamp": timestamp,
                "server": SERVER_URL,
                "day_of_server": day_of_server(),
                "api_base": api_base,
                "version": VERSION,
            },
            "owner_vid_map": {label: vid_for(label, villages) for label in BUDGET},
            "budget": BUDGET,
            "live_state": live_troops,
            "abandoned_lists": abandoned_lists,
            "cap_check": {
                "strike_troops_out": strike_total,
                "slow_troops_if_all_active": slow_total_if_active,
                "auto_scout_troops": auto_scout["slot_count"],
                "grand_total_if_all_active": strike_total + slow_total_if_active + auto_scout["slot_count"],
                "limit_proxy": None,  # v3.1 removed the soft 14k proxy
                "hard_cap": HARD_TROOP_CAP,
                "status": "OK" if verify_ok else "TRIMMED" if trim_actions else "WARNING",
            },
            "trim_actions": trim_actions,
            "diff_actions": [
                {
                    "tier": a["tier"],
                    "action": a["action"],
                    "list_id": a["slot"].get("list_id"),
                    "list_name": a["slot"].get("list_name"),
                    "coords": (
                        None if a["action"] == "SPLIT_LIST"
                        else list(a["slot"]["coords"])
                    ),
                    "current": a.get("current_str"),
                    "recommended": a.get("recommended_str"),
                    "reason": a["reason"],
                    "expected_daily_delta_booty": a["expected_daily_delta_booty"],
                    "collapsed_into_summary": a.get("collapsed_into_summary", False),
                    "extra": a.get("extra", {}),
                }
                for a in actions
            ],
            "tier_counts_distinct": tier_counts,
            "tier_counts_total": tier_counts_total,
            "gaps": gap_targets,
            "stacked_targets": [
                {
                    "coords": list(st["coords"]),
                    "waves": st["waves"],
                    "total_loot": st["total_loot"],
                    "has_ram": st["has_ram"],
                    "has_cata": st["has_cata"],
                }
                for st in stacked
            ],
            "auto_scout_proposal": auto_scout,
            "slow_wave_proposals": slow_waves,
            "ct2_ct3_suspects": [
                {"coords": list(sm["coords"]), "list_name": sm["list_name"], "raids": sm["sample_count"]}
                for sm in slot_ms if sm["pushing_protection_suspect"]
            ],
            "excluded_high_defense": [
                {"coords": list(sm["coords"]), "list_name": sm["list_name"], "def_proxy": sm["defense_proxy"]}
                for sm in slot_ms if sm["defense_proxy"] > DEFENSE_HARD_LIMIT
            ],
            "trigger_order_recommendation": [
                "V*-HIGH-* lists (wave spacing is internal to each plan)",
                "V*-MID-* lists",
                "V*-AUTO-SCOUT",
                "V*-INACTIVE-* lists (incl. *-Top from any SPLIT_LIST)",
                "SKIP: V*-DEAD, V*-SLOW-*",
            ],
            "list_sort_recommendations": list(list_sort_recommendations.values()),
            "split_list_proposals": [
                {
                    "list_id": s["extra"]["list_id"],
                    "list_name": s["extra"]["list_name"],
                    "active_slot_count": s["extra"]["active_slot_count"],
                    "raids_per_slot": s["extra"]["raids_per_slot"],
                    "top_share_pct": s["extra"]["top_share_pct"],
                    "new_top_name": s["extra"]["new_top_name"],
                    "new_tail_name": s["extra"]["new_tail_name"],
                    "top_20_coords": s["extra"]["top_20_coords"],
                    "tail_coords": s["extra"]["tail_coords"],
                    "expected_daily_delta_booty": s["expected_daily_delta_booty"],
                }
                for s in split_actions
            ],
            "reorder_proposals": [
                {
                    "list_name": (a.get("extra") or {}).get("list_name"),
                    "list_id": (a.get("extra") or {}).get("list_id"),
                    "coords": list(a["slot"]["coords"]),
                    "post_sort_position": (a.get("extra") or {}).get("post_sort_position"),
                    "target_position": (a.get("extra") or {}).get("target_position"),
                    "expected_daily_delta_booty": a["expected_daily_delta_booty"],
                }
                for a in actions if a["action"] == "REORDER"
            ],
            "diagnostic_failures": dict(api.failures),
            # v3.4 — always-present throughput keys (may be empty arrays)
            "throughput_metrics": [
                throughput_dict(t) for t in throughput_by_slot_id.values()
            ],
            "throughput_drop_actions": [
                {
                    "tier": a["tier"],
                    "action": a["action"],
                    "list_id": a["slot"].get("list_id"),
                    "list_name": a["slot"].get("list_name"),
                    "coords": list(a["slot"]["coords"]) if a["slot"].get("coords") else None,
                    "current": a.get("current_str"),
                    "recommended": a.get("recommended_str"),
                    "reason": a["reason"],
                    "expected_daily_delta_booty": a["expected_daily_delta_booty"],
                    "extra": a.get("extra", {}),
                }
                for a in throughput_drop_actions_only
            ],
            "historical_snapshot_count": len(historical),
            "rebalance_plan": _build_rebalance_json(
                enabled=rebalance,
                summary=rebalance_summary,
                actions=rebalance_actions,
                post_structure=rebalance_post_structure,
            ),
        }

        md_path.write_text(md, encoding="utf-8")
        json_path.write_text(json.dumps(json_payload, indent=2, default=_json_safe), encoding="utf-8")

        # Print to stdout
        print(md)
        print(f"\n📝 Markdown written to: {md_path}", file=sys.stderr)
        print(f"📦 JSON appendix written to: {json_path}", file=sys.stderr)
        print(f"📸 Snapshot written to: {snapshot_path}", file=sys.stderr)

        # v3.1 — delta summary vs prior diff
        prior = _load_prior_diff(json_path)
        if prior is not None:
            _print_delta_summary(prior, json_payload, waves_by_v, BUDGET, file=sys.stderr)
        else:
            print("(No prior diff found — skipping delta summary.)", file=sys.stderr)

        # v3.4 — throughput summary delta line
        _print_throughput_summary(
            throughput_by_slot_id=throughput_by_slot_id,
            throughput_drop_actions=throughput_drop_actions_only,
            file=sys.stderr,
        )

        # v4.0 — Path 3 rebalance summary (operator-facing)
        if rebalance and rebalance_summary is not None:
            _print_rebalance_summary(rebalance_summary, file=sys.stderr)

        return 0 if verify_ok else 1
    finally:
        await api.aclose()


def _load_prior_diff(current_json_path: Path) -> dict[str, Any] | None:
    """Find the newest diff-*.json older than ``current_json_path``.

    Walks every version subdirectory under OUT_ROOT so the delta summary can
    compare across versions (e.g. v3.1 vs v3 from a prior session).
    """
    candidates: list[Path] = []
    for version_dir in OUT_ROOT.iterdir():
        if not version_dir.is_dir():
            continue
        candidates.extend(
            p for p in version_dir.glob("diff-*.json") if p != current_json_path
        )
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None
    try:
        return json.loads(candidates[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _print_throughput_summary(
    throughput_by_slot_id: dict[int, SlotThroughput],
    throughput_drop_actions: list[dict[str, Any]],
    file: Any = sys.stderr,
) -> None:
    """v3.4 — print the required summary delta line for throughput tracking."""
    import statistics as _stats

    slots_with_pairs = [
        t for t in throughput_by_slot_id.values()
        if t.raids_per_24h_recent is not None and t.samples_used >= 2
    ]
    disappeared = [
        t for t in throughput_by_slot_id.values()
        if t.pct_change == -1.0
    ]
    tier1 = sum(1 for a in throughput_drop_actions if a["tier"] == 1)
    tier2 = sum(1 for a in throughput_drop_actions if a["tier"] == 2)

    recent_rates = [t.raids_per_24h_recent for t in slots_with_pairs]
    baseline_rates = [
        t.raids_per_24h_baseline for t in throughput_by_slot_id.values()
        if t.raids_per_24h_baseline is not None
    ]
    median_recent = _stats.median(recent_rates) if recent_rates else 0.0
    median_baseline = _stats.median(baseline_rates) if baseline_rates else 0.0

    print("", file=file)
    print(
        f"Throughput tracking: {len(slots_with_pairs)} slots with ≥2 paired snapshots in last 7d.",
        file=file,
    )
    print(
        f"  {len(throughput_drop_actions)} emitted THROUGHPUT_DROP "
        f"(Tier 1: {tier1}, Tier 2: {tier2}).",
        file=file,
    )
    print(
        f"  {len(disappeared)} slots disappeared from active in last 7d (pct_change = -1.0).",
        file=file,
    )
    print(
        f"  Median recent rate: {median_recent:.2f} raids/24h. "
        f"Median baseline: {median_baseline:.2f}/24h.",
        file=file,
    )


def _print_delta_summary(
    prior: dict[str, Any],
    current: dict[str, Any],
    waves_by_v: dict[str, list[dict[str, Any]]],
    budget: dict[str, dict[str, Any]],
    file: Any = sys.stderr,
) -> None:
    """Print a side-by-side comparison of yesterday's diff vs today's."""
    print("", file=file)
    prior_version = prior.get("run", {}).get("version") or "v?"
    current_version = current.get("run", {}).get("version") or VERSION
    print(f"═══ DELTA SUMMARY — prior ({prior_version}) vs current ({current_version}) ═══", file=file)
    print(f"  prior run: {prior.get('run', {}).get('timestamp', '?')}", file=file)
    print(f"  this run:  {current.get('run', {}).get('timestamp', '?')}", file=file)
    print("", file=file)

    # Tier counts — exclude collapsed_into_summary rows (v3.3 fix)
    def _tier_counts(actions: list[dict[str, Any]]) -> dict[int, int]:
        c = {1: 0, 2: 0, 3: 0}
        for a in actions:
            if a.get("collapsed_into_summary"):
                continue
            t = a.get("tier", 3)
            c[t] = c.get(t, 0) + 1
        return c

    prior_counts = _tier_counts(prior.get("diff_actions", []))
    cur_counts = _tier_counts(current.get("diff_actions", []))
    print("  Tier counts (prior → today):", file=file)
    for tier in (1, 2, 3):
        d = cur_counts[tier] - prior_counts[tier]
        sign = "+" if d >= 0 else ""
        print(f"    Tier {tier}: {prior_counts[tier]} → {cur_counts[tier]}  ({sign}{d})", file=file)

    # Committed troops per (village, unit)
    prior_strike = prior.get("cap_check", {}).get("strike_troops_out", 0)
    cur_strike = current.get("cap_check", {}).get("strike_troops_out", 0)
    print("", file=file)
    print(f"  Strike troops committed: {prior_strike} → {cur_strike}  "
          f"({'+' if cur_strike >= prior_strike else ''}{cur_strike - prior_strike})", file=file)

    # Per (village, unit) committed today
    committed_now: dict[tuple[str, str], int] = defaultdict(int)
    for src, waves in waves_by_v.items():
        for w in waves:
            committed_now[(src, w["unit"])] += w["count"]
    print("", file=file)
    print("  Troop commitments by (village, unit) — today:", file=file)
    for v_label in SOURCE_VILLAGES:
        b = budget[v_label]
        row_bits = []
        for u in ("t1", "t3", "t5", "t6"):
            bv = b.get(u, 0)
            if bv <= 0:
                continue
            cv = committed_now.get((v_label, u), 0)
            idle_pct = ((bv - cv) / bv * 100) if bv else 0
            row_bits.append(f"{UNIT_PROFILES[u]['name']} {cv}/{bv} ({idle_pct:.0f}% idle)")
        print(f"    {v_label}: {' · '.join(row_bits)}", file=file)

    # Stacked targets count
    cur_stacked = len(current.get("stacked_targets", []))
    prior_stacked = len(prior.get("stacked_targets", []))
    print("", file=file)
    print(f"  Multi-wave stacked targets: {prior_stacked} → {cur_stacked}  "
          f"({'+' if cur_stacked >= prior_stacked else ''}{cur_stacked - prior_stacked})", file=file)

    # Consolidation DEACTIVATEs
    cur_consolidations = sum(
        1 for a in current.get("diff_actions", [])
        if a.get("action") == "DEACTIVATE" and "duplicate_target" in (a.get("reason") or "")
    )
    print(f"  Duplicate-target DEACTIVATEs (Bug 3 consolidation): {cur_consolidations}", file=file)
    print("", file=file)
    print("═════════════════════════════════════════════", file=file)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base (default: {DEFAULT_API})")
    parser.add_argument(
        "--rebalance",
        action="store_true",
        default=False,
        help=(
            "Enable Path 3 full rebalance pass. WARNING: this produces a "
            "comprehensive rewrite plan affecting hundreds of slots. Review "
            "before executing."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.api, rebalance=args.rebalance))


if __name__ == "__main__":
    sys.exit(main())
