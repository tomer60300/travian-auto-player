# FastAPI Backend Conventions

- Type hints on ALL function signatures (Python 3.12+ syntax: `str | None` not `Optional[str]`)
- Pydantic v2 models for all request/response schemas
- Async endpoints by default (`async def`), async SQLAlchemy sessions via aiosqlite
- Dependency injection via `Depends()` — never import db sessions directly
- Fernet encryption keys from environment variables, never hardcoded
- WebSocket handlers in dedicated modules under `web/ws/`
- Use structlog or rich for logging, never print()
- HTTP client: httpx for standard requests, curl_cffi for stealth/anti-bot
- HTML parsing: BeautifulSoup4 + lxml, parsers live in `parsers/`
- Retry logic via tenacity decorators
- Caching: diskcache for persistent, cachetools for in-memory TTL
- Config via pydantic-settings, loaded from environment variables
- Database: SQLAlchemy 2.0 async ORM, SQLite via aiosqlite, tables created via init_db()
- No alembic migrations — schema changes go through model definitions in web/models/
- Tests: pytest + pytest-asyncio, colocate in tests/ directory
