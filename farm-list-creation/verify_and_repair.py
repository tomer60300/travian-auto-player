"""Slow, read-only verification of all 12 V3 farm lists + repair of missing entries.

For each list: read back from the server (authoritative), compare to v3_farm_lists.json.
Attempt to add any MISSING entry (2 Clubswingers, disabled). If an add still fails,
fetch the map tile to classify WHY: target village deleted, changed ownership, or
other. Writes verify-repair-report.md with a human-readiness verdict.

Run AFTER populate_highrisk.py has fully completed (never concurrently — one session,
no parallel game-server requests).

Run:  uv run python farm-list-creation/verify_and_repair.py
Env:  TRAVIAN_USERNAME, TRAVIAN_PASSWORD, (optional) TRAVIAN_SERVER.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import orchestrate as O  # noqa: E402

READ_GAP = (6.0, 16.0)        # between list reads
REPAIR_GAP = (8.0, 18.0)      # between repair adds
TILE_GAP = (6.0, 14.0)        # between tile-detail checks
START = time.monotonic()


def emin() -> float:
    return (time.monotonic() - START) / 60.0


async def sleep_rand(lo, hi, label):
    d = random.uniform(lo, hi)
    O.slog(f"VR sleep {d:.1f}s ({label}) elapsed={emin():.1f}min")
    await asyncio.sleep(d)


async def browse(session, v3_id, kind, p):
    nav = session.http_client.navigator
    O.captcha_check(session, f"pre:browse:{kind}")
    try:
        if kind == "farmlist":
            await nav.navigate_to_farm_list(v3_id)
        elif kind == "map":
            await nav.navigate_to_map(v3_id)
        else:
            await nav.idle_browse(v3_id)
        p["api_call_count"] += 1
        O.alog(f"VR browse:{kind} OK count={p['api_call_count']}")
    except Exception as e:
        O.elog(f"VR browse:{kind} non-critical: {e}")
    O.captcha_check(session, f"post:browse:{kind}")


async def classify_tile(session, v3_id, x, y, expected_player, p):
    """Read map tile to explain why a target can't be added."""
    await browse(session, v3_id, "map", p)
    try:
        info = await O.call_api(
            session, lambda: session.scout_service.get_tile_details(x, y), f"VR:tile({x},{y})", p
        )
    except Exception as e:
        return {"reason": "tile_lookup_failed", "detail": str(e)[:160]}
    has_village = bool(getattr(info, "village_id", 0)) and not getattr(info, "is_abandoned", False)
    player = getattr(info, "player_name", "") or ""
    if not has_village:
        return {"reason": "village_gone", "detail": f"no active village at ({x},{y}); oasis={getattr(info,'is_oasis',False)} abandoned={getattr(info,'is_abandoned',False)}"}
    if expected_player and player and player.strip().lower() != expected_player.strip().lower():
        return {"reason": "owner_changed", "detail": f"now owned by '{player}' (expected '{expected_player}')"}
    return {"reason": "exists_add_failed", "detail": f"village '{getattr(info,'village_name','')}' owner '{player}' present but add rejected"}


async def main() -> int:
    data = json.loads(O.DATA_JSON.read_text(encoding="utf-8"))
    p = O.load_progress()
    O.olog("=== VERIFY & REPAIR START (slow, read-only + targeted repair) ===")

    session = None
    summary = {"lists": [], "repaired": [], "unfixable": [], "v3_ok": False}
    try:
        session, v3_id = await O.connect_and_locate(p)
        summary["v3_ok"] = True
        summary["v3_id"] = v3_id
        await browse(session, v3_id, "idle", p)

        all_lists = await O.call_api(
            session, lambda: session.farm_service.get_all_farm_lists(), "VR:get_all", p
        )
        by_name = {fl.name: fl for fl in all_lists}

        # ── Pass 1: slow read-back of every list ──────────────────────
        live = {}  # name -> {present:set, active:int, bad:int, owner:int, id:int}
        for spec in data["lists"]:
            name = spec["name"]
            fl = by_name.get(name)
            if fl is None:
                live[name] = None
                summary["lists"].append({"name": name, "exists": False})
                O.olog(f"VR: list '{name}' MISSING entirely")
                continue
            flv = await O.call_api(
                session, lambda fl=fl: session.farm_service.get_farm_list(fl.id), f"VR:read[{name}]", p
            )
            present = {(s.target.x, s.target.y): s for s in flv.slots}
            active = sum(1 for s in flv.slots if s.is_active)
            bad = sum(1 for s in flv.slots if not (s.troop.t1 == 2 and s.troop.total == 2))
            live[name] = {"present": present, "id": fl.id, "owner": fl.owner_village.id}
            summary["lists"].append(
                {
                    "name": name,
                    "exists": True,
                    "owner_ok": fl.owner_village.id == v3_id,
                    "present": len(present),
                    "expected": len(spec["entries"]),
                    "active": active,
                    "bad_troops": bad,
                }
            )
            O.olog(
                f"VR read '{name}': present={len(present)}/{len(spec['entries'])} "
                f"active={active} bad_troops={bad} owner_ok={fl.owner_village.id == v3_id}"
            )
            await sleep_rand(*READ_GAP, f"after read {name}")
            if random.random() < 0.35:
                await browse(session, v3_id, "idle", p)

        # ── Pass 2: repair missing entries ────────────────────────────
        units = O.raid_units()
        for spec in data["lists"]:
            name = spec["name"]
            l = live.get(name)
            if not l:
                continue
            missing = [e for e in spec["entries"] if (e["x"], e["y"]) not in l["present"]]
            if not missing:
                continue
            O.olog(f"VR repair '{name}': {len(missing)} missing -> attempting add")
            await browse(session, v3_id, "farmlist", p)
            for e in missing:
                x, y = e["x"], e["y"]
                try:
                    await O.call_api(
                        session,
                        lambda x=x, y=y: session.farm_service.add_slot(
                            l["id"], x=x, y=y, units=units, active=False, force=False
                        ),
                        f"VR:repair[{name}]({x},{y})",
                        p,
                    )
                    summary["repaired"].append({"list": name, "x": x, "y": y})
                    O.olog(f"VR repaired {name}({x},{y})")
                    await sleep_rand(*REPAIR_GAP, f"after repair {name}({x},{y})")
                except O.CaptchaDetected:
                    raise
                except Exception as e2:
                    O.elog(f"VR repair FAILED {name}({x},{y}): {str(e2)[:160]} -> classifying tile")
                    await sleep_rand(*TILE_GAP, "before tile check")
                    cls = await classify_tile(session, v3_id, x, y, e.get("player", ""), p)
                    summary["unfixable"].append({"list": name, "x": x, "y": y, **cls})
                    O.olog(f"VR unfixable {name}({x},{y}): {cls['reason']} — {cls['detail']}")
                    await sleep_rand(*TILE_GAP, "after tile check")

        # ── Pass 3: final read-back of repaired lists ─────────────────
        touched = sorted({r["list"] for r in summary["repaired"]})
        for name in touched:
            fl = live[name]
            flv = await O.call_api(
                session, lambda fid=fl["id"]: session.farm_service.get_farm_list(fid), f"VR:final[{name}]", p
            )
            active = sum(1 for s in flv.slots if s.is_active)
            bad = sum(1 for s in flv.slots if not (s.troop.t1 == 2 and s.troop.total == 2))
            spec = next(s for s in data["lists"] if s["name"] == name)
            for it in summary["lists"]:
                if it["name"] == name:
                    it["present"] = len(flv.slots)
                    it["active"] = active
                    it["bad_troops"] = bad
            O.olog(f"VR final '{name}': present={len(flv.slots)}/{len(spec['entries'])} active={active} bad={bad}")
            await sleep_rand(*READ_GAP, f"after final {name}")

        write_report(data, summary)
        O.olog("=== VERIFY & REPAIR COMPLETED ===")
        return 0
    except O.CaptchaDetected as e:
        O.elog(f"VR STOP captcha: {e}")
        write_report(data, summary, note=f"STOPPED_CAPTCHA: {e}")
        return 2
    except Exception as e:
        O.elog(f"VR STOP exception: {e}\n{traceback.format_exc()}")
        write_report(data, summary, note=f"STOPPED_ERROR: {e}")
        return 1
    finally:
        if session is not None:
            try:
                await session.disconnect()
                O.olog("VR graceful disconnect done")
            except Exception:
                pass


def write_report(data, summary, note=""):
    total_present = sum(i.get("present", 0) for i in summary["lists"] if i.get("exists"))
    total_expected = sum(len(s["entries"]) for s in data["lists"])
    total_active = sum(i.get("active", 0) for i in summary["lists"] if i.get("exists"))
    total_bad = sum(i.get("bad_troops", 0) for i in summary["lists"] if i.get("exists"))
    lists_ok = sum(1 for i in summary["lists"] if i.get("exists"))
    lines = [
        "# Verify & Repair — Report",
        "",
        f"- Generated: {O.now_iso()}  | wall-clock this run: {emin():.1f} min",
        f"- V3 sender village reachable & owned: {summary.get('v3_ok')} (id={summary.get('v3_id')})",
        f"- Lists existing: {lists_ok}/12",
        f"- Entries present: {total_present}/{total_expected}",
        f"- Entries ACTIVE (must be 0): {total_active}",
        f"- Entries with wrong troops (must be 0): {total_bad}",
        f"- Repaired this run: {len(summary['repaired'])}",
        f"- Unfixable (target gone/changed/other): {len(summary['unfixable'])}",
    ]
    if note:
        lines.append(f"- Note: **{note}**")
    lines += ["", "## Per-list", "", "| List | exists | owner_ok | present/expected | active | bad_troops |", "|---|---|---|---|---|---|"]
    for i in summary["lists"]:
        if not i.get("exists"):
            lines.append(f"| {i['name']} | NO | - | - | - | - |")
        else:
            lines.append(
                f"| {i['name']} | yes | {i.get('owner_ok')} | {i.get('present')}/{i.get('expected')} "
                f"| {i.get('active')} | {i.get('bad_troops')} |"
            )
    if summary["unfixable"]:
        lines += ["", "## Unfixable targets (verified on map)", "", "| List | coord | reason | detail |", "|---|---|---|---|"]
        for u in summary["unfixable"]:
            lines.append(f"| {u['list']} | ({u['x']},{u['y']}) | {u['reason']} | {u['detail']} |")
    (HERE / "verify-repair-report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
