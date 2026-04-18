# Farm Builder — Investigation Findings

**Date:** 2026-04-18
**Branch:** feature/web-ui
**Source of domain knowledge:** `.one-shot/insights.md` (one-shot CLI run from 2026-04-17)

## 1. Critical domain facts (from the CLI one-shot)

- **DEF value = `defender_combat_strength`**, NOT `defender_total` (sum of troops). Min observed = 10, max = 500+. The 10-219 troop-composition table operates on this field. A def=0 result indicates a parser failure, not an undefended village.
- **Teuton TK = t6**, not t4. t4 is Scout. The one-shot prompt had this wrong; production mapping must use t6.
- **Player-total population** requires `auto_scout_service.get_player_population(pid)` (HTTP call per unique player). GraphQL `player.population` returns 0 for other players.
- **Chebyshev distance is used for "within radius"**; no such function exists in the codebase — implement inline as `max(abs(dx), abs(dy))`. `scan_map()` filters by Euclidean, so to capture all Chebyshev-R tiles you must scan with Euclidean radius `ceil(sqrt(2)*R)` (≈ 43 for R=30), then post-filter.
- **Farm list slot limit = 100**. Past 100, API returns `raidList.errorRaidListSlotLimit`. Overflow lists created with `-2`, `-3` suffix is required.
- **`add_slot` rejects all-zero troops** with `raidList.error_no_troops`. Placeholder-inactive slots need at least 1 troop in the dict.
- **Scout travel times for R=30 are 1-4 hours (Teuton scout speed). Sequential sends with stealth pacing ≈ 10-20s each.** A full-scan scout wave is hours.
- **Enrichment cost:** ~1 tile/6-8s under stealth throttle (burst-limit triggers). For ~700 tiles, budget 70-90 min.

## 2. Backend wiring (confirmed)

### Route registration — `src/travian_api/web/app.py`
- REST routers imported at lines 21-33, registered at lines 153-165.
- WS routers imported at lines 36-41, registered at lines 167-173.
- Add `farm_builder_router` after `scout_router`, `farm_builder_ws_router` after `oasis_raider_ws_router`.

### REST conventions
- Prefix per module (e.g., `/api/scout`, `/api/farm`). For this feature: `/api/farm-builder`.
- Session injection: `session: TravianSession = Depends(get_travian_session)`.
- Pydantic models for request/response.
- Errors: `HTTPException(status_code=HTTP_xxx, detail=...)`.
- Streaming: `StreamingResponse` with NDJSON per farm.py `/defense-scan`.

### WS template: `farm_ws.py` (`/ws/farm/run/{list_id}`)
Closest match to what I need: long-running job, stop support, ExecutionSession tracking. I'll mirror its protocol exactly.

### ExecutionSession — `web/execution_sessions.py`
- Ring buffer: 2000 messages per session.
- Session TTL: 24 hours.
- Subscriber queue: 500 per subscriber.
- **IN-MEMORY ONLY** — does not survive server restart. This is Level A+C durability, not Level B. Confirmed per prompt's scope.
- List endpoint: `GET /api/sessions`.
- Stream endpoint: `WS /ws/sessions/{session_id}/stream?token=<JWT>`.
- Client subscribes → receives `session_meta`, then `history` (backfill), then live `message` events.

### AutoScoutService — filter function already exists
**`filter_targets()`** at `auto_scout_service.py:166`:
```python
def filter_targets(
    self, tiles, *,
    max_population=None, min_population=None,
    exclude_coords=None, exclude_player_ids=None, exclude_alliance_ids=None,
    only_no_player=False, exclude_oases=True, max_distance=None,
) -> List[MapTileInfo]
```

**BUT** — this filter does NOT include:
- Player-total-pop (the critical filter the one-shot proved matters; requires separate `fetch_player_populations` call first).
- Alliance exclusion by **tag string** (exists only by `alliance_id` set). Alliance tags are strings from tile-details enrichment.

**Filter-parity risk:** The prompt warns about the "known auto-scout filter-parity bug" — scout_ws.py reimplements some filter logic inline (line 979) rather than calling `filter_targets`. I will extract a shared canonical filter function that takes the full filter spec (including player-total-pop and alliance-tag strings) and use it from both paths. This is Phase 2.

### FarmListService — signatures confirmed stable
- `create_farm_list(village_id, name, default_units=None) -> int`
- `add_slot(list_id, x, y, units=None, active=True, force=False) -> None`
- `get_all_farm_lists() -> List[FarmList]`
- `get_farm_list(list_id) -> FarmList`
- `delete_slots(slot_ids) -> None`

### SQLAlchemy — `web/models/db.py`
- Uses `Base` + `Mapped[...]` async declarative style.
- No Alembic; tables auto-created via `Base.metadata.create_all()` in `init_db()` on startup.
- `User.id` FK convention: `ForeignKey("users.id")`.
- New models go in a new file `web/models/farm_builder.py` and must be imported by `db.py` (or registered on `Base`) so `create_all()` picks them up.

### No disk-based job persistence exists
Confirmed via search: `json.dump`/`checkpoint`/`phase_state`/`pickle` appear only for cookies, JWT cache, and streaming output — never for job state. Adding one here is scope creep (Level B rejected).

## 3. Frontend wiring (confirmed)

### Routing — `frontend/src/App.jsx`
- Lazy-loaded pages via `lazy(() => import('./pages/X'))`.
- Protected routes wrapped in `<GuardedPage><X /></GuardedPage>`.
- New page: `const FarmBuilder = lazy(() => import('./pages/FarmBuilder'))` + route `<Route path="/farm-builder" ...>`.

### Sidebar — `frontend/src/components/Layout.jsx`
- `navItems` array at lines 11-23. Insert `{ to: '/farm-builder', label: 'Farm Builder', icon: '🔨' }` between `/farm` and `/scout`.

### WebSocket factory — `frontend/src/ws.js`
- `createWebSocket(path, onMessage, onError, onClose, { reconnect, maxRetries, onReconnecting, onReconnected })`
- With `reconnect: true`, returns `{ ws, close() }`.
- Token injected via `localStorage` `token` key into query string.

### Zustand stores
- `gameStore`: `villages`, `activeVillageId`, `tribeId`.
- `logStore`: activity log entries (used for cross-page log drawer).
- `authStore`: token/user.
- **No session/operation store** — pages manage their own WS + local message state. Live-run viewer lives in `Sessions.jsx` and uses `/api/sessions` polling + `/ws/sessions/{id}/stream`.

### Reusable components I will use
- `<WebSocketPanel>` — live log pane (messages, status, onClear).
- `<OperationCard>` — running-op card with progress bar + elapsed timer.
- `<ConfirmDialog>` — gate 2 confirmation.
- `<VillageSelector>` — home village picker (single); for multi-select home villages I'll build a small inline `<SelectMultiple>`.
- `useToast()` — notifications.
- CSS classes: `.card`, `.heading-gold`, `.btn-primary/secondary/danger`, `.input-field`, `.status-dot*`, `.progress-track`/`.progress-fill`, `.data-table`.

### Sessions flow template — `frontend/src/pages/Sessions.jsx`
- On mount: `GET /api/sessions` every 10s.
- On click session: `createWebSocket('/ws/sessions/{id}/stream', ...)`.
- Handles `session_meta`, `history` (backfill), `message` (live), `session_ended`.
- **This is exactly the reconnect mechanism LiveRunView needs.** I will mirror it.

## 4. Filter-parity plan (Phase 2)

I will extract a **single canonical filter function** into `auto_scout_service.py`:

```python
def filter_canonical(
    tiles: List[MapTileInfo],
    *,
    exclude_oases: bool = True,
    exclude_abandoned: bool = True,
    exclude_natars: bool = True,
    exclude_own_village_ids: Optional[Set[int]] = None,
    exclude_player_ids: Optional[Set[int]] = None,
    exclude_alliance_tags_lower: Optional[Set[str]] = None,  # new: string tags
    player_total_pops: Optional[Dict[int, int]] = None,       # new: pre-fetched
    max_player_total_pop: Optional[int] = None,               # new
    max_target_village_pop: Optional[int] = None,
    min_target_village_pop: Optional[int] = None,
    within_chebyshev_of: Optional[List[Tuple[int, int, int]]] = None,  # [(x,y,r)]
) -> Tuple[List[MapTileInfo], Dict[str, int]]:
    """Return (survivors, drop_counts_by_reason)."""
```

Both `scout_ws.py`'s inline filter and the new farm builder will call this. To avoid regression in existing scout scan, I will:
1. Add `filter_canonical` as a new function alongside `filter_targets`.
2. NOT rewrite `scout_ws.py`'s current scan call (keeps behavior identical).
3. Only the new farm builder uses `filter_canonical`.
4. Leave a TODO in `scout_ws.py` noting the dup, but do NOT refactor it in this feature branch.

This avoids the trap of "fixing" scout during a feature build.

**Rationale:** The prompt says "extract one now" but also "do not invent a new filter path". The cleanest reading: add the shared function, call it from the new path, note the duplicate. Actually touching scout is Phase 8's job, not Phase 2's.

## 5. Scope confirmations vs. prompt constraints

| Rule | Status |
|------|--------|
| No modify `stealth/` | ✅ will not touch |
| No new abstractions / base classes | ✅ using 2 functions in 1 file for matrix+advanced |
| No disk-based job persistence | ✅ no `json.dump` in background task |
| No parallel scouting | ✅ sequential via existing service |
| No silent except | ✅ all failures logged + recorded, 3 retries then fail loud |
| Hardcoded Teuton table only in `farm_builder_service.TROOP_COMPOSITION_TABLES` | ✅ single source of truth |
| "~N hours ETA" not softened | ✅ literal copy per prompt |
| New files only (no adjacent service "improvements") | ✅ filter extraction is a new function, not a rewrite |

## 6. Blockers

**None.** All required infra exists. Investigation surfaced no plan-invalidating issues.

One minor note: the `ExecutionSession` session_type string is a free-form label used by Sessions.jsx to show readable names. I will use `farm-builder` and also add a mapping in Sessions.jsx UI so the card label reads "Farm Builder".

## 7. Phased execution plan

1. ✅ Investigation + findings (this doc).
2. Extract shared filter into `auto_scout_service.filter_canonical` (additive; no call-site changes).
3. Backend: models + service + REST routes + WS channel.
4. Register routes/models in `app.py` and `db.py`.
5. Frontend: `FarmBuilder.jsx` page + subcomponents (ScanConfig, MatrixSpec, AdvancedSpec, ScanPreviewTable, DefenseScanPlan, LiveRunView).
6. Frontend: route + nav link.
7. Evidence doc + regression check (scout, farm, sessions pages still work).

Each phase gets a git commit. I will surface any blocker mid-build if I discover a wrong assumption.
