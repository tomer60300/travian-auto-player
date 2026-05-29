# Plan FINAL (locked) — Create V3 Farm Lists (POC Phase 1)

Locked from plan-v2. Read plan-v1.md / plan-v2.md for full rationale; this is the
go/no-go execution spec.

## Go/No-Go checklist
- [x] Every failure mode has a defined response — plan-v2 §7 + R5.
- [x] Progress file written after every game-server call.
- [x] Verification is a separate, read-only pass (`verify_all()`), independent of writes.
- [x] Total time estimate 90–180 min, enforced by the wall-clock governor (R6).
- [x] No step requires user input *during* the run.
- [ ] **PRECONDITION UNMET: `TRAVIAN_PASSWORD` is not set** (verified in both shells).
      Execution cannot authenticate until it is supplied. This is a pre-flight input,
      not an in-run decision. Phase 4 detects it before any network call and stops
      cleanly if absent (no fabrication, no retry).

## Locked parameters
- Server: `https://ts2.x1.europe.travian.com` (env `TRAVIAN_SERVER` if set, else literal).
- User: env `TRAVIAN_USERNAME` (= REDACTED@example.com). Password: env `TRAVIAN_PASSWORD` (REQUIRED).
- Sender village: **V3**, located by coords (23,88) in `auth_state.villages`; name cross-checked == "V3".
- Tribe sanity: tribe_id == 2 (Teutons). Log-only.
- Troops: raid lists → `{t1:2, t2..t10:0}`; HighRisk lists → `units=None` (all-zero). `active=False`. `force=False`.
- Lists in JSON order:
  1. V3-Small-Near (19)   2. V3-Small-Far-30-50 (45)  3. V3-Small-Far-50-70 (59)
  4. V3-Med-Near (15)     5. V3-Med-Far (47)          6. V3-Big (37)
  7. V3-Edge (43)         8. V3-HighRisk-0-30 (54)*   9. V3-HighRisk-30-45 (90)*
  10. V3-HighRisk-45-55 (89)* 11. V3-HighRisk-55-65 (91)* 12. V3-HighRisk-65-70 (59)*
  (* = HighRisk, no troops). Total 648.

## Pace (locked)
inter-target 4–12 s · mid-list jitter 20–45 s every 15–25 entries · inter-create
60–180 s · inter-list 60–180 s · one session break 5–10 min if cumulative > 120 min ·
governor biases gaps to keep total in [90,180] min · all values randomized each draw.

## Per-call protocol (locked)
1. poll `captcha_guard.is_blocked` → if True STOP.
2. make exactly one FarmListService call.
3. poll `captcha_guard.is_blocked` → if True STOP (don't count last entry).
4. append to api.log (ts, endpoint, status, ms) + update progress.json + stealth.log (delays).
5. sleep the planned gap.

## Verification (locked, separate pass)
`verify_all()`: `get_all_farm_lists()` → 12 names, all V3-owned. Per list
`get_farm_list(id)` → slot count == expected; EVERY slot `is_active==False`; raid
slots `troop.t1==2 and troop.total==2`; HighRisk slots `troop.total==0`.

## Execution entrypoint
`farm-list-creation/orchestrate.py`, run via:
`uv run python farm-list-creation/orchestrate.py`
with `TRAVIAN_PASSWORD` (and `TRAVIAN_SERVER`) present in the launching shell's env.
The script: connects → switches to V3 → pre-checks/resume → creates+populates 12
lists (disabled) → per-list verify → final independent verify → writes final-report.md.
Idempotent/resumable from progress.json.
