# Travian Auto Player

A Python library, CLI, and self-hosted web UI for automating Travian Legends gameplay. Async-first, multi-village, with stealth anti-bot protection.

## Quick Start (Web UI)

### Prerequisites

- **Python 3.11+** — [python.org/downloads](https://www.python.org/downloads/)
- **Node.js 20.19+ or 22.12+** — [nodejs.org](https://nodejs.org/) (the frontend build uses Vite 8, which needs exactly this range; the startup scripts enforce it)

### Setup & Run

```bash
git clone https://github.com/tomer60300/travian-auto-player.git
cd travian-auto-player
git checkout feature/web-ui
```

Then one command:

| OS | Command |
|----|---------|
| **Windows** | Double-click `start.bat` |
| **Linux / Mac** | `./start.sh` |

The script installs all dependencies, builds the frontend, and starts the server.

### First-Time Usage

1. Open **http://localhost:8001** in your browser
2. **Register** — create a username and password (this is your local web UI account, not Travian)
3. **Connect** — enter your Travian server URL (e.g. `https://ts2.x1.europe.travian.com`) and your Travian login credentials
4. You're in — use Dashboard, Farm Lists, Auto Scout, Build Queue, and more

### Access from Phone / Another Device

The server binds to `0.0.0.0:8001`, so it's already accessible on your local network at `http://<your-lan-ip>:8001`.

For access outside your network, use **Tailscale** (free, no port forwarding needed):

1. Install Tailscale on your server machine and your phone/laptop — [tailscale.com/download](https://tailscale.com/download)
2. Sign in on both devices with the same account
3. Find your server's Tailscale IP: `tailscale ip`
4. Access from any device: `http://<tailscale-ip>:8001`

### Development Mode (hot-reload)

```bash
# Terminal 1 — Backend with relaxed CSP
TRAVIAN_DEV=1 python -m uvicorn travian_api.web.app:app --host 0.0.0.0 --port 8000 --reload

# Terminal 2 — Frontend Vite dev server
cd frontend && npm install && npm run dev
```

Frontend dev server runs on `:5173` and proxies API calls to the backend.

---

## Features

### Web UI (`travian-web`)
- **Self-Hosted Dashboard** — React + FastAPI served at `http://localhost:8001`
- **Multi-User** — SQLite auth with per-user Travian session isolation
- **Buildings** — View, upgrade, construct with live construction queue countdown
- **Farm Lists** — Full management with sort/filter, booty display (taken/capacity), copy/move between lists, defense scan with combat strength, active/inactive sync
- **Auto-Scout** — Map scan with alliance/player exclusion (persisted), population filters (including real player population from profile pages), loop mode with countdown
- **Military** — Scout and raid dispatch with tribe-aware troop names
- **Reports** — Browse reports with collapsible raid analyzer panel
- **Build Queue** — Visual plan builder with drag-and-drop, validation, and live execution via WebSocket
- **Video Rewards** — Claim individual or all production boosts
- **Activity Log** — Real-time server + client log streaming via WebSocket
- **Captcha Guard** — Automatic bot-detection with full-screen alert, operation freeze, and user-guided resolution
- **Stealth** — Request throttling, human-like delays, browser header simulation, noise injection, activity scheduling

### CLI
- **Authentication** — 2-step login with JWT caching, interactive setup prompt
- **Multi-Village** — Full support for multiple villages per account
- **Auto-Builder** — YAML-based build queue with priorities, multi-level chaining, gold guard, and video speedup
- **Military** — Scouts, raids, attacks with tribe-aware troop selection
- **Farm Lists** — Full CRUD + smart raid intelligence (last raid, carry ratio, distance, booty)
- **Auto-Scout** — Scan map, filter by population/distance/player/alliance, send scouts with loop mode
- **Reports** — Fetch and parse scout/battle reports with smart type detection
- **Raid Analyzer v2** — Scout-gated pipeline with binary search scoring, cache, and re-scout queue
- **Video Rewards** — Automated ATG ad simulation for production boosts and build speedups
- **Gold Guard** — Never spends gold unless you explicitly opt in

---

## CLI Quick Start

### Install (CLI only)

```bash
pip install -e .
travian-setup
```

`travian-setup` checks if the `travian` command is on your PATH. If not, it offers to add it automatically. You only need to run this once.

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

OK - Logged in!
  Player: Chieftain
  Tribe: 2 (Teuton)
  Villages (2):
    20030  Main Village  (-160|168)  (main)
    20031  New village   (-161|167)
```

### Configuration

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

---

## CLI Reference

### Authentication

```bash
travian auth login          # Login and show player info
travian auth token          # Print the current JWT token
```

### Villages

```bash
travian village list        # List all your villages
travian village switch 20031  # Switch active village context
```

### Buildings

All building commands accept `--village-id` / `-v` to target a specific village:

```bash
travian building list                          # List all buildings
travian building list -v 20031                 # List for specific village
travian building resources                     # Show current resources
travian building queue                         # Show construction queue
travian building upgrade --slot-id 15          # Upgrade a building
travian building upgrade --slot-id 15 --allow-gold  # Allow gold spend
travian building construct --slot-id 25 --building Cranny  # New building
```

### Military

```bash
travian military scout --x 100 --y 200 --amount 5
travian military scout --x 100 --y 200 --amount 3 --type defenses
travian military raid --x 50 --y -30 --troop t1=10 --troop t2=5
```

### Farm Lists

```bash
travian farm list                              # List all farm lists
travian farm show 10165                        # Show list with raid intelligence
travian farm send 10165                        # Send a farm list
travian farm send-all --yes                    # Send all farm lists
travian farm run 10165                         # Loop-send every 5 minutes
travian farm run 10165 --interval 180 --duration 120  # Custom interval/duration
travian farm create --name "New List"          # Create farm list
travian farm add-target 10165 --x -162 --y 167 -t t1=5  # Add target
travian farm delete 10165                      # Delete farm list
```

### Auto-Scout

```bash
travian scout scan --radius 10                 # Scan and preview targets
travian scout scan --radius 15 --max-pop 50    # Filter by population
travian scout auto --radius 10 --max-pop 50 --amount 1 --type resources --yes  # Scan + send
travian scout auto --radius 15 --max-pop 100 --dry-run  # Dry run
```

### Reports

```bash
travian reports list                           # Recent reports
travian reports show <report-id>               # Report details
travian reports village 14 98                  # All reports for a village
travian reports analyze --radius 15            # Raid analyzer v2
```

### Video Rewards

```bash
travian video available                        # Check availability
travian video claim ironProductionBonus         # Claim boost
travian video claim-all                        # Claim all boosts
```

### Auto-Builder (Build Queue)

```bash
travian queue validate plan.yaml               # Validate plan
travian queue run plan.yaml                    # Execute plan
travian queue run plan.yaml --use-video --verbose  # With video speedup
```

**Example `plan.yaml`:**

```yaml
village: 20031
plan:
  - slot: 3
    expect: Clay Pit
    target: 5
    priority: 1
  - building: Cranny
    target: 5
    priority: 2
```

---

## Architecture

```
travian-auto-player/
├── src/travian_api/
│   ├── cli.py                  # CLI (typer)
│   ├── config.py               # Settings (.env + env vars)
│   ├── clients/
│   │   └── http_client.py      # Async HTTP with retry + stealth
│   ├── models/                 # Pydantic models
│   ├── services/               # Business logic (auth, buildings, farm, scout, military, reports)
│   ├── parsers/                # HTML + report parsing
│   ├── stealth/                # Anti-bot: throttler, human delays, headers, noise, captcha guard
│   └── web/
│       ├── app.py              # FastAPI app + middleware
│       ├── routes/             # REST API endpoints
│       ├── ws/                 # WebSocket handlers (farm loop, scout, queue, logs)
│       ├── models/db.py        # SQLite models (User, TravianCredential)
│       └── sessions.py         # Per-user Travian session isolation
├── frontend/
│   ├── src/
│   │   ├── pages/              # React pages (Dashboard, FarmLists, AutoScout, BuildQueue, etc.)
│   │   ├── components/         # Shared components (Toast, ConfirmDialog, CaptchaAlert, etc.)
│   │   └── stores/             # Zustand state management
│   └── package.json
├── start.bat                   # One-click Windows startup
├── start.sh                    # One-click Linux/Mac startup
├── pyproject.toml              # Python package config
└── CHANGELOG.md
```

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `TRAVIAN_BASE_URL` | _(prompted)_ | Game server URL |
| `TRAVIAN_USERNAME` | _(prompted)_ | Account email |
| `TRAVIAN_PASSWORD` | _(prompted)_ | Account password |
| `TRAVIAN_STEALTH` | `true` | Enable stealth anti-bot mode |
| `TRAVIAN_STEALTH_SPEED` | `1.0` | Delay multiplier (0.5=fast, 2.0=cautious) |
| `TRAVIAN_DEV` | `false` | Relaxed CSP for Vite dev server |
| `TRAVIAN_LOG_LEVEL` | `INFO` | Logging level |
| `TRAVIAN_TIMEOUT` | `30` | Request timeout (seconds) |

## Documentation

In-depth docs live under `docs/`:

| Topic | Doc |
|---|---|
| Travian REST API | [`docs/03-rest-api.md`](docs/03-rest-api.md) |
| Travian GraphQL API | [`docs/04-graphql-api.md`](docs/04-graphql-api.md) |
| Map system | [`docs/05-map-system.md`](docs/05-map-system.md) |
| Authentication | [`docs/19-authentication-full.md`](docs/19-authentication-full.md) |
| Buildings & resources | [`docs/16-buildings-resources.md`](docs/16-buildings-resources.md) |
| Farm-list API | [`docs/14-farm-list-api.md`](docs/14-farm-list-api.md) |
| Troop sending | [`docs/13-troop-sending.md`](docs/13-troop-sending.md) |
| Multi-village | [`docs/18-multi-village.md`](docs/18-multi-village.md) |
| Reports system | [`docs/12-reports-system.md`](docs/12-reports-system.md) |
| Resource production | [`docs/20-resource-production.md`](docs/20-resource-production.md) |
| Video reward protocol | [`docs/11-video-reward-protocol.md`](docs/11-video-reward-protocol.md) |
| **Stealth / anti-bot system** | [`docs/21-stealth-anti-bot.md`](docs/21-stealth-anti-bot.md) |
| **Resumable cross-device operations** | [`docs/22-resumable-operations.md`](docs/22-resumable-operations.md) |
| **Stealth vs performance — design decisions** | [`docs/23-stealth-decisions.md`](docs/23-stealth-decisions.md) |
| Changelog | [`CHANGELOG.md`](CHANGELOG.md) |

## Known Limitations

- **Farm List Send**: Requires Gold Club
- **Single server process**: For production use, run behind a reverse proxy with HTTPS
- **Auto-Scout enrichment**: One API call per tile through stealth throttler — large radius scans are slow by design
- **Raid Analyzer**: Scoring optimized for Teuton Clubswingers

## Disclaimer

Educational purposes only. Respect your server's terms of service. Use responsibly.
