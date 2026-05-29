# Plan v2 — Stealth self-review revision

Supersedes plan-v1. Same architecture (standalone script → TravianSession →
FarmListService, all through HttpClient stealth chain). This revision hardens the
items flagged in v1's "Open questions" and the mission's self-review checklist.

## Changes from v1 (see orchestrator.log for the line-level diff)

### R1 — Request shape is identical to the official frontend ✅ (no change needed)
`create_farm_list` → `POST /api/v1/farm-list`; `add_slot` → `POST /api/v1/farm-list/slot`
with `request_type="xhr"` (adds X-Requested-With + Sec-Fetch-Mode:cors, exactly the
shape the Travian rally→farm-list tab emits); `get_farm_list` → `POST /api/v1/graphql`.
Headers/Referer/cookies/TLS are all assembled by `BrowserHeaders` + curl_cffi inside
HttpClient — I assemble none of it myself. Confirmed every call is a FarmListService
method; **no inline/raw HTTP, no service-layer bypass.**

### R2 — Decouple captcha callback from the web app
v1 reused `TravianSession`, whose captcha callback imports `ws_manager` /
`log_stream_manager` (web-app singletons). To avoid any web-app coupling, immediately
after constructing the session I **replace** the callback:
`session.http_client.captcha_guard.set_trigger_callback(local_cb)` where `local_cb`
writes the captcha event to `stealth.log` + `errors.log`. The guard still blocks
exactly as before; only the notification sink changes. Safe + self-contained.

### R3 — Captcha polling is built into the loop (not exception-only)
Before AND after every game-server call I read `http_client.captcha_guard.is_blocked`.
- Pre-call blocked → STOP immediately (clean disconnect, exit).
- Post-call blocked → the just-returned response may be a captcha page; STOP and do
  NOT count that entry as added/confirmed.
Note: `throttler.wait()` would *block indefinitely* on an active guard; the pre-call
poll short-circuits before that can happen, so we never hang — we stop.

### R4 — `force=False`, with verify read-back as the authoritative truth
`add_slot` discards its HTTP response, so duplicate/confirmation outcomes aren't
visible at add time. Rather than reimplement the call (which would bypass the service
method), the **per-list `get_farm_list` read-back is the source of truth**: it lists
exactly which (x,y) landed and each slot's `is_active`. Resolution:
- Coords present + inactive → `disabled_confirmed=true`.
- Coords present + ACTIVE → anomaly: log to errors.log; this must never happen with
  `active=False`; if it does, STOP (a stealth/state inconsistency).
- Coords missing after add → record in `entries_failed`, log to errors.log, **do not
  retry with force** (conservative — a target needing force is likely already farmed
  elsewhere; surfacing beats silently overriding a Travian confirmation).
`force` stays `False` (matches the frontend's first-attempt behavior).

### R5 — Retry timing never re-fires faster than a human
- HttpClient already retries transient `RequestError`/`Timeout` (tenacity, 3 tries,
  `wait_random_exponential(max=12)` — randomized, no power-of-two bot stripes).
- Orchestrator-level backoffs sit on top, all with long human-scale waits:
  - 5xx (`NetworkError.status >= 500`): sleep 60 → 180 → 540 s (each +0–20 s jitter),
    max 3 attempts, then STOP.
  - 429 (`NetworkError.status == 429`): the client already added a 120 s throttler
    penalty; orchestrator additionally sleeps 600 s, retries once; second 429 → STOP.
  - Connection drop / `SessionExpiredError`: save progress, reconnect
    (`session.connect()`), resume; max 3 reconnects, ≥30 s between attempts.
- No tight retry loop anywhere.

### R6 — Wall-clock governor keeps total in 90–180 min
Track `t0`. Maintain `entries_remaining`. Before each inter-target sleep compute
`projected = elapsed + entries_remaining * avg_planned_gap`. If `projected > 170 min`
→ draw inter-target gaps from the LOW end (4–6 s) and reduce jitter frequency. If the
run is trending fast (`projected < 95 min` near the end) → draw cooldowns from the
HIGH end. Randomization is preserved within the chosen band (no constant reuse).

### R7 — Burst risk
Throttler `burst_max=30`/60 s; planned cadence ≈ 6–8 req/min — comfortably under, so
the built-in burst cooldown should never fire (defense in depth, not relied upon).
Mid-list jitter (every 15–25 entries, random threshold) + 60–180 s inter-list
cooldowns + one 5–10 min session break break up any sustained signature. 660
mutating calls are spread across ~130–150 min, not a 30-min burst.

### R8 — One Travian session only; port 8000/8001 untouched
Nothing is listening on :8000 or :8001 (verified). The script starts no web server
and registers nothing in `session_manager`. It is the only session. It performs
**zero** sends/scouts/WS loops — creation + read-back only.

### R9 — Resume safety
Startup loads `progress.json` AND live `get_all_farm_lists()`. Per target name:
existing+V3-owned → resume (add only coords not already slots); existing+non-V3 →
STOP that list. Entry-level skip is by (x,y) coordinate compare — duplicates are
impossible.

### R10 — JWT (24 h) fits the ≤3 h run; renewal still handled
HttpClient auto-reauths on session-expiry via the auth callback; orchestrator also
catches `SessionExpiredError` and reconnects (R5).

## Unchanged from v1
Connect path (§1), pre-checks (§2), per-list flow (§3), pace ranges (§4),
independent verification (§5), progress schema (§6), failure table (§7),
logging layout (§8), troop mapping (Clubswinger→t1; HighRisk→all-zero).

## Residual risk accepted
- **Missing `TRAVIAN_PASSWORD`** (verified unset in both shells) blocks login. Phase 4
  will detect this before any network call and stop cleanly with resume instructions
  rather than guess/fabricate a credential.
- If a large fraction of targets require `force`, those entries land in
  `entries_failed` and are reported, not force-added. Acceptable for a POC.
