"""Snapshot-history service (v3.4).

Loads historical ``current-lists-*.json`` snapshots written by
``scripts/raid_optimizer_diff_v3.py`` and computes per-slot raid throughput
(raids per 24h) over recent vs baseline windows. The optimizer uses the
output to emit THROUGHPUT_DROP actions when a slot's fire rate has dropped
≥50% relative to its prior baseline.

Read-only; no HTTP, no game-state writes. Walks the on-disk snapshot
directory (including per-version subdirectories) and parses JSON. Resilient
to schema variations and corrupt files — corrupt snapshots are skipped with
a logged warning, never crashing the run.

The join key across snapshots is ``slot.id`` (stable per-slot integer
assigned by Travian and preserved as long as the slot exists in the same
farm list). When a slot is deleted and recreated, ``total_raids`` may
appear to decrease — those pairs are skipped (treated as no signal).
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Filename pattern: ``current-lists-2026-05-15T19-09-53Z.json``
_FILENAME_RE = re.compile(
    r"current-lists-(\d{4}-\d{2}-\d{2})T(\d{2})-(\d{2})-(\d{2})Z\.json$"
)


# ─── Dataclasses ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SlotSnapshot:
    """A single slot's state at one snapshot point."""

    slot_id: int
    list_id: int
    list_name: str
    x: int
    y: int
    total_raids: int
    total_booty: int
    last_raid_time: int | None
    is_active: bool


@dataclass(frozen=True)
class HistoricalSnapshot:
    """A whole on-disk snapshot file parsed into per-slot records."""

    run_timestamp: datetime  # UTC
    file_path: Path
    slots: dict[int, SlotSnapshot]  # keyed by slot_id


@dataclass(frozen=True)
class SlotThroughput:
    """Computed throughput metrics for a single slot.

    ``raids_per_24h_recent`` / ``raids_per_24h_baseline`` are MEDIAN of
    per-pair rates over their windows (resilient to outliers from server
    lag or burst-y operator behavior). ``pct_change`` is a fractional
    delta — ``-1.0`` means rate dropped to zero; ``+0.5`` means 50%
    increase. ``samples_used`` counts the recent-window snapshot pairs.
    """

    slot_id: int
    list_name: str
    coords: tuple[int, int]
    raids_per_24h_recent: float | None
    raids_per_24h_baseline: float | None
    pct_change: float | None
    last_seen_active_at: datetime | None
    samples_used: int
    avg_loot_per_raid: float = 0.0  # convenience for downstream booty-loss math


# ─── Parsing ──────────────────────────────────────────────────────────────


def parse_snapshot_filename(path: Path) -> datetime | None:
    """Extract the run timestamp from a snapshot filename.

    Returns ``None`` if the filename doesn't match the expected pattern.
    """
    m = _FILENAME_RE.search(path.name)
    if not m:
        return None
    date_part, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4)
    iso = f"{date_part}T{hh}:{mm}:{ss}+00:00"
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _slot_from_raw(raw_slot: dict, list_id: int, list_name: str) -> SlotSnapshot | None:
    """Convert a raw slot dict (from the JSON) into a SlotSnapshot.

    Returns ``None`` on missing required fields. Skips slots whose
    ``total_raids`` is 0 — there's no history to track.
    """
    try:
        slot_id = int(raw_slot["id"])
        x = int(raw_slot["x"])
        y = int(raw_slot["y"])
        total_raids = int(raw_slot.get("total_raids") or 0)
    except (KeyError, TypeError, ValueError):
        return None

    if total_raids == 0:
        return None

    total_booty = int(raw_slot.get("total_booty") or 0)
    last_raid = raw_slot.get("last_raid") or {}
    last_raid_time = last_raid.get("time") if isinstance(last_raid, dict) else None
    if last_raid_time is not None:
        try:
            last_raid_time = int(last_raid_time)
        except (TypeError, ValueError):
            last_raid_time = None

    return SlotSnapshot(
        slot_id=slot_id,
        list_id=list_id,
        list_name=list_name,
        x=x,
        y=y,
        total_raids=total_raids,
        total_booty=total_booty,
        last_raid_time=last_raid_time,
        is_active=bool(raw_slot.get("is_active")),
    )


def load_snapshot(path: Path) -> HistoricalSnapshot | None:
    """Parse a single ``current-lists-*.json`` snapshot.

    Returns ``None`` if the file can't be read, parsed, or is missing
    required structure. Logs the failure reason — does not raise.
    """
    ts = parse_snapshot_filename(path)
    if ts is None:
        logger.warning("Skipping snapshot with unparseable filename: %s", path.name)
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Skipping unreadable snapshot %s: %s", path.name, exc)
        return None

    details = raw.get("details")
    if not isinstance(details, dict):
        logger.warning("Snapshot %s has no 'details' dict — skipping", path.name)
        return None

    slots: dict[int, SlotSnapshot] = {}
    for list_id_str, list_detail in details.items():
        if not isinstance(list_detail, dict):
            continue
        try:
            list_id = int(list_id_str)
        except (TypeError, ValueError):
            continue
        list_name = str(list_detail.get("name") or "")
        for raw_slot in list_detail.get("slots") or []:
            if not isinstance(raw_slot, dict):
                continue
            snap = _slot_from_raw(raw_slot, list_id, list_name)
            if snap is None:
                continue
            # If the same slot_id appears twice (shouldn't happen but defensive),
            # keep the one with higher total_raids.
            existing = slots.get(snap.slot_id)
            if existing is None or snap.total_raids > existing.total_raids:
                slots[snap.slot_id] = snap

    return HistoricalSnapshot(run_timestamp=ts, file_path=path, slots=slots)


def load_recent_snapshots(
    directory: Path,
    max_age_days: int = 14,
    now: datetime | None = None,
) -> list[HistoricalSnapshot]:
    """Find and parse all snapshots within the last ``max_age_days``.

    Walks the supplied directory AND any first-level subdirectory (so
    per-version subdirs like ``v3.3/`` are included automatically).
    Returns a list sorted by ``run_timestamp`` ASCENDING (oldest first).
    Returns ``[]`` if the directory doesn't exist or contains nothing.
    """
    if not directory.exists() or not directory.is_dir():
        return []

    cutoff_dt = (now or datetime.now(UTC))
    # Mirror "max_age_days" as a timedelta cutoff.
    from datetime import timedelta
    cutoff_dt -= timedelta(days=max_age_days)

    candidates: list[Path] = []
    # Top-level files
    candidates.extend(directory.glob("current-lists-*.json"))
    # First-level subdir files (per-version directories)
    for sub in directory.iterdir():
        if sub.is_dir():
            candidates.extend(sub.glob("current-lists-*.json"))

    snapshots: list[HistoricalSnapshot] = []
    for path in candidates:
        ts = parse_snapshot_filename(path)
        if ts is None:
            continue
        if ts < cutoff_dt:
            continue
        snap = load_snapshot(path)
        if snap is not None:
            snapshots.append(snap)

    snapshots.sort(key=lambda s: s.run_timestamp)
    return snapshots


# ─── Throughput calculator ────────────────────────────────────────────────


def _pair_rate_per_24h(
    earlier: SlotSnapshot, later: SlotSnapshot,
    earlier_ts: datetime, later_ts: datetime,
) -> float | None:
    """Rate in raids/24h from a single snapshot pair, or ``None`` if the
    pair carries no usable signal (raids went down, time delta non-positive)."""
    raids_delta = later.total_raids - earlier.total_raids
    if raids_delta < 0:
        return None
    hours_delta = (later_ts - earlier_ts).total_seconds() / 3600.0
    if hours_delta <= 0:
        return None
    return (raids_delta / hours_delta) * 24.0


def compute_throughput(
    snapshots: list[HistoricalSnapshot],
    now: datetime,
    recent_window_days: int = 7,
    baseline_window_days: int = 14,
) -> dict[int, SlotThroughput]:
    """Compute per-slot throughput from a list of historical snapshots.

    For each slot_id that appears in any snapshot:
      - Pair every consecutive snapshot (oldest→newest) for that slot.
      - Recent rate = median of per-pair rates where the later snapshot's
        timestamp is within ``recent_window_days`` of ``now``.
      - Baseline rate = median of per-pair rates where the later snapshot
        is in ``[recent_window_days, baseline_window_days]`` ago.
      - ``pct_change`` = (recent - baseline) / baseline. ``None`` if either
        side is absent. Special case: slot present in older snapshots but
        absent from the newest → ``pct_change = -1.0`` (dropped to zero).

    Edge cases handled:
      * total_raids decreasing between snapshots (slot deleted+recreated):
        the pair is skipped (no signal).
      * Zero / negative time delta (out-of-order snapshots): pair skipped.
      * Slot in old snapshots only: pct_change = -1.0; samples_used = 0.
      * Slot in newest only: recent = None; pct_change = None.
    """
    from datetime import timedelta

    if not snapshots:
        return {}

    snapshots = sorted(snapshots, key=lambda s: s.run_timestamp)

    recent_cutoff = now - timedelta(days=recent_window_days)
    baseline_cutoff = now - timedelta(days=baseline_window_days)

    # Build per-slot ordered history: list of (timestamp, SlotSnapshot)
    slot_history: dict[int, list[tuple[datetime, SlotSnapshot]]] = {}
    for snap in snapshots:
        for slot_id, slot in snap.slots.items():
            slot_history.setdefault(slot_id, []).append((snap.run_timestamp, slot))

    out: dict[int, SlotThroughput] = {}
    for slot_id, history in slot_history.items():
        # history is already in snapshot order (we iterated snapshots in time order)
        recent_rates: list[float] = []
        baseline_rates: list[float] = []
        last_seen_active_at: datetime | None = None

        for earlier_ts, earlier in history:
            if earlier.is_active:
                last_seen_active_at = earlier_ts

        for i in range(1, len(history)):
            earlier_ts, earlier = history[i - 1]
            later_ts, later = history[i]
            # Codex review (v3.4): guard against future-dated snapshots. A
            # clock-skewed snapshot whose timestamp is > now would otherwise
            # land in the "recent" window and feed garbage into the median.
            if later_ts > now:
                continue
            rate = _pair_rate_per_24h(earlier, later, earlier_ts, later_ts)
            if rate is None:
                continue
            if later_ts >= recent_cutoff:
                recent_rates.append(rate)
            elif later_ts >= baseline_cutoff:
                baseline_rates.append(rate)

        recent_rate = statistics.median(recent_rates) if recent_rates else None
        baseline_rate = statistics.median(baseline_rates) if baseline_rates else None

        # Slot in newest snapshot? Compare against the freshest run.
        present_in_newest = slot_id in snapshots[-1].slots

        if not present_in_newest:
            # Slot disappeared. Treat rate as crashed to zero.
            pct_change: float | None = -1.0
        elif recent_rate is not None and baseline_rate is not None and baseline_rate > 0:
            pct_change = (recent_rate - baseline_rate) / baseline_rate
        else:
            pct_change = None

        # avg_loot_per_raid from the freshest available record
        last_slot = history[-1][1]
        avg_loot = (
            last_slot.total_booty / last_slot.total_raids
            if last_slot.total_raids > 0
            else 0.0
        )

        out[slot_id] = SlotThroughput(
            slot_id=slot_id,
            list_name=last_slot.list_name,
            coords=(last_slot.x, last_slot.y),
            raids_per_24h_recent=recent_rate,
            raids_per_24h_baseline=baseline_rate,
            pct_change=pct_change,
            last_seen_active_at=last_seen_active_at,
            samples_used=len(recent_rates),
            avg_loot_per_raid=round(avg_loot, 2),
        )

    return out


# ─── Convenience helpers for downstream consumers ────────────────────────


def throughput_dict(t: SlotThroughput) -> dict:
    """Serialize a SlotThroughput for JSON output."""
    return {
        "slot_id": t.slot_id,
        "list_name": t.list_name,
        "coords": list(t.coords),
        "raids_per_24h_recent": t.raids_per_24h_recent,
        "raids_per_24h_baseline": t.raids_per_24h_baseline,
        "pct_change": t.pct_change,
        "last_seen_active_at": (
            t.last_seen_active_at.isoformat() if t.last_seen_active_at else None
        ),
        "samples_used": t.samples_used,
        "avg_loot_per_raid": t.avg_loot_per_raid,
    }
