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

# List only specific building types (e.g., type 1 = Woodcutter)
travian building list --building-type 1

# Upgrade a building at slot 15
travian building upgrade 15
```

#### Military Operations

```bash
# Send 5 scouts to coordinates (100, 200)
travian military scout "100,200" --scouts 5

# Send raid with specific troops
travian military raid "50,-30" --troops "t1=10,t2=5,t3=2"
```

#### Reports

```bash
# List recent reports (last 24 hours)
travian reports list --max-age 24

# Show detailed report content
travian reports show <report-id>

# Filter by report type
travian reports list --report-type scout
```

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

- **Video Rewards**: Construction speed-up via ads requires complex ATG network simulation (not implemented)
- **Movement Cancellation**: Troop movement cancellation requires UI interaction (not implemented)  
- **Report Deletion**: Bulk report operations need form submission handling (not implemented)
- **Multiple Villages**: Currently focused on single village operations

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