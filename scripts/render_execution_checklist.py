"""Render an operator-facing execution-checklist-{ISO}.md from a diff JSON.

Reads ~/.travian/raid-optimizer/v3.5/diff-{ISO}.json (the latest one) and
emits ~/.travian/raid-optimizer/v3.5/execution-checklist-{ISO}.md.

v5.0 wave-stacking aware:
- Phase B groups ADD_TO_LIST + MOVE_SLOT actions by destination list. Each
  row carries a Wave (of total) column.
- Adds a "Phase B summary" table listing the top 30 targets with their
  full wave plans laid out across columns (Wave 1 / 2 / 3 / 4).
- Pre-flight includes a cleanup step for any v4.0-era V*-LOCAL-Clubs
  lists detected in the live snapshot.
- Phase D Send All ordering drops the LOCAL tier.

Splits any phase with >50 rows into Phase X1/X2/X3 sub-batches.
"""

from __future__ import annotations

import json
import os
import sys
import io
from collections import defaultdict
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

OUT_DIR = Path.home() / ".travian" / "raid-optimizer" / "v3.5"
# v4.0 legacy directory — we look here for v4.0 LOCAL list snapshots so the
# pre-flight cleanup step can name the lists the operator should delete.
LEGACY_OUT_DIR = Path.home() / ".travian" / "raid-optimizer" / "v3.4"


def latest_diff_json() -> Path:
    files = sorted(OUT_DIR.glob("diff-*.json"))
    if not files:
        raise SystemExit(f"No diff-*.json snapshots found in {OUT_DIR}")
    return files[-1]


def iso_from_filename(p: Path) -> str:
    name = p.name
    return name[len("diff-"):-len(".json")]


def detect_legacy_local_lists() -> list[str]:
    """Scan the most recent current-lists-*.json snapshot under v3.5 (or v3.4
    fallback) for any V*-LOCAL-Clubs lists the operator may have created in
    Travian during the v4.0 execution mission.
    """
    candidates = sorted(OUT_DIR.glob("current-lists-*.json"))
    if not candidates:
        candidates = sorted(LEGACY_OUT_DIR.glob("current-lists-*.json"))
    if not candidates:
        return []
    snap = json.load(candidates[-1].open(encoding="utf-8"))
    summaries = snap.get("summaries") or []
    return sorted({
        s["name"] for s in summaries
        if isinstance(s.get("name"), str) and "-LOCAL-" in s["name"]
    })


def format_minutes(action_count: int, seconds_per_action: int = 30) -> int:
    return max(1, round(action_count * seconds_per_action / 60))


def chunk(items, n):
    for i in range(0, len(items), n):
        yield i // n, items[i:i + n]


def dest_of(a: dict) -> str:
    extra = a.get("extra") or {}
    return (a.get("recommended_list_name") or extra.get("recommended_list_name")
            or a.get("target_list_name") or "?")


def group_by_dest(actions):
    groups: dict[str, list[dict]] = defaultdict(list)
    for a in actions:
        groups[dest_of(a)].append(a)
    for rows in groups.values():
        # Sort by wave_index ASC (waves of the same target stay in order)
        # then expected_daily_delta_booty DESC (high-value targets first).
        rows.sort(key=lambda r: (
            (r.get("extra") or {}).get("wave_index", 99),
            -float(r.get("expected_daily_delta_booty") or 0),
        ))
    return dict(sorted(groups.items()))


def render_phase_a_row(a: dict) -> str:
    coords = a.get("coords") or [0, 0]
    name = (a.get("target_name") or "?")[:32]
    src = a.get("current_list_name") or "—"
    reason = a.get("reason") or "(dead)"
    return f"| ({coords[0]},{coords[1]}) | {name} | `{src}` | {reason} |"


def render_phase_active_row(a: dict) -> str:
    coords = a.get("coords") or [0, 0]
    extra = a.get("extra") or {}
    name = (a.get("target_name") or extra.get("target_name") or "?")[:32]
    if a.get("action") == "ADD_TO_LIST":
        wave_str = f"{extra.get('wave_index', '?')}/{extra.get('of_total_waves', '?')}"
        unit = extra.get("recommended_unit_display") or "?"
        count = extra.get("recommended_count") or 1
        composition = f"{count}× {unit}"
        arrival = extra.get("arrival_min")
        arrival_str = f"T+{arrival:.0f}min" if isinstance(arrival, (int, float)) else "?"
        haul = extra.get("expected_haul_for_this_wave") or 0
        booty = extra.get("expected_daily_booty") or 0
        src = extra.get("current_list_name") or "—"
    else:  # MOVE_SLOT cleanup
        wave_str = "—"
        composition = "(remove)"
        arrival_str = "—"
        haul = 0
        booty = 0
        src = extra.get("current_list_name") or "—"
    delta = a.get("expected_daily_delta_booty") or 0
    return (
        f"| ({coords[0]},{coords[1]}) | {name} | wave **{wave_str}** | "
        f"{composition} | {arrival_str} | {int(haul)} | {int(booty)} | {int(delta):+d} | `{src}` |"
    )


def render_phase_b_summary(phase_b_actions: list[dict], top_n: int = 30) -> list[str]:
    """Build the per-target wave-plan summary for the top N targets by total expected_haul."""
    # Group actions (waves) by target coord
    by_target: dict[tuple, list[dict]] = defaultdict(list)
    for a in phase_b_actions:
        if a.get("action") != "ADD_TO_LIST":
            continue
        coord = tuple(a.get("coords") or [])
        if coord:
            by_target[coord].append(a)
    # Total expected daily booty per target = sum over waves
    target_stats = []
    for coord, waves in by_target.items():
        waves_sorted = sorted(waves, key=lambda w: (w.get("extra") or {}).get("wave_index", 99))
        total_booty = sum(
            float((w.get("extra") or {}).get("expected_daily_booty") or 0)
            for w in waves_sorted
        )
        target_name = (waves_sorted[0].get("extra") or {}).get("target_name") or "?"
        avg_loot = max(
            ((w.get("extra") or {}).get("expected_haul_for_this_wave") or 0)
            for w in waves_sorted
        )
        target_stats.append((coord, target_name, avg_loot, total_booty, waves_sorted))
    target_stats.sort(key=lambda t: -t[3])
    top = target_stats[:top_n]
    lines: list[str] = []
    lines.append("| Coord | Target | avg_loot | Wave 1 | Wave 2 | Wave 3 | Wave 4 | Total daily booty |")
    lines.append("|---|---|---:|---|---|---|---|---:|")
    for coord, name, avg_loot, total_booty, waves in top:
        cells = []
        for idx in range(4):
            if idx < len(waves):
                ex = waves[idx].get("extra") or {}
                cells.append(
                    f"{ex.get('recommended_list_owner', '?')}-"
                    f"{ex.get('recommended_unit_display', '?')} T+{(ex.get('arrival_min') or 0):.0f}"
                )
            else:
                cells.append("—")
        lines.append(
            f"| ({coord[0]},{coord[1]}) | {name[:28]} | {int(avg_loot)} | "
            f"{cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | {int(total_booty)} |"
        )
    return lines


def render_phase(
    md: list[str],
    *,
    phase_letter: str,
    phase_title: str,
    blurb: str,
    actions: list[dict],
    minutes: int,
    row_renderer,
    table_header: str,
) -> None:
    add = md.append
    add(f"## Phase {phase_letter} — {phase_title}")
    add("")
    add(f"Total: **{len(actions)} actions** (~{minutes} min). {blurb}")
    add("")
    if not actions:
        add("_No actions in this phase._")
        add("")
        return
    groups = group_by_dest(actions)
    phase_idx = 1
    for dest, rows in groups.items():
        if not rows:
            continue
        for chunk_i, chunk_rows in chunk(rows, 50):
            sub_label = f"{phase_letter}{phase_idx}"
            if len(rows) > 50:
                sub_label += f" (batch {chunk_i + 1})"
            add(f"### Phase {sub_label} — `{dest}` ({len(chunk_rows)} rows)")
            add("")
            add(table_header)
            for row in chunk_rows:
                add(row_renderer(row))
            add("")
            phase_idx += 1


def main() -> None:
    diff_path = latest_diff_json()
    iso = iso_from_filename(diff_path)
    print(f"Reading: {diff_path}", file=sys.stderr)
    d = json.load(diff_path.open(encoding="utf-8"))
    rebal = d.get("rebalance_plan") or {}
    if not rebal.get("enabled"):
        raise SystemExit("rebalance_plan disabled — re-run optimizer with --rebalance")
    summary = rebal.get("summary") or {}

    phase_a = rebal.get("phase_a_dead_relocations") or []
    phase_b = rebal.get("phase_b_high_mid_moves") or []
    phase_c = rebal.get("phase_c_inactive_moves") or []

    # Detect leftover v4.0 LOCAL lists so the operator can delete them.
    legacy_local_lists = detect_legacy_local_lists()

    md: list[str] = []
    add = md.append

    add(f"# Rebalance Execution Checklist — {iso}")
    add("")
    add("Wave-stacking plan (v5.0). All steps are manual in the Travian Plus farm-list "
        "UI; this checklist is read-only. Phases A → B → C are sequential; Phase D is "
        "the Send All sequencing config done once at the end.")
    add("")

    # Pre-flight
    add("## Pre-flight (do before any other phase)")
    add("")
    if legacy_local_lists:
        add("### Pre-flight 0 — Delete v4.0-era LOCAL lists (superseded)")
        add("")
        add("The v4.0 mission created LOCAL lists for V4/V5/V6/V7. v5.0 supersedes that "
            "model with wave-stacking — V4-V7 are no longer LOCAL-role villages. **Delete "
            "these lists in Travian UI** (or leave empty if you prefer to keep the slots):")
        add("")
        for ln in legacy_local_lists:
            add(f"- [ ] Delete `{ln}` (or empty it; it's superseded by the new HIGH/MID lists)")
        add("")

    add("### Pre-flight A — Create 5 empty DEAD pool lists")
    add("")
    add("Open each owner village's farm-list panel and create an empty list. Leave Send All "
        "**OFF** for every DEAD list — they are write-only destinations for sidelined farms.")
    add("")
    dead_owners = sorted({
        a.get("recommended_list_owner") for a in phase_a
        if a.get("recommended_list_owner")
    })
    for owner in dead_owners:
        add(f"- [ ] `{owner}-DEAD` — owner {owner} — Send All **OFF**")
    add("")

    # Active lists to create (from new_lists_to_create minus DEAD pools)
    new_lists = summary.get("new_lists_to_create") or []
    active_new = [n for n in new_lists if not n.endswith("-DEAD")]
    if active_new:
        add("### Pre-flight B — Create new active lists")
        add("")
        add("These lists are referenced by the wave plan but don't exist yet. Create them now "
            "(Send All **ON**) so Phase B can add slots into them without back-and-forth.")
        add("")
        for n in active_new:
            owner = n.split("-")[0] if "-" in n else "?"
            add(f"- [ ] `{n}` — owner {owner} — Send All **ON**")
        add("")

    # Phase A
    render_phase(
        md,
        phase_letter="A",
        phase_title="Dead-farm cleanup",
        blurb="For each row: open the **source list** → find slot at coord → duplicate to "
              "the **destination** `V{n}-DEAD` → delete from source.",
        actions=phase_a,
        minutes=format_minutes(len(phase_a)),
        row_renderer=render_phase_a_row,
        table_header=(
            "| Coord | Target | Source list | DEAD reason |\n"
            "|---|---|---|---|"
        ),
    )

    # Phase B summary (per-target wave plans)
    add("## Phase B — Build HIGH/MID lists (wave stacks)")
    add("")
    add(f"Total: **{len(phase_b)} actions** (~{format_minutes(len(phase_b))} min, highest value). "
        "Each ADD_TO_LIST is one wave for one target; the same target appears in multiple "
        "lists when its plan has 2-4 waves. MOVE_SLOT rows are cleanup steps for pre-existing "
        "slots not in the wave plan.")
    add("")

    add("### Phase B summary — per-target wave plans (top 30 by total daily booty)")
    add("")
    add("Read this table first to see the wave-stack structure at a glance. Each row is one "
        "target; columns show which village + unit fires each wave. T+N is arrival time in "
        "minutes after Send All.")
    add("")
    for line in render_phase_b_summary(phase_b, top_n=30):
        add(line)
    add("")

    # Phase B sub-sections by destination list
    render_phase(
        md,
        phase_letter="B",
        phase_title="Wave-plan execution (grouped by destination list)",
        blurb="Each row is one wave. For ADD_TO_LIST: open destination list → add slot at "
              "coord → set composition → activate (Send All ON). For MOVE_SLOT cleanup: "
              "delete from the named source list.",
        actions=phase_b,
        minutes=format_minutes(len(phase_b), seconds_per_action=45),
        row_renderer=render_phase_active_row,
        table_header=(
            "| Coord | Target | Wave | Composition | Arrival | Haul | Daily booty | Δ vs status quo | Source/Notes |\n"
            "|---|---|---|---|---|---:|---:|---:|---|"
        ),
    )

    # Phase C
    render_phase(
        md,
        phase_letter="C",
        phase_title="Build INACTIVE / Tail lists",
        blurb="Lower-value lists; same row semantics as Phase B.",
        actions=phase_c,
        minutes=format_minutes(len(phase_c)),
        row_renderer=render_phase_active_row,
        table_header=(
            "| Coord | Target | Wave | Composition | Arrival | Haul | Daily booty | Δ vs status quo | Source/Notes |\n"
            "|---|---|---|---|---|---:|---:|---:|---|"
        ),
    )

    # Phase D
    add("## Phase D — Send All sequencing setup")
    add("")
    add("Configure the Trigger All routine to fire in this order:")
    add("")
    add("1. All `V*-HIGH-*` lists (in any order — wave spacing is internal to each plan)")
    add("2. All `V*-MID-*` lists")
    add("3. `V*-AUTO-SCOUT`")
    add("4. All `V*-INACTIVE-*` lists")
    add("5. **SKIP** `V*-DEAD` and `V*-SLOW-*` lists")
    add("")

    # Verification
    add("## After all phases — sanity verification (24h after Phase B)")
    add("")
    add("Re-run `python scripts/raid_optimizer_diff_v3.py` (no `--rebalance`) and check:")
    add("")
    add("- [ ] Each top-30 multi-wave target shows non-zero `last_raid.bounty` "
        "(the wave-1 arrival should fire within the first cycle)")
    add("- [ ] No new `SPLIT_LIST` recommendations on the freshly-built lists")
    add("- [ ] BUDGET-vs-live diagnostic shows no new WARN cells appearing")
    add("- [ ] V3's wave-1 share (from the optimizer's rebalance summary) holds ≥50%")
    add("")

    # Summary
    add("## Rebalance plan summary")
    add("")
    add(f"- Targets analyzed: **{summary.get('targets_analyzed', '?')}**")
    add(f"- Targets with multi-wave plans (≥2 waves): "
        f"**{summary.get('targets_with_multi_wave', '?')}**")
    dist = summary.get("wave_distribution") or {}
    add(f"- Wave distribution: 1-wave **{dist.get('1_wave', 0)}**, "
        f"2-wave **{dist.get('2_wave', 0)}**, "
        f"3-wave **{dist.get('3_wave', 0)}**, "
        f"4-wave **{dist.get('4_wave', 0)}**")
    add(f"- Total wave slots (ADD_TO_LIST): **{summary.get('total_wave_slots', '?')}**")
    add(f"- Dead-farm relocations: **{summary.get('relocated_to_dead', len(phase_a))}**")
    add(f"- MOVE_SLOT cleanups: **{summary.get('move_slot_cleanups', 0)}**")
    add(f"- New lists to create: **{len(new_lists)}**")
    add(f"- Current estimated daily booty: **{summary.get('current_estimated_daily_booty', '?')} res**")
    add(f"- Post-rebalance estimated: **{summary.get('post_rebalance_estimated_daily_booty', '?')} res**")
    lift = summary.get('expected_lift_pct')
    if lift is not None:
        add(f"- Expected lift: **{lift:+.1f}%**")
    v3_wave1_pct = summary.get('v3_wave1_pct')
    if v3_wave1_pct is not None:
        add(f"- V3 wave-1 share: **{v3_wave1_pct:.1f}%** "
            f"(target ≥50%)")
    add("")

    out_path = OUT_DIR / f"execution-checklist-{iso}.md"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote: {out_path}")
    print(f"Phase A: {len(phase_a)} | Phase B: {len(phase_b)} | Phase C: {len(phase_c)}")
    if legacy_local_lists:
        print(f"Legacy LOCAL lists detected (cleanup listed in Pre-flight 0): {legacy_local_lists}")


if __name__ == "__main__":
    main()
