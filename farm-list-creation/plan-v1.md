# Plan v1 — Create V3 Farm Lists (POC Phase 1)

**Goal:** Create 12 farm lists on V3 (42|17), 648 entries total, ALL disabled.
7 raid lists = 2 Clubswingers/entry; 5 HighRisk lists = no troops.
No raids sent. No captcha tripped (stop if one fires). 90–180 min wall-clock.

---

## 0. Architecture decision (grounded in code)

**Execution = a standalone async Python script** that imports the app's own
`TravianSession` + `FarmListService` and drives them directly. Reasons:

1. **Disabled state is impossible via REST.** `web/routes/farm.py::add_target`
   calls `farm_service.add_slot(...)` WITHOUT passing `active`, so it defaults to
   `active=True`. There is **no** deactivate/toggle endpoint anywhere in
   `routes/`. The only code path that can create an inactive slot is
   `FarmListService.add_slot(list_id, x, y, units, active=False)`.
2. **Constraint #1 explicitly allows** "the FarmListService (via Python imports)"
   as a first-class alternative to the 8001 REST API — "both go through the
   stealth chain."
3. Every service call (`create_farm_list`, `add_slot`, `get_farm_list`,
   `get_all_farm_lists`) routes through `HttpClient.post_json` → throttler →
   browser headers → curl_cffi TLS → `_check_suspicious_response` (captcha guard).
   So the stealth chain is fully honored. **No file under `stealth/` is touched.**
4. Nothing is listening on :8000 or :8001 right now → no production session to
   disturb, and only ONE Travian session will exist (the script's), which is the
   minimum possible footprint.

The script will NOT be registered in `session_manager` and will NOT start any
web server, WebSocket loop, scout, or send. It is creation-only.

> Note on the mission's "use 8001 REST" framing: that path was written assuming
> REST could set disabled, which it cannot. The Python-imports path is the
> sanctioned equivalent (constraint #1) and is the only one that meets the
> hard disabled requirement without modifying shared app code.

---

## 1. Connect path

```
TravianSession(user_id=99001, server_url=SERVER, username=USER, password=PWD)
  .connect()                      # = auth_service.login(): POST /api/v1/auth/login
                                  #   -> redirect GET -> JWT cookie -> GraphQL ownPlayer
```

- `SERVER` = `https://ts2.x1.europe.travian.com` (mission + JSON meta), read from
  `TRAVIAN_SERVER` env if present, else this literal.
- `USER` = `TRAVIAN_USERNAME` env (read from the environment, never hardcoded).
- `PWD`  = `TRAVIAN_PASSWORD` env. **Required.** If missing → stop cleanly (see §7).
- After connect, `session.auth_state.villages` is populated. **Find V3** by
  matching coords (23,88) — and cross-check name == "V3". Call
  `session.switch_village(v3_id)`. Abort if V3 absent or coords mismatch.
- Verify identity: `tribe_id == 2` (Teutons) — sanity log only, not fatal.
- The script uses a dedicated per-user data dir (temp/.../99001) so its cookie
  jar / JWT cache / persona are isolated and persisted (enables resume).

## 2. Pre-checks

- `get_all_farm_lists()` once. Build `{name: FarmList}`.
- For each of the 12 target names:
  - **Not present** → create fresh.
  - **Present, owner_village.id == v3_id** → RESUME: skip create, reuse its id,
    add only entries whose (x,y) are not already slots (compare by coords).
  - **Present, owner_village.id != v3_id** → STOP for that list, log, exit
    (per failure table: indicates manual prior creation; do not interfere).
- Load `progress.json` if it exists; skip any entry already in `entries_added`
  or `entries_skipped_duplicate`.

## 3. Per-list operation (JSON order: Small-Near first … HighRisk-65-70 last)

For each list:
1. If not resuming: `list_id = create_farm_list(v3_id, name)`  (1 request)
2. Inter-create cooldown: sleep 60–180 s (randomized).
3. For each entry (x,y) not yet done:
   - **Captcha poll (pre):** if `http_client.captcha_guard.is_blocked` → STOP.
   - `units` = `{t1:2, t2..t10:0}` for raid lists; `None` (all-zero) for HighRisk.
   - `add_slot(list_id, x, y, units=units, active=False, force=False)`  (1 request)
   - **Captcha poll (post):** if blocked → STOP (do not count this entry).
   - Append entry to progress (`disabled_confirmed=false` for now). Write progress.json.
   - Inter-target gap: sleep 4–12 s (randomized, on top of throttler ~1–2.5 s).
   - Every 15–25 entries (random threshold): jitter pause 20–45 s.
4. **Per-list verify (1 read):** `get_farm_list(list_id)`. Confirm slot count and
   that every slot `is_active == False`. Flip each matched entry's
   `disabled_confirmed=true` by coord. Log mismatches to errors.log.
5. Inter-list cooldown: sleep 60–180 s (randomized).
6. If cumulative wall-clock > 120 min and the one-time session break not yet
   taken → idle 5–10 min (the session break).

**Troop mapping:** Clubswinger = Teuton t1. Raid units dict =
`{"t1":2,"t2":0,...,"t10":0}`. HighRisk = `units=None` → add_slot fills all-zero.

## 4. Pace schedule (concrete)

| Gap | Range | Notes |
|---|---|---|
| Inter-target add | 4–12 s | on top of throttler 1.0–2.5 s |
| Mid-list jitter | 20–45 s | every 15–25 entries (random threshold) |
| Inter-create cooldown | 60–180 s | after creating a list, before first entry |
| Inter-list cooldown | 60–180 s | between lists |
| Session break | 5–10 min | exactly one, only if cumulative > 120 min |

**Estimate:** 648 adds × ~9 s avg ≈ 97 min; ~32 jitters × ~32 s ≈ 17 min;
12 creates + 12 inter-list + 12 inter-create cooldowns ≈ ~28 min; 1 break ~7 min.
**≈ 130–150 min**, inside the 90–180 window. A soft governor (§ self-review)
will bias delays low if projected total exceeds ~170 min.

**Total game-server requests:** 12 creates + 648 adds = 660 (matches budget) +
~12 per-list verify reads + 1 pre-check read + final verify reads ≈ ~685 total.
Reads are deliberately per-LIST, not per-entry, to avoid doubling the request
surface (per-entry read-back would be 1296 calls — anomalous).

## 5. Verification path (independent, read-only, after execution)

Separate function `verify_all()`:
- `get_all_farm_lists()` → confirm exactly the 12 names exist, each owned by V3.
- For each list: `get_farm_list(id)` → assert slot count == expected and EVERY
  slot `is_active == False`. For raid lists assert each slot `troop.t1 == 2` and
  `troop.total == 2`; for HighRisk assert `troop.total == 0`.
- Emit a per-list table (created vs expected) into final-report.md.

## 6. Progress persistence (`./farm-list-creation/progress.json`)

Schema per mission spec. Written after EVERY game-server call (create, add,
verify). Keyed by list name. Each entry: `{x,y,added_at,disabled_confirmed}`.
Top-level: `started_at, last_updated_at, phase, lists{}, api_call_count, errors[]`.
On startup, load and skip entries already in `entries_added` /
`entries_skipped_duplicate`.

## 7. Failure responses (no user escalation)

Per mission table. Highlights:
- **Missing TRAVIAN_PASSWORD** → cannot authenticate → STOP before any login;
  write final-report.md explaining the blocker + exact resume steps. (Not in the
  mission's table, but the conservative analogue: stop cleanly, log, no retry.)
- **Captcha** (`captcha_guard.is_blocked` True) → STOP immediately, log, graceful
  disconnect, exit. No retry, no workaround.
- **429** → wait 10 min, retry once; second 429 → stop.
- **5xx** (NetworkError status>=500) → backoff 60/180/540 s, max 3, then stop.
- **Connection drop** → save progress, reconnect (session.connect / login(force)),
  resume; max 3 reconnect attempts.
- **add returns duplicate / "already in list"** → mark entries_skipped_duplicate,
  continue.
- **add other non-fatal error** → errors.log, continue.
- **JWT expired** → HttpClient auto-reauth via callback; orchestrator also catches
  SessionExpiredError and reconnects.
- **Unexpected exception** → full traceback to errors.log, save progress, graceful
  disconnect, stop.

## 8. Logging layout

`orchestrator.log` (phases/decisions), `api.log` (ts, endpoint, status, duration),
`stealth.log` (every pace decision + delay + captcha poll), `errors.log`,
`progress.json`, `final-report.md`. All timestamps ISO-8601 UTC.

## Open questions for v2 self-review
- `force=False` correctness if targets already farmed elsewhere (overlap risk).
- Exact retry timing so failures never re-fire faster than human.
- Whether the captcha callback (ws_manager import) is safe outside the web app.
- Governor to keep total within 90–180 min under worst-case randomization.
