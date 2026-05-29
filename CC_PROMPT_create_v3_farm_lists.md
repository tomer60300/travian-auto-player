# Claude Code Mission — Create V3 Farm Lists (POC Phase 1)

## Mission

Create 12 farm lists with **648 total entries** on the user's live Travian account, all entries **initially disabled**, sender village V3 (42|17). This is a POC: 2 Clubswingers per entry on the seven raid lists; the five HighRisk lists hold targets only (no troop assignment).

This is **not** a typical multi-agent code task. It is one long serial flow against a live remote game server with strict anti-detection requirements. You operate against the user's real account at `https://ts2.x1.europe.travian.com`. Mistakes are visible to other players and to Travian's anti-cheat ("Multi-hunter") team. Stealth is non-negotiable.

You have full autonomy from start to finish. No user escalation. Make safe, conservative decisions and proceed.

## Pre-flight (user does this once, before running)

The user sets these environment variables and ensures the data files are in the working directory:

```bash
export TRAVIAN_USERNAME="REDACTED@example.com"
export TRAVIAN_PASSWORD="<password from chat>"
export TRAVIAN_SERVER="https://ts2.x1.europe.travian.com"
```

Required files in CWD:
- `v3_farm_lists.json` — authoritative machine-readable target data
- `v3_farm_lists.md` — human-readable reference (you may read for context, but JSON is source of truth)

Working in the repo: `travian-auto-player`, branch `feature/web-ui`.

## Absolute constraints — do not violate

1. **All HTTP to the game server flows through `src/travian_api/clients/http_client.py`.** No raw `requests`/`httpx`/`curl_cffi`/`fetch` calls to Travian endpoints. Use the FarmListService (via Python imports) OR the web UI REST API at `http://127.0.0.1:8001` — both go through the stealth chain.
2. **Never modify any file under `src/travian_api/stealth/`.** Those nine modules are load-bearing anti-detection code.
3. **Use the debug instance on port 8001 only.** Never touch port 8000 (production). If 8001 is not running, start it; never restart 8000.
4. **No parallel API calls to the game server.** One in-flight request at a time. The throttler enforces 1.5–3.0 s gaps; you add more on top.
5. **All entries created in DISABLED state.** If the API does not support creating disabled, create + immediately disable in the same flow before adding the next entry.
6. **Stop immediately on captcha detection.** Do not retry, do not work around. Exit cleanly with a clear message.
7. **No hero in any entry.** Hero is account-wide; this is not for hero use.
8. **No siege units (rams/catapults) in any entry.** Farm-list sends are raids; siege is wasted carry.
9. **Sender village is V3 (42|17) for every list.** Verify before creating each list.

## Phased execution — do all five phases in order

### PHASE 1 — Plan

Investigate the repository to ground your plan in actual code, not assumptions. Read at minimum:

- `src/travian_api/services/farm_list_service.py` — confirm exact method signatures, whether create-disabled is supported, how `force` flag behaves.
- `src/travian_api/web/routes/` — the farm list route module — confirm exact REST shapes.
- `src/travian_api/stealth/throttler.py` and `src/travian_api/stealth/human_delay.py` — read so you understand baseline pacing.
- `src/travian_api/stealth/captcha_guard.py` — confirm how captcha state is exposed so you can poll it.
- `src/travian_api/clients/http_client.py` — confirm the entry path that the FarmListService uses.

Then produce a written plan covering:

1. **Connect path** — login → POST `/api/users/login` → JWT → POST `/api/travian/connect` with credentials → verify with GET `/api/travian/status`. Confirm V3 is in the village list, switch active village to V3.
2. **Pre-checks** — GET `/api/farm/lists` to see if any of the 12 target list names already exist. Plan for the resume case (see "Failure responses" below).
3. **Per-list operation** — for each of the 12 lists in this order: create list (POST `/api/farm/lists` with sender village = V3 ID), then for each entry: add target (POST `/api/farm/lists/{id}/targets`). Determine the exact way to ensure each new entry lands disabled — research first, then encode the exact call sequence.
4. **Pace schedule** — concrete target durations per list and total. See "Pace targets" below for ranges.
5. **Verification path** — how you will confirm at the end that all 648 entries exist and are disabled.
6. **Progress persistence** — exact filename and JSON schema for the resume file.

Write the plan to `./farm-list-creation/plan-v1.md`. Log every section to `./farm-list-creation/logs/orchestrator.log`.

### PHASE 2 — Self-review with stealth emphasis

Re-read your plan-v1 critically. For each step, ask:

- **Could this trip detection?** Is the request shape identical to what the official web UI would emit? Are headers/referer/cookies right? Does the FarmListService handle that, or am I assembling the call myself?
- **Is the pace human-plausible?** A real player does not create 648 farm-list entries in five minutes. The full operation should be spread over **90–180 minutes**. Burst-and-pause is fine; sustained max-rate is not.
- **Have I left a burst risk?** Even with throttler enforcing 1.5–3 s gaps, 660 consecutive requests over 30 minutes is anomalous compared to typical farm-list management. Add longer cooldowns between lists. Add mid-list jitter every 15–25 entries. Add at least one "session break" (5–10 min idle) if total wall-clock exceeds 120 min.
- **What happens if a request fails mid-burst?** Does my retry logic create a faster-than-human re-fire? Add exponential backoff with jitter.
- **What does the captcha guard look like in practice?** Have I built a poll into the loop, or am I only catching it via exception?
- **Have I bypassed the service layer anywhere?** Trace every call I plan to make and confirm it goes through HttpClient.
- **Am I using port 8001?** Double-check.
- **What if a list already exists with the same name from a previous run?** My resume logic must be safe — only fill in missing entries, never duplicate.
- **What if my JWT expires mid-run?** It is 24 h; this fits — but plan for renewal anyway.

Revise the plan addressing every concern. Write `./farm-list-creation/plan-v2.md`. Diff against plan-v1 in `./farm-list-creation/logs/orchestrator.log` so the changes are visible.

### PHASE 3 — Final plan check

One more pass focused on go/no-go:

- Every failure mode has a defined response.
- No step requires user input.
- Progress file is written after every API call.
- Verification is independent of execution (separate read-only pass).
- Total time estimate is between 90 and 180 minutes.

Write the locked plan to `./farm-list-creation/plan-final.md`. If the plan still has open questions, resolve them by reading more code — do not proceed to execution with ambiguity.

### PHASE 4 — Safe execution

Follow `plan-final.md` exactly. The high-level flow:

1. **Ensure web UI is running on 8001.** If not, start it: `python -m uvicorn travian_api.web.app:app --host 127.0.0.1 --port 8001` (background, log to `./farm-list-creation/logs/uvicorn.log`). Wait for healthy response on GET `/api/health` (or equivalent) before proceeding.
2. **Authenticate** (app JWT, then Travian session). Confirm V3 is present.
3. **Pre-check existing lists.** Decide resume vs. fresh-start per the failure-responses rules.
4. **Iterate the 12 lists in JSON order** (Small-Near first, HighRisk-65-70 last). For each list:
   - Create the list with sender_village = V3.
   - Wait inter-create cooldown (60–180 s).
   - For each entry: add target → verify disabled → write progress → wait inter-target gap (4–12 s randomized, on top of throttler).
   - Every 15–25 entries: jitter pause (20–45 s).
5. **One session break** if cumulative wall-clock exceeds 120 min: pause 5–10 min idle.
6. **Final verification**: read all lists back, confirm counts match (19, 45, 59, 15, 47, 37, 43, 54, 90, 89, 91, 59) and every entry is disabled.

Throughout: poll captcha guard before every API call. Save progress.json after every API call.

### PHASE 5 — Report

Produce `./farm-list-creation/final-report.md`:

- Per-list created vs. expected counts
- Total wall-clock time, total API calls
- Any anomalies, errors, retries
- Confirmation that all 648 entries are disabled
- Manual review checklist for the user (e.g., "spot-check 3 entries per list in the UI")

## Pace targets (concrete)

| Gap | Range |
|---|---|
| Inter-target add (within a list) | 4–12 s randomized |
| Mid-list jitter (every 15–25 entries) | 20–45 s |
| Inter-list cooldown | 60–180 s |
| Session break (≥1 if total > 120 min) | 5–10 min |
| Total wall-clock target | 90–180 min |
| Total API calls (creates + adds) | 660 |

These are minimums on top of the throttler's built-in 1.5–3.0 s gaps. Randomize all values; do not use the same constant repeatedly.

## Failure responses (no user escalation)

| Condition | Response |
|---|---|
| Captcha detected | Stop immediately. Log. Exit cleanly. Do not retry, do not work around. |
| HTTP 429 (rate limited) | Wait 10 min. Retry once. If 429 again: stop. |
| HTTP 5xx | Exponential backoff (60s, 180s, 540s), max 3 retries. Then stop. |
| Connection drop | Save progress. Reconnect. Resume from progress.json. Max 3 reconnect attempts. |
| List with target name already exists, same sender village | Treat as resume: skip create, add only missing targets (compare by coords). |
| List with target name already exists, different sender village | Stop. Log. Exit. (Indicates manual prior creation; do not interfere.) |
| Target add returns "duplicate" | Mark as already-present in progress.json. Continue. |
| Target add returns other non-fatal error | Log to errors.log. Continue. |
| JWT expired | Re-login. Resume. |
| Travian session disconnected | Reconnect via `/api/travian/connect`. Resume. |
| Unexpected exception | Log full traceback. Save progress. Attempt graceful disconnect. Stop. |

## Progress persistence

`./farm-list-creation/progress.json`:

```json
{
  "started_at": "ISO-8601",
  "last_updated_at": "ISO-8601",
  "phase": "PHASE_4_EXECUTION",
  "lists": {
    "V3-Small-Near": {
      "status": "completed | in_progress | pending",
      "remote_list_id": 123,
      "entries_added": [
        {"x": 21, "y": 96, "added_at": "ISO-8601", "disabled_confirmed": true}
      ],
      "entries_skipped_duplicate": [],
      "entries_failed": []
    }
  },
  "api_call_count": 0,
  "errors": []
}
```

Write after every API call. On startup, load this file and skip any entry already in `entries_added` or `entries_skipped_duplicate`.

## Logging layout

```
./farm-list-creation/
  plan-v1.md
  plan-v2.md
  plan-final.md
  progress.json
  final-report.md
  logs/
    orchestrator.log    — phase transitions, decisions, plan changes
    api.log             — every API call: timestamp, endpoint, status, duration
    stealth.log         — every pace decision, every delay applied, captcha polls
    errors.log          — anything unexpected, with full context
    uvicorn.log         — web UI server output (if you start it)
```

Every entry timestamped ISO-8601.

## Data file contract

`v3_farm_lists.json` structure (already on disk):

```json
{
  "meta": {
    "sender_village": {"name": "V3", "coords": [23, 88]},
    "total_targets": 648,
    "per_entry_troops": {"Clubswinger": 2},
    "entry_initial_state": "disabled",
    "high_risk_troops": null
  },
  "lists": [
    {
      "name": "V3-Small-Near",
      "sender_village_coords": [23, 88],
      "initial_state": "disabled",
      "troops_per_entry": {"Clubswinger": 2},
      "is_high_risk": false,
      "entries": [
        {"x": 21, "y": 96, "village": "...", "village_pop": 193, "player": "...", "player_pop": 193, "alliance": "KNGFW", "distance": 8.3}
      ]
    }
  ]
}
```

Lists where `is_high_risk: true` get **no troop assignment** — entries created with empty/no troop spec, disabled. The seven raid lists get `Clubswinger: 2` per entry, disabled.

## What "disabled" means and how to verify

In Travian, a farm-list entry has an active/inactive state. New entries default to active. You must ensure every entry ends up inactive before proceeding. Research the exact mechanism during Phase 1:

- Does FarmListService expose a deactivate call?
- Does the create-target endpoint take an initial-state parameter?
- If neither: post-creation, immediately disable each entry as part of the same per-entry flow (one extra API call per entry, factored into pace).

Verify by reading back. Do not trust your own write — read GET `/api/farm/lists/{id}` and confirm the entry shows as inactive.

## Hard "do not" list

- Do not create lists for any village other than V3.
- Do not add any entry not present in `v3_farm_lists.json`.
- Do not activate any entry. Every entry stays disabled.
- Do not change the per-entry troop count from 2 Clubswingers (or empty for HighRisk).
- Do not modify any file under `src/travian_api/stealth/`.
- Do not call `/api/farm/lists/{id}/send` or `/api/farm/send-all` or any WebSocket farm-loop endpoint. This run is creation only — no raids are sent.
- Do not run the auto-scout or any scanning operation during this run.
- Do not write to port 8000 (production instance).
- Do not parallelize game-server requests.

## Success criteria

When you finish, all of the following must be true:

1. Exactly 12 farm lists exist on V3 with the exact names in `v3_farm_lists.json`.
2. Each list has the exact entry count in the JSON (19, 45, 59, 15, 47, 37, 43, 54, 90, 89, 91, 59 = 648 total).
3. Every entry is disabled.
4. Seven raid lists have 2 Clubswingers per entry.
5. Five HighRisk lists have no troop assignment.
6. No raids were sent.
7. No captcha was triggered (if one was, you stopped — that is also acceptable).
8. `final-report.md` exists with the full audit trail.
9. `progress.json` shows `phase: COMPLETED` and zero outstanding entries.

If any criterion fails, write the failure to `final-report.md` with what was attempted and what blocked it. Do not retry blindly.

## Final reminder

This account belongs to a real player on a live x1 server. A detection event harms them. Conservative is correct. If a step feels risky, slow down further or stop and log. The user has accepted that the operation may take up to three hours; speed is not a goal.

Begin Phase 1 now.
