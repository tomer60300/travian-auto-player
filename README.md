# Travian API

A Python library and CLI for automating Travian Legends gameplay. Async-first, multi-village, with an auto-builder that chains upgrades from a YAML plan.

## Features

- **🔐 Authentication** — 2-step login with JWT caching, interactive setup prompt
- **🏘️ Multi-Village** — Full support for multiple villages per account
- **🏗️ Auto-Builder** — YAML-based build queue with priorities, multi-level chaining, gold guard, and video speedup
- **⚔️ Military** — Scouts, raids, attacks with village selection
- **📊 Reports** — Fetch and parse scout/battle reports with smart type detection
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
  Player: ToChe
  Tribe: 2 (Teuton)
  Villages (2):
    69130  Main Village  (-160|168)  (main)
    75483  New village   (-161|167)
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

### Villages

```bash
# List all your villages
travian village list

# Switch active village context
travian village switch 75483
```

### Buildings

All building commands accept `--village-id` / `-v` to target a specific village:

```bash
# List all buildings
travian building list
travian building list -v 75483

# Show current resources
travian building resources
travian building resources -v 75483

# Show construction queue
travian building queue

# Upgrade a building (gold guard ON by default)
travian building upgrade --slot-id 15
travian building upgrade --slot-id 15 -v 75483

# Allow gold spend if queue is occupied
travian building upgrade --slot-id 15 --allow-gold
```

### Military

```bash
# Send scouts
travian military scout --x 100 --y 200 --amount 5
travian military scout --x 100 --y 200 --amount 3 --type defenses

# Send from a specific village
travian military scout --x 100 --y 200 --amount 5 --village-id 75483

# Send a raid
travian military raid --x 50 --y -30 --troop t1=10 --troop t2=5
```

### Reports

```bash
# List recent reports
travian reports list --max-age-hours 24

# Show detailed report
travian reports show <report-id>
```

### Video Rewards

```bash
# Check availability
travian video available

# Claim a production boost (~33 seconds)
travian video claim ironProductionBonus

# Claim all available production boosts
travian video claim-all

# Claim building speedup
travian video claim buildingUpgrade --village-id 75483 --slot-id 3 --building-id 8
```

Available types: `lumberProductionBonus`, `clayProductionBonus`, `ironProductionBonus`, `cropProductionBonus`, `buildingUpgrade`

### Auto-Builder (Build Queue)

The main feature. Define what to build in YAML, run one command, walk away.

#### Step 1: Find your slots

```bash
travian building list -v 75483
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
village: 75483
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
village: 75483
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
│   │   ├── military.py         # TroopSendResult, TargetInfo
│   │   └── reports.py          # ReportListItem, ScoutReportData, BattleReportData
│   ├── services/
│   │   ├── auth_service.py     # Login, JWT, re-auth
│   │   ├── building_service.py # Buildings, resources, upgrades
│   │   ├── build_queue_service.py  # Auto-builder engine
│   │   ├── military_service.py # Scout, raid, attack
│   │   ├── reports_service.py  # Report fetching + GraphQL batch
│   │   ├── target_resolver.py  # Coordinate/name resolution
│   │   └── video_reward_service.py # ATG ad simulation
│   ├── parsers/
│   │   ├── html_parser.py      # dorf1/dorf2/build page parsing
│   │   └── report_parser.py    # Scout/battle report parsing
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

- **Movement Cancellation**: Not implemented (requires UI interaction)
- **Report Deletion**: Bulk operations not implemented
- **Video `buildingUpgrade`**: May be disabled on some accounts (cooldown or server restriction)
- **Single plan per run**: Each `queue run` targets one village. Run multiple plans for multiple villages.

## Disclaimer

Educational purposes only. Respect your server's terms of service. Use responsibly.
