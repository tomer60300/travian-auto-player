"""V3 Farm List creation orchestrator (POC Phase 1).

Creates 12 farm lists / 648 DISABLED entries on village V3 (42|17), strictly
through the app's stealth chain (TravianSession -> FarmListService -> HttpClient).

- Creation + read-back only. No sends, no scouting, no web server, no WS loops.
- Every entry created with active=False (the only disable path in the codebase).
- Captcha guard polled before AND after every game-server call -> STOP on detection.
- Human pacing + wall-clock governor target 90-180 min.
- Resumable from progress.json (coord-based, never duplicates).

Run:  uv run python farm-list-creation/orchestrate.py
Requires env: TRAVIAN_USERNAME, TRAVIAN_PASSWORD, (optional) TRAVIAN_SERVER.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "src"))

from travian_api.exceptions import NetworkError, SessionExpiredError  # noqa: E402
from travian_api.web.sessions import TravianSession  # noqa: E402

# ── Config ──────────────────────────────────────────────────────────────
SERVER = (os.environ.get("TRAVIAN_SERVER") or "https://ts2.x1.europe.travian.com").rstrip("/")
USERNAME = os.environ.get("TRAVIAN_USERNAME", "")
PASSWORD = os.environ.get("TRAVIAN_PASSWORD", "")
USER_ID = 99001
V3_X, V3_Y = 23, 88
DATA_JSON = REPO / "v3_farm_lists.json"
LOGS = HERE / "logs"
PROGRESS = HERE / "progress.json"
EXPECTED_COUNTS = [19, 45, 59, 15, 47, 37, 43, 54, 90, 89, 91, 59]

# Pace bands (seconds unless noted)
INTER_TARGET = (4.0, 12.0)
INTER_TARGET_LOW = (4.0, 6.0)        # governor: when projecting > 170 min
JITTER = (20.0, 45.0)                 # mid-list, every 15-25 entries
INTER_CREATE = (60.0, 180.0)
INTER_LIST = (60.0, 180.0)
SESSION_BREAK = (300.0, 600.0)        # 5-10 min, once, if cumulative > 120 min
PER_ENTRY_MIN = 0.21                  # amortized minutes/entry for the governor

START = time.monotonic()


class CaptchaDetected(Exception):
    pass


class MissingPassword(Exception):
    pass


# ── Logging ─────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _w(fname: str, msg: str) -> None:
    with (LOGS / fname).open("a", encoding="utf-8") as fh:
        fh.write(f"{now_iso()}  {msg}\n")


def olog(m: str) -> None:
    _w("orchestrator.log", m)


def alog(m: str) -> None:
    _w("api.log", m)


def slog(m: str) -> None:
    _w("stealth.log", m)


def elog(m: str) -> None:
    _w("errors.log", m)


def elapsed_min() -> float:
    return (time.monotonic() - START) / 60.0


# ── Progress ────────────────────────────────────────────────────────────
def load_progress() -> dict:
    if PROGRESS.exists():
        try:
            return json.loads(PROGRESS.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "started_at": now_iso(),
        "last_updated_at": now_iso(),
        "phase": "PHASE_4_EXECUTION",
        "lists": {},
        "api_call_count": 0,
        "errors": [],
    }


def save_progress(p: dict) -> None:
    p["last_updated_at"] = now_iso()
    PROGRESS.write_text(json.dumps(p, indent=2, ensure_ascii=False), encoding="utf-8")


def list_state(p: dict, name: str) -> dict:
    if name not in p["lists"]:
        p["lists"][name] = {
            "status": "pending",
            "remote_list_id": None,
            "entries_added": [],
            "entries_skipped_duplicate": [],
            "entries_failed": [],
        }
    return p["lists"][name]


def done_coords(ls: dict) -> set[tuple[int, int]]:
    s = {(e["x"], e["y"]) for e in ls["entries_added"]}
    s |= {(e["x"], e["y"]) for e in ls["entries_skipped_duplicate"]}
    return s


# ── Pacing ──────────────────────────────────────────────────────────────
async def sleep_rand(lo: float, hi: float, label: str) -> None:
    d = random.uniform(lo, hi)
    slog(f"sleep {d:.1f}s ({label}) band=({lo},{hi}) elapsed={elapsed_min():.1f}min")
    await asyncio.sleep(d)


def inter_target_band(remaining: int) -> tuple[float, float]:
    projected = elapsed_min() + remaining * PER_ENTRY_MIN
    if projected > 170:
        slog(f"governor: projected {projected:.0f}min > 170 -> LOW band")
        return INTER_TARGET_LOW
    return INTER_TARGET


# ── Captcha ─────────────────────────────────────────────────────────────
def captcha_check(session: TravianSession, where: str) -> None:
    g = session.http_client.captcha_guard
    blocked = g.is_blocked
    slog(f"captcha poll [{where}] blocked={blocked}")
    if blocked:
        st = g.status
        elog(f"CAPTCHA BLOCKED at {where}: {st}")
        raise CaptchaDetected(f"{where}: {st}")


async def captcha_cb(pattern, *, url="", status_code=0, response_snippet="") -> None:
    elog(
        f"CAPTCHA TRIGGER pattern={pattern} url={url} status={status_code} "
        f"snippet={(response_snippet or '')[:200]}"
    )
    slog(f"CAPTCHA TRIGGER pattern={pattern} status={status_code}")


# ── API call wrapper (backoff for 5xx/429; reconnect for drops) ──────────
async def call_api(session, factory, endpoint: str, p: dict, *, allow_reconnect=True):
    captcha_check(session, f"pre:{endpoint}")
    backoffs = [60, 180, 540]
    five_xx = 0
    rate_retry = 0
    reconnects = 0
    while True:
        t0 = time.monotonic()
        try:
            result = await factory()
            ms = int((time.monotonic() - t0) * 1000)
            p["api_call_count"] += 1
            alog(f"{endpoint} status=OK ms={ms} count={p['api_call_count']}")
            captcha_check(session, f"post:{endpoint}")
            return result
        except CaptchaDetected:
            raise
        except NetworkError as e:
            ms = int((time.monotonic() - t0) * 1000)
            code = e.status_code
            alog(f"{endpoint} status=ERR code={code} ms={ms} msg={str(e)[:160]}")
            captcha_check(session, f"post-err:{endpoint}")
            if code == 429:
                if rate_retry >= 1:
                    raise
                rate_retry += 1
                elog(f"429 on {endpoint} -> sleep 600s then retry once")
                await asyncio.sleep(600)
                continue
            if code and 500 <= code < 600:
                if five_xx >= len(backoffs):
                    raise
                b = backoffs[five_xx] + random.uniform(0, 20)
                five_xx += 1
                elog(f"5xx({code}) on {endpoint} -> backoff {b:.0f}s (attempt {five_xx})")
                await asyncio.sleep(b)
                continue
            raise  # other 4xx: not retryable here
        except (SessionExpiredError, ConnectionError, OSError) as e:
            ms = int((time.monotonic() - t0) * 1000)
            alog(f"{endpoint} status=DROP ms={ms} msg={str(e)[:160]}")
            if not allow_reconnect or reconnects >= 3:
                raise
            reconnects += 1
            elog(f"connection/session issue on {endpoint} -> reconnect #{reconnects}")
            await asyncio.sleep(30 + random.uniform(0, 15))
            try:
                await session.connect()
                session.http_client.captcha_guard.set_trigger_callback(captcha_cb)
                olog(f"reconnected (#{reconnects}) as {session.player_name}")
            except Exception as ce:
                elog(f"reconnect #{reconnects} failed: {ce}")
            continue


# ── Connect / locate V3 ─────────────────────────────────────────────────
async def connect_and_locate(p: dict) -> tuple[TravianSession, int]:
    if not PASSWORD:
        raise MissingPassword("TRAVIAN_PASSWORD is not set in the environment.")
    if not USERNAME:
        raise MissingPassword("TRAVIAN_USERNAME is not set in the environment.")

    olog(f"connecting to {SERVER} as {USERNAME} (user_id={USER_ID})")
    session = TravianSession(USER_ID, SERVER, USERNAME, PASSWORD)
    # Decouple captcha notifications from the web app (R2).
    session.http_client.captcha_guard.set_trigger_callback(captcha_cb)
    await session.connect()
    p["api_call_count"] += 1  # login flow (counted coarsely)
    olog(
        f"connected: player={session.player_name} tribe_id={session.tribe_id} "
        f"villages={len(session.auth_state.villages)}"
    )
    if session.tribe_id != 2:
        olog(f"WARNING: tribe_id={session.tribe_id} (expected 2=Teutons)")

    v3 = next(
        (v for v in session.auth_state.villages if v.x == V3_X and v.y == V3_Y),
        None,
    )
    if v3 is None:
        v3 = next((v for v in session.auth_state.villages if v.name == "V3"), None)
    if v3 is None:
        raise RuntimeError(
            f"V3 ({V3_X}|{V3_Y}) not found among villages: "
            f"{[(v.name, v.x, v.y) for v in session.auth_state.villages]}"
        )
    if (v3.x, v3.y) != (V3_X, V3_Y):
        raise RuntimeError(f"Village named V3 has coords ({v3.x}|{v3.y}), expected ({V3_X}|{V3_Y})")
    session.switch_village(v3.id)
    olog(f"V3 located: id={v3.id} name={v3.name!r} coords=({v3.x}|{v3.y}); active village set")
    return session, v3.id


def raid_units() -> dict[str, int]:
    u = {f"t{i}": 0 for i in range(1, 11)}
    u["t1"] = 2
    return u


# ── Per-list processing ─────────────────────────────────────────────────
async def process_list(session, v3_id: int, spec: dict, p: dict, remaining_ref: list[int]) -> None:
    name = spec["name"]
    is_hr = spec["is_high_risk"]
    entries = spec["entries"]
    ls = list_state(p, name)

    # Locate or create the list.
    all_lists = await call_api(
        session, lambda: session.farm_service.get_all_farm_lists(), "get_all_farm_lists", p
    )
    existing = next((fl for fl in all_lists if fl.name == name), None)
    if existing is not None:
        if existing.owner_village.id != v3_id:
            elog(
                f"LIST '{name}' exists but owner={existing.owner_village.id} != V3({v3_id}); "
                "STOP this list (manual prior creation)."
            )
            ls["status"] = "blocked_wrong_owner"
            save_progress(p)
            raise RuntimeError(f"List '{name}' owned by another village ({existing.owner_village.id})")
        list_id = existing.id
        ls["remote_list_id"] = list_id
        ls["status"] = "in_progress"
        # Seed already-present coords so resume never duplicates.
        present = {(s.target.x, s.target.y) for s in existing.slots}
        known = done_coords(ls)
        for (x, y) in present - known:
            ls["entries_added"].append(
                {"x": x, "y": y, "added_at": now_iso(), "disabled_confirmed": False}
            )
        olog(f"RESUME list '{name}' id={list_id} ({len(present)} slots already present)")
        save_progress(p)
    else:
        list_id = await call_api(
            session,
            lambda: session.farm_service.create_farm_list(v3_id, name),
            f"create_farm_list[{name}]",
            p,
        )
        if not list_id:
            raise RuntimeError(f"create_farm_list returned falsy id for '{name}'")
        ls["remote_list_id"] = list_id
        ls["status"] = "in_progress"
        olog(f"CREATED list '{name}' id={list_id} (high_risk={is_hr})")
        save_progress(p)
        await sleep_rand(*INTER_CREATE, f"inter-create after {name}")

    # Add entries.
    already = done_coords(ls)
    failed = {(e["x"], e["y"]) for e in ls["entries_failed"]}
    to_add = [e for e in entries if (e["x"], e["y"]) not in already and (e["x"], e["y"]) not in failed]
    olog(f"list '{name}': {len(to_add)} entries to add ({len(already)} already done)")

    next_jitter = random.randint(15, 25)
    since_jitter = 0
    units = None if is_hr else raid_units()

    for e in to_add:
        x, y = e["x"], e["y"]
        try:
            await call_api(
                session,
                lambda x=x, y=y: session.farm_service.add_slot(
                    list_id, x=x, y=y, units=units, active=False, force=False
                ),
                f"add_slot[{name}]({x},{y})",
                p,
            )
        except CaptchaDetected:
            raise
        except NetworkError as e2:
            msg = str(e2).lower()
            if "duplicate" in msg or "already" in msg:
                ls["entries_skipped_duplicate"].append({"x": x, "y": y, "added_at": now_iso()})
                elog(f"duplicate add_slot {name}({x},{y}): {str(e2)[:120]}")
            else:
                ls["entries_failed"].append({"x": x, "y": y, "error": str(e2)[:200]})
                elog(f"add_slot FAILED {name}({x},{y}): {str(e2)[:200]}")
            save_progress(p)
            continue
        ls["entries_added"].append(
            {"x": x, "y": y, "added_at": now_iso(), "disabled_confirmed": False}
        )
        remaining_ref[0] -= 1
        save_progress(p)

        since_jitter += 1
        if since_jitter >= next_jitter:
            await sleep_rand(*JITTER, f"mid-list jitter in {name}")
            since_jitter = 0
            next_jitter = random.randint(15, 25)
        else:
            lo, hi = inter_target_band(remaining_ref[0])
            await sleep_rand(lo, hi, f"inter-target {name}({x},{y})")

    # Per-list verify read-back (authoritative).
    await verify_one_list(session, list_id, spec, p, mark_confirmed=True)
    ls["status"] = "completed"
    save_progress(p)
    olog(f"list '{name}' COMPLETED (added={len(ls['entries_added'])} failed={len(ls['entries_failed'])})")


# ── Verification ────────────────────────────────────────────────────────
async def verify_one_list(session, list_id: int, spec: dict, p: dict, *, mark_confirmed: bool) -> dict:
    name = spec["name"]
    is_hr = spec["is_high_risk"]
    expected = len(spec["entries"])
    fl = await call_api(
        session, lambda: session.farm_service.get_farm_list(list_id), f"verify[{name}]", p
    )
    by_coord = {(s.target.x, s.target.y): s for s in fl.slots}
    active = [c for c, s in by_coord.items() if s.is_active]
    if is_hr:
        bad_troops = [c for c, s in by_coord.items() if s.troop.total != 0]
    else:
        bad_troops = [c for c, s in by_coord.items() if not (s.troop.t1 == 2 and s.troop.total == 2)]

    result = {
        "name": name,
        "list_id": list_id,
        "expected": expected,
        "present": len(fl.slots),
        "active_slots": len(active),
        "bad_troops": len(bad_troops),
        "high_risk": is_hr,
    }
    olog(
        f"VERIFY '{name}': present={result['present']}/{expected} active={result['active_slots']} "
        f"bad_troops={result['bad_troops']}"
    )
    if active:
        elog(f"ANOMALY '{name}': {len(active)} slots are ACTIVE (expected all disabled): {active[:10]}")
    if bad_troops:
        elog(f"TROOP MISMATCH '{name}': {len(bad_troops)} slots: {bad_troops[:10]}")

    if mark_confirmed:
        ls = list_state(p, name)
        for e in ls["entries_added"]:
            s = by_coord.get((e["x"], e["y"]))
            e["disabled_confirmed"] = bool(s is not None and not s.is_active)
        # coords expected by data but missing on server -> record failed
        present_coords = set(by_coord)
        for ent in spec["entries"]:
            c = (ent["x"], ent["y"])
            if c not in present_coords and c not in {(f["x"], f["y"]) for f in ls["entries_failed"]}:
                ls["entries_failed"].append({"x": c[0], "y": c[1], "error": "missing after add"})
        save_progress(p)
    return result


async def verify_all(session, data: dict, p: dict) -> list[dict]:
    olog("FINAL VERIFICATION (independent read-only pass)")
    all_lists = await call_api(
        session, lambda: session.farm_service.get_all_farm_lists(), "final:get_all_farm_lists", p
    )
    by_name = {fl.name: fl for fl in all_lists}
    results = []
    for spec in data["lists"]:
        name = spec["name"]
        fl = by_name.get(name)
        if fl is None:
            results.append({"name": name, "error": "MISSING LIST"})
            elog(f"FINAL: list '{name}' MISSING")
            continue
        if fl.owner_village.id != p["lists"].get(name, {}).get("remote_list_id_owner", fl.owner_village.id):
            pass
        results.append(await verify_one_list(session, fl.id, spec, p, mark_confirmed=False))
    return results


# ── Main ────────────────────────────────────────────────────────────────
async def main() -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    data = json.loads(DATA_JSON.read_text(encoding="utf-8"))
    p = load_progress()
    save_progress(p)
    olog(f"=== PHASE 4 EXECUTION START === server={SERVER} user={USERNAME}")

    session = None
    try:
        session, v3_id = await connect_and_locate(p)

        total_entries = sum(len(s["entries"]) for s in data["lists"])
        done_total = sum(
            len(done_coords(list_state(p, s["name"]))) for s in data["lists"]
        )
        remaining_ref = [max(0, total_entries - done_total)]
        olog(f"entries total={total_entries} remaining={remaining_ref[0]}")

        took_break = False
        for spec in data["lists"]:
            if list_state(p, spec["name"]).get("status") == "completed":
                olog(f"skip already-completed list '{spec['name']}'")
                continue
            await process_list(session, v3_id, spec, p, remaining_ref)
            await sleep_rand(*INTER_LIST, f"inter-list after {spec['name']}")
            if not took_break and elapsed_min() > 120:
                olog("taking the one-time session break")
                await sleep_rand(*SESSION_BREAK, "session break")
                took_break = True

        results = await verify_all(session, data, p)
        p["phase"] = "COMPLETED"
        save_progress(p)
        write_report(data, p, results, status="COMPLETED")
        olog("=== PHASE 4 EXECUTION COMPLETED ===")
        return 0

    except CaptchaDetected as e:
        elog(f"STOP: captcha detected: {e}")
        olog(f"STOP: captcha detected: {e}")
        p["phase"] = "STOPPED_CAPTCHA"
        p["errors"].append({"at": now_iso(), "type": "captcha", "detail": str(e)})
        save_progress(p)
        write_report(data, p, [], status="STOPPED_CAPTCHA", note=str(e))
        return 2
    except MissingPassword as e:
        elog(f"STOP: missing credential: {e}")
        olog(f"STOP: missing credential: {e}")
        p["phase"] = "BLOCKED_NO_PASSWORD"
        p["errors"].append({"at": now_iso(), "type": "missing_password", "detail": str(e)})
        save_progress(p)
        write_report(data, p, [], status="BLOCKED_NO_PASSWORD", note=str(e))
        return 3
    except Exception as e:
        tb = traceback.format_exc()
        elog(f"STOP: unexpected exception: {e}\n{tb}")
        olog(f"STOP: unexpected exception: {e}")
        p["phase"] = "STOPPED_ERROR"
        p["errors"].append({"at": now_iso(), "type": "exception", "detail": str(e)})
        save_progress(p)
        write_report(data, p, [], status="STOPPED_ERROR", note=str(e))
        return 1
    finally:
        if session is not None:
            try:
                await session.disconnect()
                olog("graceful disconnect done")
            except Exception as e:
                elog(f"disconnect error: {e}")


def write_report(data: dict, p: dict, results: list[dict], *, status: str, note: str = "") -> None:
    lines = [
        "# V3 Farm List Creation — Final Report",
        "",
        f"- Status: **{status}**",
        f"- Generated: {now_iso()}",
        f"- Server: {SERVER}",
        f"- Wall-clock: {elapsed_min():.1f} min",
        f"- Total game-server API calls: {p['api_call_count']}",
    ]
    if note:
        lines.append(f"- Note: {note}")
    lines += ["", "## Per-list", "", "| List | Expected | Added | Skipped | Failed | Status |", "|---|---|---|---|---|---|"]
    for spec in data["lists"]:
        n = spec["name"]
        ls = p["lists"].get(n, {})
        lines.append(
            f"| {n} | {len(spec['entries'])} | {len(ls.get('entries_added', []))} | "
            f"{len(ls.get('entries_skipped_duplicate', []))} | {len(ls.get('entries_failed', []))} | "
            f"{ls.get('status', 'pending')} |"
        )
    if results:
        lines += ["", "## Independent verification", "", "| List | present/expected | active(should be 0) | bad_troops |", "|---|---|---|---|"]
        for r in results:
            if "error" in r:
                lines.append(f"| {r['name']} | {r['error']} | - | - |")
            else:
                lines.append(
                    f"| {r['name']} | {r['present']}/{r['expected']} | {r['active_slots']} | {r['bad_troops']} |"
                )
    confirmed = sum(
        1
        for n in p["lists"]
        for e in p["lists"][n].get("entries_added", [])
        if e.get("disabled_confirmed")
    )
    total_added = sum(len(p["lists"][n].get("entries_added", [])) for n in p["lists"])
    lines += [
        "",
        f"- Entries added: {total_added}; disabled-confirmed: {confirmed}",
        "",
        "## Manual review checklist",
        "- [ ] Open the rally point → farm-list tab on V3; confirm 12 lists exist.",
        "- [ ] Spot-check 3 entries per list: each is INACTIVE (toggle off).",
        "- [ ] Raid lists show 2 Clubswingers/entry; HighRisk lists show no troops.",
        "- [ ] No raids have been dispatched (check movements/rally point).",
    ]
    (HERE / "final-report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
