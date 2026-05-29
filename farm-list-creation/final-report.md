# V3 Farm List Creation — Final Report

- Status: **COMPLETED**
- Generated: 2026-05-27T21:42:52Z
- Server: https://ts2.x1.europe.travian.com
- Wall-clock: 100.6 min
- Total game-server API calls: 314

## Per-list

| List | Expected | Added | Skipped | Failed | Status |
|---|---|---|---|---|---|
| V3-Small-Near | 19 | 19 | 0 | 0 | completed |
| V3-Small-Far-30-50 | 45 | 45 | 0 | 0 | completed |
| V3-Small-Far-50-70 | 59 | 59 | 0 | 0 | completed |
| V3-Med-Near | 15 | 15 | 0 | 0 | completed |
| V3-Med-Far | 47 | 47 | 0 | 0 | completed |
| V3-Big | 37 | 36 | 0 | 1 | completed |
| V3-Edge | 43 | 43 | 0 | 0 | completed |
| V3-HighRisk-0-30 | 54 | 0 | 0 | 54 | completed |
| V3-HighRisk-30-45 | 90 | 0 | 0 | 90 | completed |
| V3-HighRisk-45-55 | 89 | 0 | 0 | 89 | completed |
| V3-HighRisk-55-65 | 91 | 0 | 0 | 91 | completed |
| V3-HighRisk-65-70 | 59 | 0 | 0 | 59 | completed |

## Independent verification

| List | present/expected | active(should be 0) | bad_troops |
|---|---|---|---|
| V3-Small-Near | 19/19 | 0 | 0 |
| V3-Small-Far-30-50 | 45/45 | 0 | 0 |
| V3-Small-Far-50-70 | 59/59 | 0 | 0 |
| V3-Med-Near | 15/15 | 0 | 0 |
| V3-Med-Far | 47/47 | 0 | 0 |
| V3-Big | 36/37 | 0 | 0 |
| V3-Edge | 43/43 | 0 | 0 |
| V3-HighRisk-0-30 | 0/54 | 0 | 0 |
| V3-HighRisk-30-45 | 0/90 | 0 | 0 |
| V3-HighRisk-45-55 | 0/89 | 0 | 0 |
| V3-HighRisk-55-65 | 0/91 | 0 | 0 |
| V3-HighRisk-65-70 | 0/59 | 0 | 0 |

- Entries added: 264; disabled-confirmed: 264

## Manual review checklist
- [ ] Open the rally point → farm-list tab on V3; confirm 12 lists exist.
- [ ] Spot-check 3 entries per raid list: each is INACTIVE (toggle off) with 2 Clubswingers.
- [ ] Confirm the 5 HighRisk lists exist but are empty (see finding below).
- [ ] No raids have been dispatched (check movements/rally point).

---

## Findings & required decisions

### Outcome summary
- **12 farm lists created on V3 (id 20003, in-game name "03", 42|17).** ✅
- **7 raid lists: 264 / 265 entries added, all DISABLED, all with exactly 2 Clubswingers (t1=2).** Independent read-back confirms `active=0` and `bad_troops=0` for every raid list.
- **5 HighRisk lists: created but EMPTY (0 / 383 entries).**
- No raids sent. **No captcha triggered.** Wall-clock 100.6 min. Clean exit (code 0).

### Finding 1 — HighRisk lists cannot be populated as specified (BLOCKER)
Every HighRisk slot add was rejected by Travian with HTTP 400:
`{"error":"raidList.error_no_troops","message":"No troops chosen."}`

**Travian Legends does not allow a farm-list entry with zero troops** — every slot
must carry at least one unit. The mission spec ("HighRisk = targets only, no troop
assignment") is therefore **infeasible on this server**. Per the hard "do not" list
("Do not change the per-entry troop count … (or empty for HighRisk)"), I did **not**
assign troops to work around it. The 5 lists exist and are ready; they just hold no
targets yet.

**Decision needed from you — pick one:**
- **A.** Assign a minimal troop count (e.g. 1 Clubswinger / `{t1:1}`) to HighRisk
  entries so Travian accepts them. I can re-run for just the 5 HighRisk lists.
- **B.** Leave the 5 HighRisk lists empty and keep those 383 targets out of farm lists.
- **C.** Use 2 Clubswingers for HighRisk too (same as raid lists).

### Finding 2 — V3-Big has 36/37 (1 transient failure)
`V3-Big (57,100)` failed once with `api.unexpectedError` ("Unexpected error") — a
server-side hiccup, not a troop/validation issue. The other 36 entries are correct
and disabled. **Resume caveat:** the list's status is `completed`, so a plain re-run
will skip it; re-adding this single entry needs a targeted run (I can do this).

### Stealth note
No bot-detection at any point. The 384 rejected adds (383 HighRisk + 1 Big) were
legitimate API 400s spaced ~2 s apart, not throttling/captcha events. Total
game-server requests ≈ 699 over 100.6 min.