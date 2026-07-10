"""FastAPI application — serves both API and the frontend SPA."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from aqda.db import init_db
from aqda.routers import projects, documents, codes, codings, memos, settings, ai, export, shared, system
from aqda.services.shared_projects import start_sync_service, stop_sync_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await start_sync_service()
    try:
        yield
    finally:
        await stop_sync_service()


app = FastAPI(title="AQDA", version="0.3.0", lifespan=lifespan)

_LOCAL_BROWSER_ORIGINS = {
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    # The Vite development server proxies /api requests to the same local app.
    "http://127.0.0.1:5173",
    "http://localhost:5173",
}


@app.middleware("http")
async def require_local_origin_for_changes(request, call_next):
    """Reject browser-driven cross-site writes to the localhost API.

    Origin-less requests remain available to the command line and native folder
    picker. Browsers attach an Origin to cross-site POST/PUT/PATCH/DELETE requests,
    which prevents an arbitrary website from closing AQDA or changing its data.
    """
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and origin not in _LOCAL_BROWSER_ORIGINS:
            return JSONResponse(
                {"detail": "Cross-site requests to AQDA are not allowed"},
                status_code=403,
            )
    return await call_next(request)

# API routes
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
app.include_router(codes.router, prefix="/api/codes", tags=["codes"])
app.include_router(codings.router, prefix="/api/codings", tags=["codings"])
app.include_router(memos.router, prefix="/api/memos", tags=["memos"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(export.router, prefix="/api/export", tags=["export"])
app.include_router(shared.router, prefix="/api/shared", tags=["shared"])
app.include_router(system.router, prefix="/api/system", tags=["system"])


@app.api_route(
    "/api/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def api_not_found(path: str):
    return JSONResponse({"detail": f"API route not found: /api/{path}"}, status_code=404)

# Serve frontend
FRONTEND_DIR = Path(__file__).parent / "frontend" / "dist"

if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="static")
    _FRONTEND_ROOT = FRONTEND_DIR.resolve()
    _INDEX = _FRONTEND_ROOT / "index.html"

    @app.get("/{path:path}")
    async def serve_spa(path: str):
        # Serve index.html for all non-API, non-asset routes (SPA routing).
        # Resolve the target and confine it to the frontend dir so crafted
        # paths like "../../db.py" can't escape and serve arbitrary files.
        target = (_FRONTEND_ROOT / path).resolve()
        if target.is_relative_to(_FRONTEND_ROOT) and target.is_file():
            return FileResponse(target)
        return FileResponse(_INDEX)
