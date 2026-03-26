# Travian API

A comprehensive Python library and CLI for automating Travian Legends gameplay. This library provides a clean, async-first API for interacting with Travian servers, including building management, military operations, and report analysis.

## Features

- **🔐 Authentication**: Secure 2-step login with JWT token caching
- **🏗️ Building Management**: List, upgrade, and monitor village buildings
- **⚔️ Military Operations**: Send scouts, raids, and attacks
- **📊 Report Analysis**: Fetch and parse battle/scout reports  
- **🎯 Target Resolution**: Resolve coordinates and village names
- **🔧 CLI Interface**: Easy-to-use command line tools
- **⚡ Async Support**: Built with modern async/await patterns
- **🛡️ Error Handling**: Comprehensive error handling and retry logic
- **📝 Type Safety**: Full type hints with Pydantic models

## Installation

### From Source

```bash
git clone <repository-url>
cd travian-api
pip install -e ".[dev]"
```

### Dependencies

- Python 3.11+
- httpx (async HTTP)
- pydantic v2 (data models)
- typer (CLI)
- python-dotenv (configuration)
- beautifulsoup4 + lxml (HTML parsing)
- tenacity (retry logic)
- rich (CLI formatting)

## Quick Start

### 1. Configuration

Copy the example environment file and configure your server details:

```bash
cp .env.example .env
```

Edit `.env` with your server and credentials:

```env
BASE_URL=https://your-server.travian.com
USERNAME=your-username@example.com
PASSWORD=your-password
X_VERSION=389
```

### 2. CLI Usage

#### Authentication

```bash
# Login to your Travian server
travian auth login

# Check authentication status
travian auth status
```

#### Building Management

```bash
# List all buildings in your village
travian building list

# Show current resources
travian building resources

# Show construction queue
travian building queue

# Upgrade a building by slot ID (refuses if queue occupied — gold guard)
travian building upgrade --slot-id 15

# Allow gold spend (master builder) if queue is occupied
travian building upgrade --slot-id 15 --allow-gold
```

#### Military Operations

```bash
# Send scouts to coordinates
travian military scout --x 100 --y 200 --amount 5

# Scout type: resources (default) or defenses
travian military scout --x 100 --y 200 --amount 3 --type defenses

# Send a raid with specific troops
travian military raid --x 50 --y -30 --troop t1=10 --troop t2=5
```

#### Reports

```bash
# List recent reports (last 24 hours)
travian reports list --max-age-hours 24

# Show detailed report content
travian reports show <report-id>
```

#### Video Rewards

```bash
# Check which video rewards are available
travian video available

# Claim a specific production boost (~33 seconds)
travian video claim ironProductionBonus

# Claim all available production boosts in sequence
travian video claim-all

# Claim building upgrade speedup (requires IDs)
travian video claim buildingUpgrade --village-id 75483 --slot-id 3 --building-id 8
```

Available reward types: `lumberProductionBonus`, `clayProductionBonus`, `ironProductionBonus`, `cropProductionBonus`, `buildingUpgrade`

#### Auto-Builder (Build Queue)

The build queue system lets you define a build plan in YAML and execute it automatically. It chains upgrades, waits for resources, respects construction queues, and never spends gold.

**Step 1: Find your building slots**

```bash
travian building list
```

Output:
```
┌─────────────────────────────────────┐
│ Slot │ Name       │ GID │ Level    │
├──────┼────────────┼─────┼──────────┤
│    3 │ Clay Pit   │   8 │       2  │
│    5 │ Clay Pit   │   8 │       3  │
│    8 │ Clay Pit   │   8 │       3  │
│   12 │ Clay Pit   │   8 │       6  │
│   19 │ Cranny     │  23 │       1  │
│   ...│            │     │          │
└──────┴────────────┴─────┴──────────┘
```

**Step 2: Create your plan file (`plan.yaml`)**

```yaml
village: 75483
plan:
  # --- Priority 1: upgrade first ---
  
  # Upgrade Clay Pit at slot 3 from Lv2 → Lv5 (chains: 2→3→4→5)
  - slot: 3
    target: 5
    priority: 1

  # Upgrade Clay Pit at slot 5 from Lv3 → Lv5 (chains: 3→4→5)
  - slot: 5
    target: 5
    priority: 1

  # --- Priority 2: upgrade after all P1 items are done ---
  
  # Unique buildings can use name instead of slot
  - building: Cranny
    target: 5
    priority: 2

  # --- Priority 3: low priority ---
  
  - building: Residence
    target: 10
    priority: 3
```

**Plan YAML format:**

| Field | Required | Description |
|-------|----------|-------------|
| `village` | Yes | Village ID (find it in the game URL or `travian auth login` output) |
| `plan` | Yes | List of build items |

**Per-item fields:**

| Field | Required | Description |
|-------|----------|-------------|
| `slot` | One of `slot` or `building` | Slot ID (1-40). **Use for resource fields** — when you have multiple of the same building (4 Clay Pits, 6 Croplands, etc.) |
| `building` | One of `slot` or `building` | Building name (partial, case-insensitive). Use for unique buildings like Cranny, Residence, Barracks. If multiple match, picks the lowest level one below target. |
| `target` | Yes | Target level. The builder will chain all upgrades needed (e.g. Lv2 → Lv5 = three upgrades) |
| `priority` | No (default: 5) | 1 = build first, 5 = build last. Same priority items: whichever has resources first |

**Step 3: Validate your plan**

```bash
travian queue validate plan.yaml
```

This shows resolved slots, current levels, and whether anything is already done or missing.

**Step 4: Run it**

```bash
# Dry run — show what would happen without building
travian queue run plan.yaml --dry-run

# Run for real
travian queue run plan.yaml

# Run with video speedup after each build (~33s extra per upgrade)
travian queue run plan.yaml --use-video

# Custom poll interval (check resources every 60s instead of 30s)
travian queue run plan.yaml --poll 60
```

**How it works:**

1. Resolves all slots and checks current levels
2. Processes priority 1 items first, then 2, etc.
3. For each item: waits for empty construction queue → checks resources → starts upgrade
4. **Multi-level chaining**: if Clay Pit is Lv2 and target is 5, it upgrades 2→3, waits for completion, 3→4, waits, 4→5 — automatically
5. Same-priority items: builds whichever has enough resources first
6. **Gold guard**: never spends gold. If the queue is occupied, it waits instead of using the master builder
7. **Video speedup** (`--use-video`): after each upgrade starts, claims the `buildingUpgrade` video reward to cut construction time

**Example: End up with Clay Pits at 5, 5, 3, 6**

Starting state: Clay Pits at Lv2 (slot 3), Lv3 (slot 5), Lv3 (slot 8), Lv6 (slot 12)

```yaml
village: 75483
plan:
  - slot: 3
    target: 5
    priority: 1
  - slot: 5
    target: 5
    priority: 1
```

Result: slots 3 and 5 get upgraded to Lv5. Slots 8 and 12 are untouched.

### 3. Library Usage

```python
import asyncio
from travian_api import TravianClient
from travian_api.services import AuthService, BuildingService, MilitaryService

async def main():
    async with TravianClient() as client:
        # Authenticate
        auth_service = AuthService(client)
        await auth_service.login()
        
        # Get village buildings
        building_service = BuildingService(client)
        village = await building_service.get_village_buildings()
        
        print(f"Village: {village.village_name}")
        print(f"Resources: {village.resources.wood}W {village.resources.clay}C")
        
        # Send scouts
        from travian_api.services import TargetResolver
        from travian_api.models.military import ScoutRequest
        from travian_api.models.common import Coordinates
        
        target_resolver = TargetResolver(client)
        military_service = MilitaryService(client, target_resolver)
        
        scout_request = ScoutRequest(
            target=Coordinates(x=100, y=200),
            scout_count=3
        )
        
        result = await military_service.send_scouts(scout_request)
        print(f"Scout mission: {result.message}")

asyncio.run(main())
```

## Architecture

### Core Components

- **`TravianClient`**: HTTP client with session management and retry logic
- **Services**: High-level business logic (auth, buildings, military, reports)
- **Parsers**: HTML parsing for game data extraction
- **Models**: Pydantic data models for type safety
- **CLI**: User-friendly command line interface

### Data Flow

1. **Configuration**: Settings loaded from `.env` file
2. **Authentication**: 2-step login process with JWT caching
3. **HTTP Requests**: Async requests with automatic retry and session management
4. **HTML Parsing**: Extract game data from server responses
5. **Data Models**: Validate and structure data using Pydantic
6. **Business Logic**: Services coordinate operations and handle errors

## Protocol Implementation

This library implements the complete Travian Legends protocol including:

### Authentication (2-step)
1. POST `/api/v1/auth/login` with credentials
2. GET redirect URL to obtain JWT token
3. Use JWT in Cookie header for subsequent requests

### Building Operations
- Parse buildings from `dorf1.php` (resource fields) and `dorf2.php` (village buildings)
- Extract checksums for secure building upgrades
- Handle construction queues and resource management

### Military Operations
- 2-step form submission for troop dispatches
- Support for scouts, raids, and attacks
- Parse confirmation pages and extract hidden form fields
- Handle different tribe unit types (Romans, Teutons, Gauls)

### Report Processing
- HTML parsing of report lists and individual reports
- Support for scout reports, battle reports, and trade reports
- GraphQL API for batch metadata fetching
- Unicode text cleaning for proper data extraction

## Security & Best Practices

- **No Gold Usage**: Designed to avoid spending premium currency
- **Rate Limiting**: Built-in delays and retry logic to avoid server overload
- **Session Management**: Automatic re-authentication on session expiry
- **Error Handling**: Comprehensive error types and recovery mechanisms
- **Logging**: Detailed logging with sensitive data filtering

## Configuration Options

| Variable | Description | Default |
|----------|-------------|---------|
| `BASE_URL` | Travian server URL | Required |
| `USERNAME` | Your username/email | Required |
| `PASSWORD` | Your password | Required |
| `X_VERSION` | Server version number | 389 |
| `DEBUG` | Enable debug mode | false |
| `LOG_LEVEL` | Logging level | INFO |
| `JWT_CACHE_FILE` | JWT cache location | .jwt_cache.json |

## Development

### Running Tests

```bash
# Install development dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Run with coverage
pytest --cov=travian_api

# Type checking
mypy src/

# Code formatting
black src/ tests/
isort src/ tests/
```

### Project Structure

```
travian-api/
├── src/travian_api/
│   ├── __init__.py           # Package initialization
│   ├── config.py             # Configuration management
│   ├── constants.py          # Game constants and enums
│   ├── exceptions.py         # Custom exceptions
│   ├── logging_config.py     # Logging setup
│   ├── cli.py               # Command line interface
│   ├── clients/             # HTTP client
│   ├── models/              # Pydantic data models
│   ├── services/            # Business logic
│   ├── parsers/             # HTML parsing
│   └── utils/               # Utility functions
├── tests/                   # Test suite
├── .env.example            # Environment template
├── pyproject.toml          # Project configuration
└── README.md               # This file
```

## Known Limitations

- **Movement Cancellation**: Troop movement cancellation requires UI interaction (not implemented)  
- **Report Deletion**: Bulk report operations need form submission handling (not implemented)
- **Video Reward Availability**: `buildingUpgrade` reward may be disabled on some accounts (cooldown or server restriction)

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes with tests
4. Run the test suite
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

This tool is for educational purposes. Please respect the terms of service of your Travian server and use responsibly. The authors are not responsible for any consequences of using this software.

## Support

For questions and support, please open an issue on the repository.