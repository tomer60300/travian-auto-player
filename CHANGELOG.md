# Changelog

## [Unreleased] — 2026-04-09

### Added

#### CLI
- **`travian reports village X Y`** — Gather all reports (own + alliance) for any village from the map tile popup. Uses `/api/v1/map/tile-details` endpoint. Supports `--details` for full report parsing.

#### Farm List — Round-Robin Batched Sending
- **Batched round-robin** — Farm list sends now use small batches (5 targets per API call) with a persistent cursor. Distributes troops fairly across all targets instead of always filling top-to-bottom.
- **Cursor persistence** — The cursor advances with each cycle, so the next send picks up where the last one left off.
- **Troop exhaustion detection** — When a full batch returns "not enough troops", sending stops immediately (no wasted API calls).

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

#### Models & Parsers
- `Resources` model gains `lumber_per_hour`, `clay_per_hour`, `iron_per_hour`, `crop_per_hour` fields
- `MapTileInfo` gains `alliance_name` field
- `AnalysisResult` gains v2 pipeline stats: `re_scout_targets`, `village_reports_fetched/cached/failed`, `pipeline_version`
- New `ReScoutTarget` model for re-scout queue output
- New `parse_troop_overview()` parser — extracts total troops (t1-t10) from village statistics
- New `parse_map_tile_reports()` parser — extracts report links + village metadata from map tile popups

### Fixed

#### HTTP Client
- Concurrent `follow_redirects` race condition — now per-request instead of mutating client state
- HTTP 429 response not penalized — added 120s throttle penalty
- Cookie load/save errors silently swallowed — now logged as warnings
- `ConnectionResetError` from Travian rate limit — 30s penalty + automatic retry

#### Services
- `raid_analyzer_service` — `eff_R` double-penalizes raided targets (only decay for "scouted")
- `raid_analyzer_service` — Trap capacity formula wrong (`4+36*L` → `12*L` per Travian spec)
- `raid_analyzer_service` — `last_report_time` set to last-iterated, not newest report
- `raid_analyzer_service` — `_resolve_source_village` silently returns (0,0) — now raises error
- `auto_scout_service` — `enrich_tiles` concurrent semaphore bypassed throttler burst limit — now sequential
- `build_queue_service` — Sleep capped at 60s causing wasteful polling — now sleeps actual remaining time + 3s buffer
- `farm_list_service` — `delete_slots` broken with curl_cffi transport (cookies not shared) — now uses `_curl_session` when available
- `military_service` — Troop send success detection used `confirmSendTroops` (always present) — replaced with `troopMovement` check
- `reports_service` — Page count log variable wrong after break
- `farm_list.py` — `LastRaid` fields `report_object_id` and `auth_key` crash on null — made Optional

### Changed
- `logging_config.py` — `SensitiveDataFilter` extracted to module-level class with precise patterns (only redacts actual credential values, not messages that mention credentials)
- `.gitignore` — `*.json` → `/*.json` (root-only, no longer ignores JSON files in subdirectories)
- `farm_list_service.py` — `send_farm_list()` rewritten with round-robin batched sending for fair troop distribution
