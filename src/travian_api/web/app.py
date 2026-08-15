"""FastAPI application — mounts API routes + serves built React frontend."""

import asyncio
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Import DB models so init_db() creates their tables
from travian_api.web.models import farm_builder as _fb_models  # noqa: F401
from travian_api.web.models.db import init_db
from travian_api.web.routes.buildings import router as buildings_router
from travian_api.web.routes.captcha import router as captcha_router
from travian_api.web.routes.distribution import router as distribution_router
from travian_api.web.routes.exec_sessions import router as exec_sessions_router
from travian_api.web.routes.farm import router as farm_router
from travian_api.web.routes.farm_builder import router as farm_builder_router
from travian_api.web.routes.military import router as military_router
from travian_api.web.routes.queue import router as queue_router
from travian_api.web.routes.recon import router as recon_router
from travian_api.web.routes.reports import router as reports_router
from travian_api.web.routes.scout import router as scout_router
from travian_api.web.routes.status_export import router as status_export_router
from travian_api.web.routes.travian_auth import router as travian_auth_router

# Import all route routers
from travian_api.web.routes.users import router as users_router
from travian_api.web.routes.video import router as video_router
from travian_api.web.routes.villages import router as villages_router
from travian_api.web.sessions import session_manager
from travian_api.web.ws.analyzer_ws import router as analyzer_ws_router
from travian_api.web.ws.farm_builder import router as farm_builder_ws_router

# Import WebSocket routers
from travian_api.web.ws.farm_ws import router as farm_ws_router
from travian_api.web.ws.logs_ws import router as logs_ws_router
from travian_api.web.ws.oasis_raider import router as oasis_raider_ws_router
from travian_api.web.ws.queue_ws import router as queue_ws_router
from travian_api.web.ws.scout_ws import router as scout_ws_router

logger = logging.getLogger(__name__)

# Path to built React static files
STATIC_DIR = Path(__file__).parent / "static"


def ui_build_exists(static_dir: Path) -> bool:
    """True only when a complete frontend build is present.

    The directory alone is not proof: loose files (favicon.svg) can exist
    without a build, and mounting StaticFiles on a missing ``assets/`` raises
    at import — which would crash the server instead of letting the
    unbuilt-UI fallback explain the situation.
    """
    return (static_dir / "assets").is_dir() and (static_dir / "index.html").is_file()


def _quiet_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Suppress noisy Windows ProactorEventLoop socket teardown errors.

    On Windows, when a WebSocket or HTTP connection is closed by the client,
    the ProactorEventLoop tries to call shutdown(SHUT_RDWR) on the already-dead
    socket, producing a ConnectionResetError (WinError 10054).  This is harmless
    cleanup noise — the connection was already closed.  We log it at DEBUG
    instead of letting asyncio print a full traceback to the console.
    """
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        logger.debug(
            "Suppressed connection-reset during socket teardown: %s", context.get("message", "")
        )
        return
    # Fall back to the default handler for everything else
    loop.default_exception_handler(context)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: init DB on startup, disconnect sessions on shutdown."""
    # Attach the log broadcast handler so server logs stream to web UI
    from travian_api.logging_config import setup_logging

    setup_logging(attach_broadcast=True)

    # Suppress Windows ProactorEventLoop socket teardown noise
    if sys.platform == "win32":
        asyncio.get_event_loop().set_exception_handler(_quiet_exception_handler)

    await init_db()
    logger.info("Database initialized")

    # Apply stored recon credentials so a rotation survives a restart. Absent a
    # stored row the manager keeps using the .env values.
    try:
        from travian_api.web.models.db import async_session_factory
        from travian_api.web.routes.recon import load_stored_credentials

        async with async_session_factory() as db:
            if await load_stored_credentials(db):
                logger.info("Loaded stored background-account credentials")
    except Exception:
        logger.exception("Could not load stored recon credentials; using environment")

    from travian_api.debug_dump import debug_dumper
    from travian_api.web.execution_sessions import exec_session_manager

    exec_session_manager.start_cleanup()
    debug_dumper.start_cleanup()

    yield

    debug_dumper.stop_cleanup()
    exec_session_manager.stop_cleanup()
    await session_manager.disconnect_all()
    # Close any recon-proxy HttpClients (curl_cffi / httpx connection
    # pools leak across hot-reloads without this).
    try:
        from travian_api.services.recon_account import recon_account_manager

        await recon_account_manager.shutdown()
    except Exception:
        logger.exception("recon_account_manager.shutdown failed")
    logger.info("All sessions disconnected")


app = FastAPI(
    title="Travian Auto Player — Web UI",
    version="1.0.0",
    lifespan=lifespan,
)


# Correlation ID middleware — tags each request with a unique ID for tracing
class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex[:12])
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response


# Security headers middleware — skip restrictive CSP in development
_DEV_MODE = os.environ.get("TRAVIAN_DEV", "").lower() in ("1", "true", "yes")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        if _DEV_MODE:
            # Relaxed CSP for Vite HMR and dev tools
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "connect-src 'self' ws: wss: http://localhost:* ws://localhost:* http://100.103.184.128:* ws://100.103.184.128:*; "
                "img-src 'self' data:; "
                "frame-ancestors 'none'"
            )
        else:
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; "
                "connect-src 'self' ws: wss:; "
                "img-src 'self' data:; "
                "frame-ancestors 'none'"
            )
        return response


# CORS for development (Vite dev server runs on port 5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000", "http://localhost:8001"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(CorrelationIdMiddleware)

# Mount API routes
app.include_router(users_router)
app.include_router(travian_auth_router)
app.include_router(villages_router)
app.include_router(buildings_router)
app.include_router(military_router)
app.include_router(reports_router)
app.include_router(video_router)
app.include_router(farm_router)
app.include_router(scout_router)
app.include_router(queue_router)
app.include_router(status_export_router)
app.include_router(captcha_router)
app.include_router(exec_sessions_router)
app.include_router(farm_builder_router)
app.include_router(recon_router)
app.include_router(distribution_router)

# Mount WebSocket routes
app.include_router(farm_ws_router)
app.include_router(scout_ws_router)
app.include_router(queue_ws_router)
app.include_router(logs_ws_router)
app.include_router(analyzer_ws_router)
app.include_router(oasis_raider_ws_router)
app.include_router(farm_builder_ws_router)


async def serve_ui_not_built(request: Request, full_path: str) -> JSONResponse:
    """Registered instead of the SPA when no frontend build exists.

    `pip install travian-api[web]` alone ships no static assets, so without
    this the advertised `travian-web` command serves a blank 404 at `/` and
    nothing explains why.
    """
    if full_path.startswith(("api/", "ws/")) or full_path in ("api", "ws"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return JSONResponse(
        {
            "detail": (
                "The web UI has not been built. From a source checkout, run "
                "`npm run build` in frontend/ (or use start.bat / start.sh). "
                "From a pip install, reinstall a wheel that bundles the UI — "
                "one built with Node available, e.g. `pip install --force-reinstall "
                "'travian-api[web]'`. Then restart the server."
            )
        },
        status_code=503,
    )


# Serve static frontend files if a complete build exists
if ui_build_exists(STATIC_DIR):
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    # Catch-all route: serve index.html for any non-API path (SPA routing).
    #
    # index.html MUST NOT be browser-cached without revalidation. Vite emits
    # asset filenames with content hashes (e.g. ``index-BXYj2OuC.js``) so the
    # hashed chunks themselves are immutable and safely cacheable forever —
    # but index.html is the pointer that tells the browser WHICH hash to
    # fetch. If the browser caches index.html aggressively, a backend deploy
    # leaves running tabs pinned to the old chunk hashes and the user sees
    # "I rebuilt and the fix isn't showing up." Force revalidation per
    # request so a refresh / new tab picks up the new index.
    _SPA_NO_CACHE_HEADERS = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    }

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        # Don't intercept API or WebSocket paths (including the bare roots).
        if full_path.startswith(("api/", "ws/")) or full_path in ("api", "ws"):
            return JSONResponse({"detail": "Not found"}, status_code=404)

        # Resolve and verify containment BEFORE serving: a raw request like
        # /../../.env would otherwise escape the build directory and download
        # any readable file on disk, .env and key files included.
        file_path = (STATIC_DIR / full_path).resolve()
        if not file_path.is_relative_to(STATIC_DIR.resolve()):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        if file_path.is_file():
            # Loose static files at the SPA root (favicon.svg, robots.txt,
            # etc.). The content-hashed assets live under /assets via the
            # mount above, so anything reaching this branch is rare and
            # no-cache is the safer default.
            return FileResponse(file_path, headers=_SPA_NO_CACHE_HEADERS)
        # SPA fallback — always serve a fresh index.html (see note above).
        return FileResponse(
            STATIC_DIR / "index.html",
            headers=_SPA_NO_CACHE_HEADERS,
        )
else:
    app.get("/{full_path:path}")(serve_ui_not_built)


def main():
    """CLI entry point for ``travian-web`` command."""
    import uvicorn

    uvicorn.run("travian_api.web.app:app", host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
