# Travian API

A Python library and CLI for automating Travian Legends gameplay. Async-first, multi-village, with stealth anti-bot protection.

## Features

- **🔐 Authentication** — 2-step login with JWT caching, interactive setup prompt
- **🏘️ Multi-Village** — Full support for multiple villages per account
- **🏗️ Auto-Builder** — YAML-based build queue with priorities, multi-level chaining, gold guard, and video speedup
- **⚔️ Military** — Scouts, raids, attacks with tribe-aware troop selection
- **🌾 Farm Lists** — Full CRUD + smart raid intelligence (last raid, carry ratio, distance, booty)
- **🔭 Auto-Scout** — Scan map, filter by population/distance/player/alliance, send scouts with loop mode
- **📊 Reports** — Fetch and parse scout/battle reports with smart type detection
- **📍 Village Reports** — Gather all reports (own + alliance) for any village from the map tile
- **📈 Raid Analyzer v2** — Scout-gated pipeline with binary search scoring, cache, and re-scout queue
- **🎬 Video Rewards** — Automated ATG ad simulation for production boosts and build speedups
- **🛡️ Gold Guard** — Never spends gold unless you explicitly opt in

## Quick Start

### Install

```bash
git clone <repository-url>
cd travian-api
pip install -e .
travian-setup
```

`travian-setup` checks if the `travian` command is on your PATH. If not, it offers to add it automatically (no admin needed on Windows). You only need to run this once.

If `travian-setup` isn't found either, run it as: `python -m travian_api._post_install`

**Alternative**: skip all of that and always use `python -m travian_api` instead of `travian`:
```bash
python -m travian_api auth login
python -m travian_api queue run plan.yaml
```

### First run

Just run any command — it'll prompt for credentials if nothing is configured:

```bash
travian auth login
```

```
Server URL (e.g. https://ts1.x1.europe.travian.com): https://ts1.x1.europe.travian.com
Username/email: me@email.com
Password: ****
Save credentials to .env? [Y/n]: y
Saved to C:\projects\travian-api\.env

OK - Logged in!
  Player: Chieftain
  Tribe: 2 (Teuton)
  Villages (2):
    20030  Main Village  (-160|168)  (main)
    20031  New village   (-161|167)
```

After saving, every command works without typing credentials again.

### Other ways to configure

**CLI flags** (one-off):
```bash
travian --server https://ts1.x1.europe.travian.com --username me@email.com --password secret auth login
```

**Environment variables**:
```bash
export TRAVIAN_BASE_URL=https://ts1.x1.europe.travian.com
export TRAVIAN_USERNAME=me@email.com
export TRAVIAN_PASSWORD=secret
```

**`.env` file** (auto-loaded):
```env
TRAVIAN_BASE_URL=https://ts1.x1.europe.travian.com
TRAVIAN_USERNAME=me@email.com
TRAVIAN_PASSWORD=secret
```

## CLI Reference

### Authentication

```bash
# Login and show player info
travian auth login

# Print the current JWT token (useful for debugging or external tools)
travian auth token
```

### Villages

```bash
# List all your villages
travian village list

# Switch active village context
travian village switch 20031
```

### Buildings

All building commands accept `--village-id` / `-v` to target a specific village:

```bash
# List all buildings
travian building list
travian building list -v 20031

# Show current resources
travian building resources
travian building resources -v 20031

# Show construction queue
travian building queue

# Upgrade a building (gold guard ON by default)
travian building upgrade --slot-id 15
travian building upgrade --slot-id 15 -v 20031

# Allow gold spend if queue is occupied
travian building upgrade --slot-id 15 --allow-gold

# Construct a NEW building on an empty slot (slots 19-40)
# (upgrade levels existing buildings; construct places new ones)
travian building construct --slot-id 25 --building Cranny
travian building construct --slot-id 25 --building Embassy -v 20031
travian building construct --slot-id 25 --building Barracks --allow-gold
```

### Military

```bash
# Send scouts
travian military scout --x 100 --y 200 --amount 5
travian military scout --x 100 --y 200 --amount 3 --type defenses

# Send from a specific village
travian military scout --x 100 --y 200 --amount 5 --village-id 20031

# Send a raid (uses currently active village -- switch first if needed)
travian military raid --x 50 --y -30 --troop t1=10 --troop t2=5
```

### Farm Lists

Manage farm lists and trigger raids with full visibility into raid performance. Works without Gold Club for everything except sending.

#### List all farm lists

```bash
travian farm list
```

```
                              Farm Lists
┌───────┬───────────────┬───────┬─────────┬──────────────┬────────────┐
│    ID │ Name          │ Slots │ Running │ Last Started │ Village ID │
├───────┼───────────────┼───────┼─────────┼──────────────┼────────────┤
│ 10165 │ My Raid List  │     5 │    0    │       2h ago │      20030 │
│ 10200 │ Inactive Farms│     3 │    0    │       never  │      20031 │
└───────┴───────────────┴───────┴─────────┴──────────────┴────────────┘
```

#### Show farm list with raid intelligence

The `farm show` command displays everything you need to make smart raiding decisions:

```bash
travian farm show 10165
```

```
My Raid List  (id=10165)
  Village: 20030  |  Running raids: 0  |  Slots: 5
  Available troops: t1=490 t2=33 t3=141 t4=48 t5=31 t6=0

                                    Targets
┌───┬──────────┬─────┬──────┬────────┬──────────┬───────────┬───────────┬───────────┬──────────┐
│ # │ Target   │ Pop │ Dist │ Troops │ Last Raid│ Raided/Cap│ Result    │ Status    │ Total    │
├───┼──────────┼─────┼──────┼────────┼──────────┼───────────┼───────────┼───────────┼──────────┤
│ 1 │ Village1 │  12 │  1.4 │  t1=5  │  15m ago │   450/600 │ no loss   │ ready     │   3,200  │
│   │ (-162|…) │     │      │        │          │           │           │           │ (8 raids)│
├───┼──────────┼─────┼──────┼────────┼──────────┼───────────┼───────────┼───────────┼──────────┤
│ 2 │ Village2 │  45 │  3.2 │  t1=10 │   2h ago │   120/600 │ some loss │ raiding...│   1,500  │
│   │ (-159|…) │     │      │  t4=1  │          │           │           │           │ (3 raids)│
├───┼──────────┼─────┼──────┼────────┼──────────┼───────────┼───────────┼───────────┼──────────┤
│ 3 │ Village3 │   8 │  5.0 │  t1=3  │    never │         — │ —         │ inactive  │        — │
└───┴──────────┴─────┴──────┴────────┴──────────┴───────────┴───────────┴───────────┴──────────┘
```

**Columns explained:**

| Column | Description |
|--------|-------------|
| **Pop** | Target village population |
| **Dist** | Distance in fields from source village |
| **Troops** | Troop composition assigned to this target (t1–t10) |
| **Last Raid** | Time since last raid was sent |
| **Raided/Cap** | Resources raided vs carry capacity. Colour-coded: green (>70%), yellow (30-70%), red (<30%) |
| **Result** | Last raid outcome: `no loss` (green), `some loss` (yellow), `all dead` (red) |
| **Status** | Current state: `ready`, `raiding...`, `scouting...`, or `inactive` |
| **Total** | Cumulative resources raided and total raid count |

#### Send a farm list

```bash
# Interactive — shows target count and asks for confirmation
travian farm send 10165

# Skip confirmation
travian farm send 10165 --yes

# Dry run -- show what would be sent without actually sending
travian farm send 10165 --dry-run
```

> **Note:** Sending requires Gold Club. Without it, the API returns an error. All other farm list operations (create, add targets, view, delete) work without Gold Club.

#### Create, add targets, delete

```bash
# Create a new farm list
travian farm create --name "New Raid List"
travian farm create --name "Village 2 List" --village-id 20031

# Add a target with troop composition
travian farm add-target 10165 --x -162 --y 167 -t t1=5
travian farm add-target 10165 --x -159 --y 168 -t t1=3 -t t4=1

# Force add (even if target exists in another list)
travian farm add-target 10165 --x -162 --y 167 -t t1=5 --force

# Delete a farm list
travian farm delete 10165
travian farm delete 10165 --yes  # skip confirmation
```

#### Send all farm lists

Send all farm lists at once (or a subset by ID):

```bash
# Send all farm lists
travian farm send-all --yes

# Send specific lists only
travian farm send-all --lists 10165,10200 --yes

# Dry run
travian farm send-all --dry-run
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--lists` | `-l` | all | Comma-separated list IDs to send (default: all lists) |
| `--dry-run` | — | false | Show plan without sending |
| `--yes` | `-y` | false | Skip confirmation prompt |

> **Note:** Requires Gold Club, same as `farm send`.

#### Loop-send a farm list

Continuously send a farm list at a fixed interval. Runs until stopped with Ctrl+C or duration expires:

```bash
# Send farm list 10165 every 5 minutes (default), forever
travian farm run 10165

# Custom interval: every 3 minutes, for 2 hours
travian farm run 10165 --interval 180 --duration 120

# Dry run -- show config without starting loop
travian farm run 10165 --dry-run

# Show per-slot details on each send
travian farm run 10165 --verbose
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--interval` | `-i` | 300 | Seconds between sends |
| `--duration` | `-d` | 0 | Total minutes to run (0 = forever, until Ctrl+C) |
| `--dry-run` | — | false | Show plan without starting the loop |
| `--verbose` | — | false | Show per-slot send details each round |

> **Note:** Requires Gold Club. If Gold Club is not active, the loop exits immediately with an error.

#### Loop-send all farm lists

Like `farm run`, but sends all (or a subset of) farm lists each interval:

```bash
# Send all farm lists every 5 minutes, forever
travian farm run-all

# Send specific lists every 2 minutes, for 1 hour
travian farm run-all --lists 10165,10200 --interval 120 --duration 60

# Dry run
travian farm run-all --dry-run
```

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--lists` | `-l` | all | Comma-separated list IDs (default: all lists) |
| `--interval` | `-i` | 300 | Seconds between sends |
| `--duration` | `-d` | 0 | Total minutes to run (0 = forever) |
| `--dry-run` | — | false | Show plan without starting the loop |
| `--verbose` | — | false | Show per-list send details each round |

---

### Auto-Scout

Scan the map around your village, discover targets, filter by conditions, and send scouts automatically.

#### Scan (preview only)

Scan the map and display results without sending anything:

```bash
# Basic scan — enriches tiles with population and player data
travian scout scan --radius 10

# Filter by population
travian scout scan --radius 15 --max-pop 50
travian scout scan --radius 15 --min-pop 5 --max-pop 50

# Fast scan without tile enrichment (no population data)
travian scout scan --radius 20 --no-enrich

# Only show villages with no active player
travian scout scan --radius 10 --no-player

# Include oases in results
travian scout scan --radius 10 --show-oases

# Scan from a specific village
travian scout scan --radius 10 --village-id 20031

# Limit results
travian scout scan --radius 20 --limit 30
```

```
Scanning from Chieftain`s village (-161|166) radius=10
  Scanning 1 map region(s) around (-161,166) r=10
  Found 45 tiles with villages/oases in radius
  Enriching 28 tiles with details...

Found 15 targets:
                                 Scan Results
┌────┬────────────┬───────────────────────┬─────┬──────┬─────────────┬────────┐
│  # │ Coords     │ Name                  │ Pop │ Dist │ Player      │ Tribe  │
├────┼────────────┼───────────────────────┼─────┼──────┼─────────────┼────────┤
│  1 │ (-162|167) │ KAK Köyü              │  12 │  1.4 │ KAK         │ Gauls  │
│  2 │ (-159|168) │ CENGİZHAN80 Köyü      │  12 │  2.8 │ CENGİZHAN80 │ Gauls  │
│  3 │ (-164|166) │ Bergen                │  42 │  3.0 │ Odin        │ Teuton │
│ ...│            │                       │     │      │             │        │
└────┴────────────┴───────────────────────┴─────┴──────┴─────────────┴────────┘
```

#### Auto-scout (scan + send)

Scan, filter, and send scouts in one command:

```bash
# Scout all low-pop villages within radius 10 (resource scout)
travian scout auto --radius 10 --max-pop 50 --amount 1 --type resources --yes

# Scout with defenses type (reveal troops)
travian scout auto --radius 10 --max-pop 30 --amount 2 --type defenses --yes

# Dry run — show what would be scouted
travian scout auto --radius 15 --max-pop 100 --dry-run

# Use an exclude list to skip known targets
travian scout auto --radius 10 --exclude exclude.txt --yes

# Limit number of targets and add delay between sends
travian scout auto --radius 20 --limit 10 --delay 2.0 --yes

# Scout from a different village
travian scout auto --radius 10 --village-id 20031 --amount 1 --yes
```

```
Auto-Scout from Chieftain`s village (-161|166) r=10 type=resources amount=1
  Scanning 1 map region(s) around (-161,166) r=10
  Found 45 tiles with villages/oases in radius
  Enriching 28 tiles...

6 targets to scout:
┌───┬────────────┬──────────────────┬─────┬──────┬─────────────┐
│ # │ Coords     │ Name             │ Pop │ Dist │ Player      │
├───┼────────────┼──────────────────┼─────┼──────┼─────────────┤
│ 1 │ (-162|167) │ KAK Köyü         │  12 │  1.4 │ KAK         │
│ 2 │ (-159|168) │ CENGİZHAN80 Köyü │  12 │  2.8 │ CENGİZHAN80 │
│ ...│           │                  │     │      │             │
└───┴────────────┴──────────────────┴─────┴──────┴─────────────┘
  [1/6] Scouting (-162,167) KAK Köyü pop=12 dist=1.41
    -> Scouts sent! Travel: 0:09:26
  [2/6] Scouting (-159,168) CENGİZHAN80 Köyü pop=12 dist=2.83
    -> Scouts sent! Travel: 0:18:51
  ...
  Done: 6/6 scouts sent successfully

Results: 6/6 scouts sent
```

#### Exclude file format

Create a text file with coordinates to skip (one per line):

```
# exclude.txt — coordinates to never scout
# Format: x,y or x|y (pipe-separated also works)
-162,167
-159,168
-164|166

# Comments and blank lines are ignored
```

#### All auto-scout options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--radius` | `-r` | 10 | Scan radius in fields from village center |
| `--village-id` | `-v` | main village | Source village for scanning and sending scouts |
| `--max-pop` | — | no limit | Max target population (filter out large villages) |
| `--min-pop` | — | no limit | Min target population |
| `--type` | `-t` | `resources` | Scout type: `resources` (reveal resources) or `defenses` (reveal troops) |
| `--amount` | `-n` | 1 | Number of scouts to send per target |
| `--exclude` | `-e` | none | Path to exclude file (coordinates to skip) |
| `--no-player` | — | false | Only scout villages with no active player |
| `--show-oases` | — | false | Include oases in scan results |
| `--limit` | `-l` | 20 | Max number of targets to scout |
| `--dry-run` | — | false | Show targets without sending scouts |
| `--yes` | `-y` | false | Skip confirmation prompt |
| `--delay` | — | 1.0 | Seconds between scout sends (rate limiting) |

---

### Reports

```bash
# List recent reports (default: last 24 hours, up to 5 pages)
travian reports list
travian reports list --max-age-hours 48 --max-pages 10

# Show detailed report
travian reports show <report-id>

# Gather all reports for a specific village (own + alliance)
travian reports village 14 98
travian reports village 14 98 --details          # fetch full report data
travian reports village 14 98 -d --max-details 3 # limit detail fetches

# Analyze raid targets (v2 pipeline)
travian reports analyze --radius 15 --min-resources 100
travian reports analyze --radius 20 --stale-hours 12 --nap-alliance HM2 --nap-alliance LR
travian reports analyze --max-population 300 --json  # JSON output for automation
```

The raid analyzer v2 pipeline:
1. Scans your inbox for scout reports (falls back to battle reports if no scouts)
2. Deduplicates to unique target coordinates
3. Pre-filters by radius, alliance, NAP alliances, population via GQL metadata
4. Fetches full village-reports (own + alliance) for each surviving target
5. Reconstructs target state: resources, defenders, wall, traps
6. Scores using combat simulation with binary search optimization
7. Outputs ranked targets + a re-scout queue for depleted/stale targets

Results are cached (30min TTL) — repeated runs are 90%+ faster.

### Video Rewards

```bash
# Check availability
travian video available

# Claim a production boost (~33 seconds)
travian video claim ironProductionBonus

# Claim all available production boosts
travian video claim-all

# Claim building speedup
travian video claim buildingUpgrade --village-id 20031 --slot-id 3 --building-id 8
```

Available types: `lumberProductionBonus`, `clayProductionBonus`, `ironProductionBonus`, `cropProductionBonus`, `buildingUpgrade`

### Auto-Builder (Build Queue)

The main feature. Define what to build in YAML, run one command, walk away.

#### Step 1: Find your slots

```bash
travian building list -v 20031
```

```
┌─────────────────────────────────────┐
│ Slot │ Name       │ GID │ Level    │
├──────┼────────────┼─────┼──────────┤
│    3 │ Clay Pit   │   8 │       2  │
│    5 │ Clay Pit   │   8 │       3  │
│    8 │ Clay Pit   │   8 │       3  │
│   12 │ Clay Pit   │   8 │       6  │
│   19 │ Cranny     │  23 │       1  │
└──────┴────────────┴─────┴──────────┘
```

#### Step 2: Create `plan.yaml`

```yaml
village: 20031
plan:
  # --- Priority 1: build these first ---

  # Upgrade Clay Pit at slot 3 (Lv2 → Lv5, chains automatically)
  - slot: 3
    expect: Clay Pit       # safety guard — skips if slot 3 isn't a Clay Pit
    target: 5
    priority: 1

  # Upgrade Clay Pit at slot 5 (Lv3 → Lv5)
  - slot: 5
    expect: Clay Pit
    target: 5
    priority: 1

  # --- Priority 2: after all P1 items are done ---

  # Unique buildings can use name instead of slot
  - building: Cranny
    target: 5
    priority: 2

  # --- Priority 3: low priority ---

  - building: Residence
    target: 10
    priority: 3
```

#### Plan YAML format

**Top-level:**

| Field | Required | Description |
|-------|----------|-------------|
| `village` | Yes | Village ID (from `travian village list` or `travian auth login`) |
| `plan` | Yes | List of build items |

**Per-item:**

| Field | Required | Description |
|-------|----------|-------------|
| `slot` | One of `slot` or `building` | Slot ID (1-40). **Use for resource fields** with duplicates (4 Clay Pits, 6 Croplands, etc.) |
| `building` | One of `slot` or `building` | Building name (partial, case-insensitive). For unique buildings like Cranny, Barracks. If multiple match, picks the lowest level below target. |
| `expect` | No | Safety guard for `slot` items. Verifies the building name matches (partial). **Skips with a warning if it doesn't match.** |
| `target` | Yes | Target level. Auto-chains: Lv2 with target 5 → upgrades 2→3→4→5 |
| `priority` | No (default: 5) | Any positive integer. 1 = build first, higher = later. Same priority: whichever has resources first |

#### Step 3: Validate

```bash
travian queue validate plan.yaml
```

Shows resolved slots, current levels, what's already done, and any mismatches.

#### Step 4: Run

```bash
# Dry run — preview without building
travian queue run plan.yaml --dry-run

# Run for real
travian queue run plan.yaml

# With video speedup (~33s extra per build)
travian queue run plan.yaml --use-video

# Custom poll interval
travian queue run plan.yaml --poll 60

# With verbose output (show resources and cost breakdown)
travian queue run plan.yaml --verbose

# Log all output to a file
travian queue run plan.yaml --log-file build.log

# Combine all options
travian queue run plan.yaml --use-video --poll 60 --verbose --log-file build.log
```

#### How it works

1. Resolves all slots and checks current levels
2. Processes priority 1 items first, then 2, etc.
3. For each item: waits for empty queue → checks resources → starts upgrade
4. **Multi-level chaining**: target 5 means keep upgrading until Lv5
5. **Same priority**: builds whichever has enough resources first
6. **Gold guard**: never spends gold — waits for queue instead of using master builder
7. **Video speedup** (`--use-video`): claims `buildingUpgrade` reward after each build
8. **Expect guard**: skips items where slot doesn't match expected building name

#### Example: Targeted upgrades

Starting state: Clay Pits at Lv2 (slot 3), Lv3 (slot 5), Lv3 (slot 8), Lv6 (slot 12)

```yaml
village: 20031
plan:
  - slot: 3
    expect: Clay Pit
    target: 5
    priority: 1
  - slot: 5
    expect: Clay Pit
    target: 5
    priority: 1
```

Result: 5, 5, 3, 6 — only slots 3 and 5 are upgraded.

## Architecture

```
travian-api/
├── src/travian_api/
│   ├── cli.py                  # CLI (typer)
│   ├── config.py               # Settings (.env + env vars)
│   ├── constants.py            # Game constants
│   ├── exceptions.py           # Error types
│   ├── clients/
│   │   └── http_client.py      # Async HTTP with retry + session management
│   ├── models/
│   │   ├── auth.py             # AuthState, Village
│   │   ├── buildings.py        # Building, Resources, QueueItem, UpgradeResult
│   │   ├── farm_list.py        # FarmList, FarmListSlot, MapTileInfo, LastRaid
│   │   ├── military.py         # TroopSendResult, TargetInfo
│   │   ├── raid_analyzer.py    # AnalysisResult, ReScoutTarget, AnalyzerSettings
│   │   └── reports.py          # ReportListItem, ScoutReportData, BattleReportData
│   ├── services/
│   │   ├── auth_service.py     # Login, JWT, re-auth
│   │   ├── auto_scout_service.py   # Map scanning, tile enrichment, scout dispatch
│   │   ├── building_service.py # Buildings, resources, upgrades
│   │   ├── build_queue_service.py  # Auto-builder engine
│   │   ├── farm_list_service.py    # Farm list GraphQL + REST CRUD + send
│   │   ├── military_service.py # Scout, raid, attack, troop overview
│   │   ├── raid_analyzer_service.py # v2 raid analysis pipeline + scoring
│   │   ├── reports_service.py  # Report fetching + GraphQL batch + village reports
│   │   ├── target_resolver.py  # Coordinate/name resolution
│   │   ├── village_report_cache.py # In-memory TTL cache for village reports
│   │   └── video_reward_service.py # ATG ad simulation
│   ├── parsers/
│   │   ├── html_parser.py      # dorf1/dorf2/build page + troop overview parsing
│   │   └── report_parser.py    # Scout/battle/map-tile report parsing
│   └── utils/
│       ├── checksum.py         # Upgrade checksum handling
│       └── helpers.py          # Utilities
├── tests/
├── .env.example
├── pyproject.toml
└── README.md
```

### Key design decisions

- **Async-first**: All services use `httpx` async. CLI wraps with `asyncio.run()`.
- **Village context via `newdid`**: Travian switches villages by appending `?newdid=<id>` to page URLs. All services pass this through.
- **Gold guard by default**: `upgrade_building()` checks the construction queue. If occupied, it refuses unless `allow_gold=True`.
- **jQuery.param encoding**: ATG video ads require `jQuery.param()` format, not JSON. Custom `_jquery_param()` encoder handles this.
- **Real timing for video rewards**: ATG requires 3-second intervals between ticks. Faster = empty signature.

## Configuration

| Environment Variable | CLI Flag | Description | Default |
|---------------------|----------|-------------|---------|
| `TRAVIAN_BASE_URL` | `--server` | Game server URL | _(prompted)_ |
| `TRAVIAN_USERNAME` | `--username` | Account email | _(prompted)_ |
| `TRAVIAN_PASSWORD` | `--password` | Account password | _(prompted)_ |
| `TRAVIAN_X_VERSION` | — | Game client version | `389` |
| `TRAVIAN_LOG_LEVEL` | — | Logging level | `INFO` |
| `TRAVIAN_TIMEOUT` | — | Request timeout (seconds) | `30` |

## Known Limitations

- **Farm List Send**: Requires Gold Club — the API blocks `farm-list/send` without it. Loop commands exit on this error.
- **Movement Cancellation**: Not implemented (requires UI interaction)
- **Report Deletion**: Bulk operations not implemented
- **Video `buildingUpgrade`**: May be disabled on some accounts (cooldown or server restriction)
- **Single plan per run**: Each `queue run` targets one village. Run multiple plans for multiple villages.
- **Auto-Scout enrichment**: One API call per tile through stealth throttler. Large radius scans are slow by design (stealth).
- **Raid Analyzer**: Scoring optimized for Teuton Clubswingers. Other tribes use the same formula (works but not optimal).

## Disclaimer

Educational purposes only. Respect your server's terms of service. Use responsibly.
