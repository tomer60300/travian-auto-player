"""Render an operator-facing execution-checklist-{ISO}.md from a diff JSON.

Reads ~/.travian/raid-optimizer/v3.4/diff-{ISO}.json (the latest one) and
emits ~/.travian/raid-optimizer/v3.4/execution-checklist-{ISO}.md.

Groups Phase A/B/C/D rows by *destination list* so the operator opens each
list once. Splits any phase with >50 actions into Phase X1/X2/X3 batches
of 50.
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

OUT_DIR = Path.home() / ".travian" / "raid-optimizer" / "v3.4"


def latest_diff_json() -> Path:
    files = sorted(OUT_DIR.glob("diff-*.json"))
    if not files:
        raise SystemExit("No diff-*.json snapshots found")
    return files[-1]


def iso_from_filename(p: Path) -> str:
    name = p.name
    return name[len("diff-"):-len(".json")]


def format_size_minutes(action_count: int, seconds_per_action: int = 30) -> int:
    return max(1, round(action_count * seconds_per_action / 60))


def chunk(items, n):
    for i in range(0, len(items), n):
        yield i // n, items[i:i + n]


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

    # Verify LOCAL constraints (Section 3.7)
    diagnostics: list[str] = []
    local_lists: dict[str, list[dict]] = defaultdict(list)
    for action in phase_b + phase_c:
        dest = action.get("recommended_list_name") or action.get("extra", {}).get("recommended_list_name", "")
        if "-LOCAL-" in dest:
            local_lists[dest].append(action)
    for lname, actions in local_lists.items():
        if len(actions) > 15:
            diagnostics.append(f"WARN: {lname} has {len(actions)} actions (cap is 15)")

    # Re-key by destination list
    def dest_of(a):
        d = a.get("recommended_list_name") or a.get("extra", {}).get("recommended_list_name")
        if not d:
            d = a.get("target_list_name") or a.get("dest_list_name") or "?"
        return d

    def group_by_dest(actions):
        groups: dict[str, list[dict]] = defaultdict(list)
        for a in actions:
            groups[dest_of(a)].append(a)
        for lname, rows in groups.items():
            rows.sort(key=lambda r: -float(r.get("expected_daily_delta_booty") or 0))
        return dict(sorted(groups.items()))

    def render_row_b_c(a, phase_letter: str) -> str:
        coords = a.get("coords") or a.get("target_coord") or [0, 0]
        name = a.get("target_name") or a.get("extra", {}).get("target_name") or "?"
        extra = a.get("extra") or {}
        unit = extra.get("recommended_unit_display") or a.get("recommended_unit") or "?"
        count = extra.get("recommended_count") or a.get("recommended_count") or 1
        rt = extra.get("round_trip_min") or a.get("round_trip_min")
        rt_str = f"{rt:.0f}min" if isinstance(rt, (int, float)) else "?"
        rate = extra.get("expected_raids_per_day") or a.get("expected_raids_per_day")
        rate_str = f"{rate:.1f}/d" if isinstance(rate, (int, float)) else "?"
        booty = extra.get("expected_daily_booty") or a.get("expected_daily_booty") or 0
        delta = a.get("expected_daily_delta_booty") or 0
        src = a.get("current_list_name") or extra.get("current_list_name") or "—"
        return (
            f"| ({coords[0]},{coords[1]}) | {name[:32]} | {src} | "
            f"{count}× {unit} | {rt_str} | {rate_str} | "
            f"{int(booty)} | {int(delta):+d} |"
        )

    def render_row_a(a) -> str:
        coords = a.get("coords") or a.get("target_coord") or [0, 0]
        name = a.get("target_name") or "?"
        src = a.get("current_list_name") or a.get("source_list_name") or "—"
        reason = a.get("reason") or "(dead)"
        return f"| ({coords[0]},{coords[1]}) | {name[:32]} | {src} | {reason} |"

    md: list[str] = []
    add = md.append

    add(f"# Rebalance Execution Checklist — {iso}")
    add("")

    total_min = summary.get('phase_a_total_min', None)
    workload = "~3.8 hours across 3-5 sessions" if total_min is None else f"{total_min} min total"
    add(f"Total operator workload: {workload}.  All steps are manual in the Travian Plus farm-list UI; this checklist is read-only.")
    add("")

    # Pre-flight
    add("## Pre-flight (do before any other phase)")
    add("")
    add("Create the new lists Path 3 needs.")
    add("")
    add("### Pre-flight A — Create 5 empty DEAD pool lists")
    add("")
    add("Open each owner village's farm-list panel and create an empty list.  Leave Send All **OFF** for every DEAD list — they are write-only destinations for sidelined farms.")
    add("")
    for vlabel in ["V1", "V2", "V3", "V4", "V6"]:
        add(f"- [ ] `{vlabel}-DEAD` — owner {vlabel} — Send All **OFF**")
    add("")
    add("### Pre-flight B — Create 4 LOCAL lists")
    add("")
    add("Same panel, owner village = the LOCAL village itself.  Send All **ON** so these fire every Trigger All cycle.")
    add("")
    for vlabel in ["V4", "V5", "V6", "V7"]:
        add(f"- [ ] `{vlabel}-LOCAL-Clubs` — owner {vlabel} — Send All **ON**")
    add("")

    # Phase A
    add("## Phase A — Dead-farm cleanup")
    add("")
    add(f"Total: **{len(phase_a)} relocations** (~{format_size_minutes(len(phase_a))} min, lowest stakes).")
    add("")
    add("For each row: open the **source list** → find slot at coord → duplicate to the **destination** `V{n}-DEAD` → delete from source.")
    add("")
    a_groups = group_by_dest(phase_a)
    phase_index = 1
    rendered = 0
    for dest, rows in a_groups.items():
        if not rows:
            continue
        # Sub-split inside Phase A if a single dest >50 rows
        for chunk_i, chunk_rows in chunk(rows, 50):
            sub_label = f"A{phase_index}" + (f" (batch {chunk_i+1})" if chunk_i > 0 or len(rows) > 50 else "")
            add(f"### Phase {sub_label} — `{dest}` ({len(chunk_rows)} rows)")
            add("")
            add("| Coord | Target | Source list | DEAD reason |")
            add("|---|---|---|---|")
            for a in chunk_rows:
                add(render_row_a(a))
            add("")
            rendered += len(chunk_rows)
            phase_index += 1

    # Phase B
    add("## Phase B — Build HIGH/MID/LOCAL lists")
    add("")
    add(f"Total: **{len(phase_b)} moves** (~{format_size_minutes(len(phase_b))} min, highest value).")
    add("")
    add("For each row: open the **destination list** → duplicate slot from the source list → set the recommended unit/count → delete the slot from the source list (consolidation).")
    add("")
    b_groups = group_by_dest(phase_b)
    phase_index = 1
    for dest, rows in b_groups.items():
        if not rows:
            continue
        for chunk_i, chunk_rows in chunk(rows, 50):
            sub_label = f"B{phase_index}" + (f" (batch {chunk_i+1})" if chunk_i > 0 or len(rows) > 50 else "")
            add(f"### Phase {sub_label} — `{dest}` ({len(chunk_rows)} rows)")
            add("")
            add("| Coord | Target | Source list | Composition | Round-trip | Rate | Daily booty | Δ vs status quo |")
            add("|---|---|---|---|---|---|---|---|")
            for a in chunk_rows:
                add(render_row_b_c(a, "B"))
            add("")
            phase_index += 1

    # Phase C
    add("## Phase C — Build INACTIVE / Tail lists")
    add("")
    add(f"Total: **{len(phase_c)} moves** (~{format_size_minutes(len(phase_c))} min).")
    add("")
    c_groups = group_by_dest(phase_c)
    phase_index = 1
    for dest, rows in c_groups.items():
        if not rows:
            continue
        for chunk_i, chunk_rows in chunk(rows, 50):
            sub_label = f"C{phase_index}" + (f" (batch {chunk_i+1})" if chunk_i > 0 or len(rows) > 50 else "")
            add(f"### Phase {sub_label} — `{dest}` ({len(chunk_rows)} rows)")
            add("")
            add("| Coord | Target | Source list | Composition | Round-trip | Rate | Daily booty | Δ vs status quo |")
            add("|---|---|---|---|---|---|---|---|")
            for a in chunk_rows:
                add(render_row_b_c(a, "C"))
            add("")
            phase_index += 1

    # Phase D
    add("## Phase D — Send All sequencing setup")
    add("")
    add("Configure the Trigger All routine to fire in this order:")
    add("")
    add("1. All `V*-HIGH-*` lists (V3 first, then V1, V2)")
    add("2. All `V*-LOCAL-Clubs` lists (V4, V5, V6, V7)")
    add("3. All `V*-MID-*` lists")
    add("4. `V*-AUTO-SCOUT`")
    add("5. `V*-INACTIVE-*-Top`")
    add("6. `V*-INACTIVE-*` and `*-Tail`")
    add("7. **SKIP** `V*-DEAD` and `V*-SLOW-*`")
    add("")

    # Verification
    add("## After all phases — sanity verification (24h after Phase B)")
    add("")
    add("Re-run `python scripts/raid_optimizer_diff_v3.py` (no `--rebalance`) and check:")
    add("")
    add("- [ ] Each `V*-LOCAL-Clubs` list shows `total_raids > 0`")
    add("- [ ] (31, 83) shows `last_raid` bounty > 50 res from V6")
    add("- [ ] No new `SPLIT_LIST` recommendations on the freshly-built lists")
    add("- [ ] BUDGET-vs-live diagnostic shows no new WARN cells appearing")
    add("")

    # Summary
    add("## Rebalance plan summary")
    add("")
    add(f"- Targets analyzed: **{summary.get('targets_analyzed', '?')}**")
    add(f"- Dead-farm relocations: **{summary.get('relocated_to_dead', len(phase_a))}**")
    add(f"- Active relocations: **{summary.get('moved_to_active', len(phase_b) + len(phase_c))}**")
    add(f"- New lists to create: **{len(summary.get('new_lists_to_create', []))}**")
    add(f"- Current estimated daily booty: **{summary.get('current_estimated_daily_booty', '?')} res**")
    add(f"- Post-rebalance estimated: **{summary.get('post_rebalance_estimated_daily_booty', '?')} res**")
    lift = summary.get('expected_lift_pct')
    if lift is not None:
        add(f"- Expected lift: **{lift:+.1f}%**")
    add("")

    if diagnostics:
        add("## Diagnostics / warnings")
        add("")
        for d in diagnostics:
            add(f"- {d}")
        add("")

    out_path = OUT_DIR / f"execution-checklist-{iso}.md"
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote: {out_path}")
    print(f"Phase A: {len(phase_a)} actions, Phase B: {len(phase_b)}, Phase C: {len(phase_c)}")
    if diagnostics:
        print("Diagnostics:")
        for d in diagnostics:
            print(f"  {d}")


if __name__ == "__main__":
    main()
