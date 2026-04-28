# Changelog

## [Unreleased] — 2026-04-28

### Added — Resumable Cross-Device Operations

Long-running ops (oasis raid, farm-list run, auto-scout, build queue, farm
builder) now survive Safari background, page reloads, bfcache restores, and
cross-device control — a stop pressed from a laptop terminates an op that
was started from an iPhone, and vice versa.

- **`OperationManager` + `ExecutionSession` registry** — every long op runs
  inside `OperationContext` whose messages are buffered in a 24h ring buffer
  and fanned out to any number of subscribers via per-subscriber asyncio
  queues. Sessions persist after the starter WS drops; subscribers reattach
  via `WS /ws/sessions/{id}/stream`.
- **`useResumableOperation` React hook** — single hook each page mounts
  with `(opType, {onMessage, onStatusChange})`. Internally manages the
  starter WS → session-stream WS handoff, persists `session_id` in
  `localStorage`, deduplicates history-replay frames by server-side `ts`,
  and exposes a `stop()` that works through whichever socket is open.
- **Cross-device stop** — `WS /ws/sessions/{id}/stream` accepts
  `{action:"stop"}` from any subscribed client; the `_listen_for_stop`
  task forwards it to `operation_manager.request_stop(session_id)` so
  `ctx.should_stop()` flips for the running coro.
- **Stop-while-reconnecting** — `pendingStopRef` queue + drain on
  `session_init` / `session_meta(running)`, plus active reattach via
  `subscribeToExisting()` when the user taps Stop with no live socket.
- **Auto-reconnect-on-terminal fix** — `closeWs()` now fires from every
  terminal frame (`session_ended`, `error`, `operation_complete`) so the
  captured WS path doesn't keep reopening after the server closes the
  session-stream. (This was producing a 1-second connect/close loop that
  silently swallowed Stop presses.)
- **Race hardening** — `mountedRef` so unmount-cleanup doesn't reattach;
  `inConstruction` flag for sync-onClose-during-construction races;
  handle-specific `closeIfStillCurrent()` so a terminal frame that
  triggers a sync reattach doesn't close the new socket.

See `docs/22-resumable-operations.md` for the full protocol contract and
client behavior.

### Added — Stealth Hardening Pass

The stealth stack got a layer-by-layer review focused on bot-tells that
wouldn't show up in fixed-cost benchmarks (timing patterns, identical
permutations, header-shape mismatches, request-ordering desync). Most
fixes are zero-cost or sub-second; the few that aren't are gated on
glaring tells.

- **TLS fail-closed** — stealth + missing `curl_cffi` now raises instead
  of degrading to `httpx` with Chrome headers (mismatch was a stronger
  tell than running stealth-off).
- **PRG redirect headers** — `post_form` now generates fresh page-load
  headers for the redirected GET instead of reusing the form POST's
  `Content-Type`/`Origin` (a real browser issues the GET as a fresh
  document navigation).
- **`request_type="xhr"` plumbing** — `post_json`/`delete_json` now
  accept `request_type="xhr"` so endpoints that the Travian frontend
  calls via fetch (map/position, tile-details, /api/v1/farm-list/*)
  send the XHR header shape (`X-Requested-With`, `Sec-Fetch-Mode: cors`)
  instead of generic JSON-client headers.
- **Persona scoping** — TTL bumped from 7d to 365d (rotating UA/TLS
  mid-cookie-jar was itself a tell); persona file now tracks the server
  URL and rotates on a server change.
- **Jittered `get_html` retry** — `wait_random_exponential` instead of
  the textbook 1s/2s/4s power-of-two cadence.
- **Captcha guard escalation** — short 403/503 pages with high-confidence
  block phrases (`access denied`, `bot-detection`, `automated access`,
  `your ip has been`) now hard-fire the captcha guard; soft-penalty
  reserved for transient signals (429, embedded-in-bundle false
  positives).
- **`zstd` Accept-Encoding** — Chromium personas advertise zstd to match
  current Chrome stable (mismatch is checked against UA in modern
  detectors).
- **Navigator helpers** — new `navigate_to_map`, `navigate_to_farm_list`,
  `pre_construct_flow` so feature code stops calling private `_visit`.

#### Per-feature stealth fixes

- **Oasis raider** — pre-tile-details navigation is now `navigate_to_map`
  (not random `idle_browse` which produced impossible Referer chains);
  troop deduction + burst counter only on `result.success` (soft failure
  applies a 60s throttle penalty and breaks the sweep — no more "ghost
  sends" with locally-deducted-but-server-untouched troops); recurring
  interval gets `micro_jitter(0.10)`; tile XHRs route through
  `request_type="xhr"`.
- **Auto-scout** — scan centers sorted by distance from player village
  then shuffled within 4-tile buckets (in-place, not a slice copy); same
  bucket-shuffle on `filter_targets` so target permutation varies across
  runs; `navigate_to_map` before tile loop; tile XHRs via XHR header
  shape; jittered scout-retry replaces fixed 3s.
- **Farm-list** — stealth floor of 60s on `interval` when stealth is on;
  `time.monotonic()` activity logging in try/finally so every exit path
  feeds the scheduler; `navigate_to_farm_list(owner_vid)` before send;
  `send_all_farm_lists` groups by owner village so cross-village handoffs
  trigger fresh navigation; per-cycle batch size jitter (4-7) instead of
  invariant 5; 0.25-0.9s pause between batches; troop-exhaustion now
  advances the cursor PAST the depleted batch (no more bot-like
  instant-retry on the same empty slots); send + add_slot + delete_slots
  use `request_type="xhr"`.
- **Build queue** — `_post_build_reaction()` heavy-tailed 20-300s wakeup
  window after every queue→empty transition (real players don't return
  to the browser exactly 3 seconds after a timer hits zero);
  `pre_construct_flow` for new construction (after queue/can-build
  guards so no wasted page loads on requests that will be rejected);
  client `poll_interval` clamped to [30s, 1h]; resource-short waits
  2-10min planner-style instead of polling every 30s; per-account
  build-action lock + 10-90s stagger when multiple village queues
  collide on the same account.
- **Farm builder** — `_pace_add(list_id)` before every add_slot
  (RAPID-class delay + per-8-slots heavy-tailed pause, gated on
  `stealth_enabled` AND `human_delay.enabled`); pacing inside retry loop
  and after overflow handoff; `navigate_to_farm_list()` once before bulk
  edit; per-bucket shuffle so target order varies between runs;
  jittered scout-send / report-fetch retries.

See `docs/23-stealth-decisions.md` for the trade-off analysis behind each
choice.

---

## [Previous] — 2026-04-13

### Added

#### Captcha/Bot Detection — Emergency Halt System
- **CaptchaGuard module** — New `asyncio.Event`-based per-user gate (`stealth/captcha_guard.py`) that blocks ALL outbound HTTP requests when bot detection fires, and resumes when the user resolves the captcha.
- **Structural validation** — Bot detection now uses HTML structural evidence (captcha divs, script tags, error page size, HTTP status codes) instead of naive substring matching to avoid false positives from the word "recaptcha" appearing in normal Travian JS bundles.
- **Rich diagnostic logging** — When detection fires, logs include: URL, HTTP status code, response length, and a ±200 char snippet around the matched pattern (HTML-stripped).
- **Full-screen captcha alert modal** — Non-dismissible modal (`CaptchaAlert.jsx`) that appears on any page when bot detection triggers, showing pattern, URL, status code, response snippet, and step-by-step resolution instructions.
- **"Dismiss (False Positive)" button** — Quick resolution option for false positives without requiring a browser visit.
- **REST endpoints** — `GET /api/captcha/status` and `POST /api/captcha/resolve` for checking and clearing captcha state.
- **Page-refresh recovery** — Frontend checks captcha status on WebSocket connect so the modal reappears after a page refresh.
- **Multi-tab broadcast** — `captcha_alert`/`captcha_resolved` messages broadcast to all user WS connections.

#### Auto Scout — Real Player Population from Profile Pages
- **Profile page population lookup** — New `get_player_population(player_id)` method fetches `/profile/<player_id>` and extracts real account-wide population from the React JSON data (`ranks.population`), not the misleading `<div class="population">` footer which shows the logged-in user's own pop.
- **Batch profile fetching** — `fetch_player_populations(player_ids)` fetches multiple profiles sequentially (respects throttler) with progress reporting.
- **Occupied oasis population inheritance** — Occupied oases (population=0) now inherit their owner's total population from their profile, so village-level and player-level filters both apply correctly.
- **Profile vs visible breakdown in UI** — Scan results show `PlayerName: 676 (profile) | visible: 228 = Village(16,93)=228` when profile data differs from visible village sums.

### Fixed

#### Auto Scout — Max Player Pop Filter (Critical Logic Error)
- **Incorrect population calculation** — The `max_player_pop` filter was summing populations only from villages found within the scan radius. Players with villages outside the radius had their total population undercounted, allowing large players to bypass the filter. Now fetches real total population from each player's profile page.

### Changed

#### Bot Detection — Reduced False Positives
- **`recaptcha` pattern** — No longer triggers on bare substring match in large responses. Requires structural HTML evidence (`class="g-recaptcha"`, `<script src="...recaptcha/api...">`), a short error page (<5000 chars), or an error HTTP status (403/429/503).
- **Other high-confidence patterns** — `bot-detection`, `suspicious activity`, `access denied`, etc. now also require short response or error status code context before triggering the guard.

---

## [Previous] — 2026-04-12

### Added

#### Auto Scout — Player Population Debug Logging
- **Player population breakdown in scan logs** — After enrichment, the scan WS now always sends a `player_pops` message showing each player's total population with per-village breakdown (e.g., `PlayerA: 350 = Village1(50,60)=200 + Village2(55,65)=150`). Previously this was only shown when `max_player_pop` was set.
- **Player population in auto-scout logs** — The auto-scout WS panel also displays per-player population breakdown before scouting begins, making it easy to verify the `max_player_pop` filter calculation.

#### Auto Scout — Farm List Integration
- **Farm list badges in scan results** — Each scan result row now shows which farm list(s) the target already belongs to, displayed as gold badges in a new "Farm Lists" column.
- **Quick-add to farm list** — Each scan result row has a "+Farm" button that opens a dialog to add the target to any farm list with configurable troop type and count.
- **`GET /api/farm/coord-map` endpoint** — New lightweight API that returns a coordinate-to-farm-list mapping for all farm list slots, enabling efficient lookup without N+1 queries.
- **Shared troop constants** — Extracted `TRIBE_TROOPS` and `DEFAULT_TROOPS` from `Military.jsx` into `constants/troops.js` for reuse by `AddToFarmDialog`.

#### Farm Lists — Defense Scan Combat Strength
- **Combat strength extraction** — Battle report parser now extracts attacker and defender combat strength from the `<table class="combatStatistic">` element, supporting both English ("Combat strength") and German ("Kampfkraft") labels.
- **Combat strength display** — Farm list defense column now shows the defender's combat strength value (red when defenders present, green "Empty" when 0), with troop breakdown in hover tooltip.

### Fixed

#### Auto Scout — Occupied Oasis Handling
- **Oasis player info preservation** — During tile enrichment, player/alliance info from the map scan is now preserved when the tile-details HTML parser doesn't extract it (e.g., occupied oases using "Occupied by" instead of "Owner").
- **Oasis owner detection** — The tile-details parser regex now matches both `Owner` and `Occupied by` labels for player extraction.
- **`max_player_pop` filter for oases** — With player info properly preserved, occupied oasis population is now correctly included in player totals and the `max_player_pop` filter applies to them.

#### Farm Lists — Defense Scan (3 critical bugs)
- **Dict-as-object access** — `scan_defense_strength` used `getattr(detail, 'data')` on a plain dict, which always returned None. Fixed to use `detail.get('data')`.
- **Nested coordinate extraction** — Defender coordinates were accessed as flat `x`/`y` keys but the parser returns `{'coordinates': {'x': ..., 'y': ...}}`. Fixed to access the nested structure. This was the root cause of defense data never populating.
- **Report age computation** — Report age was read from a non-existent `time` attribute. Fixed to parse `date_str` using `parse_report_date()`.

---

## [Previous] — 2026-04-09

### Added

#### Web UI (`travian-web`)
- **Full self-hosted web dashboard** — React + Vite + Tailwind CSS frontend served by FastAPI backend
- **Multi-user auth** — SQLite with bcrypt password hashing, JWT tokens, Fernet-encrypted Travian credential storage
- **Per-user session isolation** — Independent HTTP clients, cookies, and service instances per user
- **Auto-reconnect** — Saved credentials used to restore sessions automatically on 403
- **Dashboard** — Resource bar, construction queue with live countdown timers, quick actions, player info
- **Buildings page** — List all building slots, upgrade/construct with confirmation, live construction queue
- **Farm Lists page** — Full CRUD with sortable/filterable slot table, booty as taken/capacity (183/300), last raid datetime, active/inactive sync, multi-select copy/move between lists, background defense scan from reports
- **Auto Scout page** — Map scan with alliance/player exclusion (persisted in localStorage), population filters, max player pop, loop mode with configurable interval
- **Military page** — Tribe-aware scout dispatch, manual raid form with dynamic troop fields
- **Reports page** — Paginated report browser with collapsible raid analyzer panel
- **Build Queue page** — Visual drag-and-drop plan builder, YAML editor, template library (6 tribe-specific templates), validation, live WebSocket execution
- **Video Rewards page** — Claim individual or all production boosts
- **Activity Log** — Real-time server-side log streaming via WebSocket, client/server origin filter, level badges, search, export to JSONL
- **Server-side log broadcast** — `LogBroadcastHandler` captures Python logging records and streams to connected web clients
- **WebSocket channels** — Farm loop-send, auto-scout, build queue execution, log streaming, raid analyzer — all with live progress
- **Rate limiting** — Per-user sliding-window rate limiter on action routes (upgrade, construct, scout, raid)
- **Correlation IDs** — `X-Request-ID` header on all API responses for request tracing
- **Security headers** — CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy (relaxed in dev mode via `TRAVIAN_DEV=1`)
- **Light theme** — CSS variable-based theme with `[data-theme="light"]` support

#### CLI
- **`travian reports village X Y`** — Gather all reports (own + alliance) for any village from the map tile popup. Uses `/api/v1/map/tile-details` endpoint. Supports `--details` for full report parsing.
- **Build plan templates API** — `GET /api/queue/templates` returns 6 tribe-specific build plans (resource, military-roman, military-teuton, military-gaul, economy, settler)

#### Raid Analyzer v2 Pipeline
- **Scout-gated entry** — Only scout reports enter the pipeline (with battle report fallback when no scouts exist)
- **Coordinate deduplication** — Multiple scouts on the same target produce one fetch, not many
- **GQL pre-filter** — Radius, alliance, NAP alliances, population threshold — all applied before any HTML fetch
- **`fetch_village_reports` integration** — One API call per target returns all reports (own + alliance)
- **Cross-run TTL cache** — `VillageReportCache` with configurable TTL (default 30min). Repeated runs 90%+ faster
- **Short-circuit on depletion** — If most recent raid had `carry_full=false`, skip all computation, straight to re-scout queue
- **Binary search scoring** — O(log n) instead of O(5000) for defended targets. Direct O(1) calculation for undefended
- **Re-scout queue** — Depleted + stale targets output as actionable list with reason, distance, last report time
- **New settings** — `nap_alliances`, `max_population`, `stale_hours`, `cache_ttl_minutes`, `village_report_concurrency`

#### Scout Optimization
- **Navigate-once** — Rally point navigation only on first target, subsequent targets skip it
- **Stealth countdown** — `"waiting"` messages streamed during inter-target delays with seconds remaining
- **ETA computation** — After first target, estimated completion time shown in progress bar
- **Pre-send target list** — All targets sent to frontend immediately after scan
- **Loop mode** — Checkbox in Auto Scout panel, configurable interval, re-scouts same targets repeatedly

### Fixed

#### Backend (19 fixes across 3 rounds)
- `http_client.py` — Concurrent `follow_redirects` race condition (per-request param instead of client mutation)
- `http_client.py` — `get_html` 429 response not penalized (added 120s throttle penalty)
- `http_client.py` — Cookie load/save errors silently swallowed (now logged)
- `http_client.py` — `ConnectionResetError` from Travian rate limit (30s penalty + retry)
- `ws/manager.py` — WebSocket `close()` without `accept()` per ASGI spec
- `ws/logs_ws.py` — Log subscriber ID collision for multi-tab users (unique per connection)
- `ws/logs_ws.py` — `_stream_logs` crash on send after close (`RuntimeError` caught)
- `ws/queue_ws.py` — Anonymous `stop_event.wait()` task leaked (named variable)
- `ws/queue_ws.py` — `run_coroutine_threadsafe` race condition (replaced with `asyncio.Queue`)
- `ws/farm_ws.py` — `next_send_at` inverted logic + field names mismatched frontend
- `ws/farm_ws.py` — Per-target failure details only in verbose mode (now always sent)
- `routes/farm.py` — Defense scan `except: continue` without logging
- `routes/scout.py` — `max_player_pop` description misleading (clarified as scan-radius only)
- `raid_analyzer_service.py` — Coordinate (0,0) silently dropped (changed to `None` sentinel)
- `raid_analyzer_service.py` — `eff_R` double-penalizes raided targets (only decay for "scouted")
- `raid_analyzer_service.py` — Trap capacity formula wrong (`4+36*L` → `12*L` per Travian spec)
- `raid_analyzer_service.py` — `last_report_time` set to last-iterated, not newest
- `raid_analyzer_service.py` — `_resolve_source_village` silently returns (0,0) (now raises error)
- `raid_analyzer_service.py` — Missing `Callable` import
- `auto_scout_service.py` — `enrich_tiles` concurrent semaphore bypassed throttler burst limit (now sequential)
- `reports_service.py` — Page count log variable wrong after break
- `reports_service.py` — `fetch_village_reports` used `karte.php` HTML instead of `tile-details` API
- `rate_limit.py` — Unbounded key growth (prune empty keys when >100)
- `app.py` — SPA catch-all returned `None` for API 404s (now returns `JSONResponse(404)`)
- `auth.py` — Keys file in CWD (moved to `~/.travian/`), permissions warning on Unix
- `db.py` — Database path hardcoded (now `~/.travian/travian_web.db`, configurable via `TRAVIAN_DB_PATH`)

#### Frontend (14 fixes across 3 rounds)
- `Reports.jsx` — `useState` used instead of `useRef` for WebSocket (crash + memory leak)
- `Reports.jsx` — Re-clicking Analyze leaks previous WebSocket
- `authStore.js` — `require()` fails in Vite/ESM (replaced with top-level imports)
- `logStream.js` — `reconnectAttempts` never reset after disconnect (reconnect permanently disabled)
- `AutoScout.jsx` — Stale closure in loop mode (all loop-read values moved to refs)
- `BuildQueue.jsx` — Timer accumulation on re-execute (clear before new)
- `Buildings.jsx` / `BuildQueue.jsx` — Construction queue countdown never ticks (added interval timer)
- `Buildings.jsx` / `BuildQueue.jsx` — Missing `activeVillageId` dep (stale data after village switch)
- `Dashboard.jsx` — Timer tick applied uniformly to all queue items (snapshot-based)
- `Dashboard.jsx` — Missing `activeVillageId` dep in data-loading useEffect
- `FarmLists.jsx` — `stopLoop` doesn't close WebSocket (only sends stop message)
- `ErrorBoundary.jsx` — `require()` fails in Vite/ESM (changed to dynamic `import()`)
- `Military.jsx` — NaN stored for non-numeric troop input (`isNaN` guard)
- `Layout.jsx` — Redirect fires without checking current route + duplicate ToastContainer
- `VillageSelector.jsx` — No disable during async village switch (concurrent calls)

### Changed
- `pyproject.toml` — Added `[web]` optional dependencies (fastapi, uvicorn, sqlalchemy, etc.) and `travian-web` entry point
- `logging_config.py` — `SensitiveDataFilter` only redacts actual credential values (not messages about credentials)
- `app.py` — CSP headers relaxed in dev mode (`TRAVIAN_DEV=1`), correlation ID middleware added
- Farm WS `cycle_end` fields renamed `success`→`sent`, `fail`→`failed`, `cycles`→`total_cycles` to match frontend
- `SavedServerResponse` field `travian_username`→`username` to match frontend
