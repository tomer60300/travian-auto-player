# Farm Builder — Evidence Report

**Date:** 2026-04-18
**Branch:** feature/web-ui
**Status:** Implemented. Manual browser verification is the user's call.

## What shipped

### Backend (7 files touched/created)
| File | Lines | Change |
|------|------|--------|
| `src/travian_api/services/auto_scout_service.py` | +98 | Added `filter_canonical` static method (additive; existing `filter_targets` untouched to avoid regressing scout page) |
| `src/travian_api/services/farm_builder_service.py` | new (~630) | Orchestrator: matrix/advanced classifier, DEF→troop lookup table (single source of truth), preview (phases 1-2), run_full (phases 3-6 with overflow-list handling, inactive placeholders, existing-report reuse) |
| `src/travian_api/web/routes/farm_builder.py` | new (~200) | REST: presets CRUD, history, scan-cache (view-layer, 1h TTL) |
| `src/travian_api/web/ws/farm_builder.py` | new (~210) | WS `/ws/farm-builder/run` — mirrors `oasis_raider.py` / `farm_ws.py` protocol; supports `{action: preview\|run\|stop}`; records run history |
| `src/travian_api/web/models/farm_builder.py` | new (~65) | `FarmBuilderPreset`, `FarmBuilderRunHistory`, `FarmBuilderScanCache` SQLAlchemy models |
| `src/travian_api/web/app.py` | +5 | Router + WS + model-module imports |
| (auto) `~/.travian/travian_web.db` | +3 tables | Auto-created on server startup via `init_db()` |

### Frontend (9 files touched/created)
| File | Change |
|------|--------|
| `frontend/src/pages/FarmBuilder.jsx` | new main page |
| `frontend/src/components/FarmBuilder/ScanConfig.jsx` | new |
| `frontend/src/components/FarmBuilder/MatrixSpec.jsx` | new (with live preview) |
| `frontend/src/components/FarmBuilder/AdvancedSpec.jsx` | new (rule builder, first-match) |
| `frontend/src/components/FarmBuilder/ScanPreviewTable.jsx` | new (sortable + searchable) |
| `frontend/src/components/FarmBuilder/DefenseScanPlan.jsx` | new (literal ETA disclosure) |
| `frontend/src/components/FarmBuilder/LiveRunView.jsx` | new (reconnect via `/ws/sessions/{id}/stream`, 5 phase bars, log pane, completed/skipped tabs) |
| `frontend/src/App.jsx` | +2 (lazy import + route) |
| `frontend/src/components/Layout.jsx` | +1 (nav entry between `/farm` and `/scout`) |

## Mechanical verification (what I could check without a browser)

| Check | Result | Evidence |
|-------|--------|----------|
| Backend imports clean | ✅ | `python -c "from travian_api.web.app import app"` → no errors |
| `lookup_troop_row(10, 'teutons')` | ✅ returns `{t1: 2}` | Python repl |
| `lookup_troop_row(78, 'teutons')` | ✅ returns `{t6: 1}` (1 TK) | Python repl |
| `lookup_troop_row(220, 'teutons')` | ✅ returns `None` (SKIP) | Python repl |
| `lookup_troop_row(N, 'romans')` | ✅ returns `None` for all (empty table) | Single-file change to add tribes later |
| REST routes registered | ✅ 4 new paths in `/openapi.json` | `GET /api/farm-builder/history`, `presets`, `presets/{id}`, `scan-cache` |
| SPA route `/farm-builder` | ✅ HTTP 200 | `curl -o /dev/null -w "%{http_code}"` |
| DB tables auto-created | ✅ `farm_builder_presets`, `farm_builder_run_history`, `farm_builder_scan_cache` | `sqlite3` listing |
| Frontend build succeeds | ✅ `FarmBuilder-vzyw1LKe.js 33.80 kB` | `npm run build` |

## Acceptance checklist (from prompt) — what I built vs. what requires manual browser check

| # | Item | Status | Notes |
|---|------|--------|-------|
| 1 | `/farm-builder` page loads with home villages from gameStore | ⚠️ code in place; requires browser open to confirm | `ScanConfig` reads `villages` from gameStore; if empty, shows a "no villages → /connect" fallback |
| 2 | Small run → Gate 1 preview shows numbers + `FB-SCAN` / `FB-FILTER` logs | ⚠️ code in place; requires full run | Log emoji prefixes `🔍 FB-SCAN`, `👥 FB-FILTER`, etc. per prompt |
| 3 | Gate 2 ETA + disclosure + cancel | ✅ | `DefenseScanPlan.jsx` renders ETA heuristic (survivor×15s PRE + survivor×60s scout × 1.3 pad); disclosure text is **literal per prompt** |
| 4 | Close browser → reopen → lands in LiveRunView with replayed buffer | ⚠️ code in place; requires browser test | `FarmBuilder.jsx` on-mount calls `GET /api/sessions`, finds active `farm-builder` session, redirects to `LiveRunView` which subscribes via `/ws/sessions/{id}/stream` (same mechanism used by `Sessions.jsx`) |
| 5 | Second-device viewing during active run | ⚠️ same reconnect mechanism as #4 | Provided by existing ExecutionSession multi-subscribe infra |
| 6 | Final report counts match `/farm` page | ⚠️ requires full successful run | `run_full()` returns report with `added/skipped/failed` mirroring farm list slots |
| 7 | Stop mid-run produces partial report, no orphan requests | ⚠️ requires run + stop test | `check_stop` polled after each target; current HTTP request finishes before next step |

## Filter-parity (explicit non-regression)

Per findings doc Section 4: I added `AutoScoutService.filter_canonical` as an additive **static method**. Existing `filter_targets` and the inline filter in `scout_ws.py` are **unchanged**. This avoids breaking the scout scan page while giving the farm builder a complete filter (with player-total-pop + alliance-tag-strings support) that the old `filter_targets` lacked.

**Architecture note:** `run_full()` takes an already-filtered `survivors` list from the client (from `preview_result`). It does NOT accept filter parameters. This structurally prevents the live-execution path from re-filtering with different criteria.

## Explicit NO-GOs observed

- ✅ No modifications under `src/travian_api/stealth/`
- ✅ No disk-based job checkpoint system
- ✅ No defense-scout parallelism (sequential through `military_service.send_scouts`)
- ✅ No new abstractions / base classes / DSLs
- ✅ No silent `except`; all failures log + record in `phase6_fail` / `defense_failed`
- ✅ Teuton table lives **only** in `TROOP_COMPOSITION_TABLES` (farm_builder_service.py)
- ✅ Gate 2 ETA copy is literal from the prompt, no encouragement added
- ✅ Not declaring done. User ships.

## Known limitations / honest gaps

1. **LiveRunView requires a real run to validate fully.** The code mirrors `Sessions.jsx`'s subscription flow, which we know works from the existing sessions page. I have not, however, driven a full end-to-end run with browser-side interaction.

2. **Inactive placeholder troops.** Per the one-shot run's lesson: Travian rejects `add_slot` with all-zero troops (`raidList.error_no_troops`). `run_full()` adds `{t1: 2}` as minimum non-zero for placeholder-inactive slots so the list is valid; the user re-runs later to promote to active with correct troops after fresh defense data arrives.

3. **ETA heuristic is rough.** `survivor×15s + survivor×60s × 1.3`. The real bottleneck is max scout travel time (can be hours for R=30 Teuton scouts). The disclosure panel is explicit: "If the server restarts during this phase, the run is lost. ETA is ~N hours."

4. **Preset save/load UI is not wired into FarmBuilder.jsx.** The REST endpoints exist (`/presets` GET/POST/DELETE) and the scan-cache is auto-written on preview, but the page itself has no preset picker UI. The scan-cache does restore on refresh via `GET /api/farm-builder/scan-cache`. Preset chips + save/load control is a simple follow-up.

5. **`scout_ws.py` still has its inline filter.** I intentionally did NOT refactor it to call `filter_canonical` — see findings.md §4. Listed as a separate future cleanup.

## Regression check (what I confirmed didn't break)

| Page | Status |
|------|--------|
| `GET /` (SPA index) | 200 |
| `GET /farm-builder` (SPA route) | 200 |
| `GET /openapi.json` — 48 total paths | unchanged paths remain |
| SQLite pre-existing tables | `users`, `travian_credentials` unchanged |

## What remains before you ship

The user owns the ship decision. Before shipping I recommend:

1. Open `/farm-builder` in a browser. Confirm form renders, matrix preview updates live as you type.
2. Small smoke test: set radius=5, one home village, click "Scan & preview". Watch `activity log` for `FB-SCAN`/`FB-FILTER` entries.
3. If preview looks right, proceed through gate 2 with a tiny expected target count (<5) to validate the full pipeline end-to-end.
4. Refresh the browser mid-run and confirm `LiveRunView` reconnects and replays.
5. Stop mid-run and confirm partial report is emitted.
6. Check `/farm` page shows the newly-created lists with correct slot counts.

If any of those fail, the fix is small (the scaffolding is in place) — surface the failure and I'll patch.
