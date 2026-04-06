"""FastAPI application — mounts API routes + serves built React frontend."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from travian_api.web.models.db import init_db
from travian_api.web.sessions import session_manager

# Import all route routers
from travian_api.web.routes.users import router as users_router
from travian_api.web.routes.travian_auth import router as travian_auth_router
from travian_api.web.routes.villages import router as villages_router
from travian_api.web.routes.buildings import router as buildings_router
from travian_api.web.routes.military import router as military_router
from travian_api.web.routes.reports import router as reports_router
from travian_api.web.routes.video import router as video_router
from travian_api.web.routes.farm import router as farm_router
from travian_api.web.routes.scout import router as scout_router
from travian_api.web.routes.queue import router as queue_router

# Import WebSocket routers
from travian_api.web.ws.farm_ws import router as farm_ws_router
from travian_api.web.ws.scout_ws import router as scout_ws_router
from travian_api.web.ws.queue_ws import router as queue_ws_router

logger = logging.getLogger(__name__)

# Path to built React static files
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: init DB on startup, disconnect sessions on shutdown."""
    await init_db()
    logger.info("Database initialized")
    yield
    await session_manager.disconnect_all()
    logger.info("All sessions disconnected")


app = FastAPI(
    title="Travian Auto Player — Web UI",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for development (Vite dev server runs on port 5173)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

# Mount WebSocket routes
app.include_router(farm_ws_router)
app.include_router(scout_ws_router)
app.include_router(queue_ws_router)

# Serve static frontend files if the build directory exists
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    # Catch-all route: serve index.html for any non-API path (SPA routing)
    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        # Don't intercept API or WebSocket paths
        if full_path.startswith(("api/", "ws/")):
            return  # Let FastAPI handle 404

        file_path = STATIC_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(STATIC_DIR / "index.html")


def main():
    """CLI entry point for ``travian-web`` command."""
    import uvicorn

    uvicorn.run("travian_api.web.app:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
