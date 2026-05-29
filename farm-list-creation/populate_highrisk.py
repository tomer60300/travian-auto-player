"""Populate the 5 HighRisk farm lists (decision C: 2 Clubswingers/entry, DISABLED).

Follow-up to orchestrate.py. The 5 HighRisk lists already exist on V3 but are empty
(Travian rejects zero-troop slots). Per user decision C, each entry gets {t1:2},
active=False. Slower pacing + real in-game browsing noise between operations.

Reuses the validated helpers in orchestrate.py (connect, captcha polling, call_api
backoff, logging, progress). Only touches is_high_risk lists. Skips the 1 V3-Big
entry (user said skip). No sends.

Run:  uv run python farm-list-creation/populate_highrisk.py
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

import orchestrate as O  # noqa: E402  (sets up src path, reuses helpers)

# Slower pace bands for this run (user: "do all slowly").
INTER_TARGET = (8.0, 20.0)
INTER_TARGET_LOW = (6.0, 10.0)     # if projecting absurdly long
JITTER = (30.0, 60.0)               # every 12-18 entries
INTER_LIST = (120.0, 240.0)
SESSION_BREAK = (300.0, 600.0)
BROWSE_EVERY = (8, 14)              # idle-browse cadence (entries)
PER_ENTRY_MIN = 0.35                # amortized minutes/entry for soft governor

START = time.monotonic()


def emin() -> float:
    return (time.monotonic() - START) / 60.0


def inter_target_band(remaining: int) -> tuple[float, float]:
    projected = emin() + remaining * PER_ENTRY_MIN
    if projected > 190:
        O.slog(f"HR governor: projected {projected:.0f}min > 190 -> LOW band")
        return INTER_TARGET_LOW
    return INTER_TARGET


async def browse(session, v3_id, kind: str, p: dict) -> None:
    """One game-browse action through the stealth chain (navigator GETs)."""
    nav = session.http_client.navigator
    O.captcha_check(session, f"pre:browse:{kind}")
    t0 = time.monotonic()
    try:
        if kind == "farmlist":
            await nav.navigate_to_farm_list(v3_id)
        elif kind == "map":
            await nav.navigate_to_map(v3_id)
        else:
            await nav.idle_browse(v3_id)
        ms = int((time.monotonic() - t0) * 1000)
        p["api_call_count"] += 1
        O.alog(f"browse:{kind} status=OK ms={ms} count={p['api_call_count']}")
        O.slog(f"browse:{kind} done elapsed={emin():.1f}min")
    except Exception as e:
        O.elog(f"browse:{kind} failed (non-critical): {e}")
    O.captcha_check(session, f"post:browse:{kind}")


async def sleep_rand(lo, hi, label):
    d = random.uniform(lo, hi)
    O.slog(f"HR sleep {d:.1f}s ({label}) band=({lo},{hi}) elapsed={emin():.1f}min")
    await asyncio.sleep(d)


async def populate_list(session, v3_id, spec, p, remaining_ref) -> None:
    name = spec["name"]
    entries = spec["entries"]
    units = O.raid_units()  # {t1:2, ...}  (decision C)

    all_lists = await O.call_api(
        session, lambda: session.farm_service.get_all_farm_lists(), "HR:get_all_farm_lists", p
    )
    fl = next((x for x in all_lists if x.name == name), None)
    if fl is None:
        raise RuntimeError(f"HighRisk list '{name}' not found (expected pre-created)")
    if fl.owner_village.id != v3_id:
        raise RuntimeError(f"List '{name}' owned by {fl.owner_village.id}, not V3 {v3_id}")
    list_id = fl.id
    present = {(s.target.x, s.target.y) for s in fl.slots}

    # Reset/refresh this list's progress state for the C re-run.
    ls = O.list_state(p, name)
    ls["remote_list_id"] = list_id
    ls["status"] = "in_progress"
    ls["entries_failed"] = []
    ls["entries_skipped_duplicate"] = []
    ls["entries_added"] = [
        {"x": x, "y": y, "added_at": O.now_iso(), "disabled_confirmed": False}
        for (x, y) in present
    ]
    O.save_progress(p)
    O.olog(f"HR populate '{name}' id={list_id} ({len(present)} already present, {len(entries)} target)")

    # Open the farm-list tab first (truthful referer for slot adds).
    await browse(session, v3_id, "farmlist", p)

    to_add = [e for e in entries if (e["x"], e["y"]) not in present]
    next_browse = random.randint(*BROWSE_EVERY)
    next_jitter = random.randint(12, 18)
    since_browse = since_jitter = 0

    for e in to_add:
        x, y = e["x"], e["y"]
        try:
            await O.call_api(
                session,
                lambda x=x, y=y: session.farm_service.add_slot(
                    list_id, x=x, y=y, units=units, active=False, force=False
                ),
                f"HR:add_slot[{name}]({x},{y})",
                p,
            )
        except O.CaptchaDetected:
            raise
        except O.NetworkError as e2:
            ls["entries_failed"].append({"x": x, "y": y, "error": str(e2)[:200]})
            O.elog(f"HR add_slot FAILED {name}({x},{y}): {str(e2)[:200]}")
            O.save_progress(p)
            continue
        ls["entries_added"].append(
            {"x": x, "y": y, "added_at": O.now_iso(), "disabled_confirmed": False}
        )
        remaining_ref[0] -= 1
        O.save_progress(p)

        since_jitter += 1
        since_browse += 1
        if since_jitter >= next_jitter:
            await sleep_rand(*JITTER, f"mid-list jitter {name}")
            since_jitter = 0
            next_jitter = random.randint(12, 18)
        elif since_browse >= next_browse:
            await browse(session, v3_id, random.choice(["idle", "idle", "map"]), p)
            since_browse = 0
            next_browse = random.randint(*BROWSE_EVERY)
            # re-open farm-list tab after wandering off, like a real player
            await browse(session, v3_id, "farmlist", p)
        else:
            lo, hi = inter_target_band(remaining_ref[0])
            await sleep_rand(lo, hi, f"inter-target {name}({x},{y})")

    # Per-list verify (decision C: every slot disabled + t1==2/total==2).
    flv = await O.call_api(
        session, lambda: session.farm_service.get_farm_list(list_id), f"HR:verify[{name}]", p
    )
    by = {(s.target.x, s.target.y): s for s in flv.slots}
    active = [c for c, s in by.items() if s.is_active]
    bad = [c for c, s in by.items() if not (s.troop.t1 == 2 and s.troop.total == 2)]
    for ent in ls["entries_added"]:
        s = by.get((ent["x"], ent["y"]))
        ent["disabled_confirmed"] = bool(s is not None and not s.is_active)
    ls["status"] = "completed"
    O.save_progress(p)
    O.olog(
        f"HR VERIFY '{name}': present={len(flv.slots)}/{len(entries)} active={len(active)} bad_troops={len(bad)}"
    )
    if active:
        O.elog(f"HR ANOMALY '{name}': {len(active)} ACTIVE slots: {active[:10]}")
    if bad:
        O.elog(f"HR TROOP MISMATCH '{name}': {len(bad)}: {bad[:10]}")


async def main() -> int:
    data = json.loads(O.DATA_JSON.read_text(encoding="utf-8"))
    hr_specs = [s for s in data["lists"] if s["is_high_risk"]]
    p = O.load_progress()
    O.olog(f"=== HIGHRISK POPULATE (decision C: t1=2, disabled) START === {len(hr_specs)} lists")

    session = None
    try:
        session, v3_id = await O.connect_and_locate(p)
        remaining_ref = [sum(len(s["entries"]) for s in hr_specs)]
        O.olog(f"HR entries to add (max): {remaining_ref[0]}")
        await browse(session, v3_id, "idle", p)  # settle in before working

        took_break = False
        for spec in hr_specs:
            await populate_list(session, v3_id, spec, p, remaining_ref)
            await sleep_rand(*INTER_LIST, f"inter-list after {spec['name']}")
            if not took_break and emin() > 120:
                O.olog("HR: taking one-time session break")
                await sleep_rand(*SESSION_BREAK, "session break")
                took_break = True

        # Final independent verification of all 5 HR lists.
        O.olog("HR FINAL VERIFICATION")
        all_lists = await O.call_api(
            session, lambda: session.farm_service.get_all_farm_lists(), "HR:final:get_all", p
        )
        by_name = {x.name: x for x in all_lists}
        report = []
        for spec in hr_specs:
            name = spec["name"]
            fl = by_name.get(name)
            if fl is None:
                report.append((name, "MISSING", "-", "-"))
                continue
            flv = await O.call_api(
                session, lambda fl=fl: session.farm_service.get_farm_list(fl.id),
                f"HR:final:verify[{name}]", p,
            )
            by = {(s.target.x, s.target.y): s for s in flv.slots}
            active = sum(1 for s in flv.slots if s.is_active)
            bad = sum(1 for s in flv.slots if not (s.troop.t1 == 2 and s.troop.total == 2))
            report.append((name, f"{len(flv.slots)}/{len(spec['entries'])}", active, bad))

        p["phase"] = "COMPLETED"
        O.save_progress(p)
        write_hr_report(data, p, report)
        O.olog("=== HIGHRISK POPULATE COMPLETED ===")
        return 0

    except O.CaptchaDetected as e:
        O.elog(f"HR STOP: captcha: {e}")
        O.olog(f"HR STOP: captcha: {e}")
        p["phase"] = "STOPPED_CAPTCHA"
        O.save_progress(p)
        write_hr_report(data, p, [], note=f"STOPPED_CAPTCHA: {e}")
        return 2
    except Exception as e:
        tb = traceback.format_exc()
        O.elog(f"HR STOP: exception: {e}\n{tb}")
        O.olog(f"HR STOP: exception: {e}")
        p["phase"] = "STOPPED_ERROR"
        O.save_progress(p)
        write_hr_report(data, p, [], note=f"STOPPED_ERROR: {e}")
        return 1
    finally:
        if session is not None:
            try:
                await session.disconnect()
                O.olog("HR graceful disconnect done")
            except Exception as e:
                O.elog(f"HR disconnect error: {e}")


def write_hr_report(data, p, report, note=""):
    lines = [
        "# HighRisk Population — Report (decision C: 2 Clubswingers, disabled)",
        "",
        f"- Generated: {O.now_iso()}",
        f"- Wall-clock this run: {emin():.1f} min",
        f"- Total game-server calls this run incl. browsing: {p['api_call_count']}",
    ]
    if note:
        lines.append(f"- Note: **{note}**")
    lines += ["", "| List | present/expected | active(should be 0) | bad_troops(should be 0) |", "|---|---|---|---|"]
    for row in report:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} | {row[3]} |")
    hr = [s["name"] for s in data["lists"] if s["is_high_risk"]]
    added = sum(len(p["lists"].get(n, {}).get("entries_added", [])) for n in hr)
    conf = sum(
        1 for n in hr for e in p["lists"].get(n, {}).get("entries_added", []) if e.get("disabled_confirmed")
    )
    failed = sum(len(p["lists"].get(n, {}).get("entries_failed", [])) for n in hr)
    lines += ["", f"- HighRisk entries added: {added}; disabled-confirmed: {conf}; failed: {failed}"]
    (HERE / "highrisk-report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
