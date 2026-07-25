"""FastAPI application — serves both API and the frontend SPA."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from aqda import __version__
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


app = FastAPI(title="AQDA", version=__version__, lifespan=lifespan)

_LOCAL_BROWSER_ORIGINS = {
    "http://127.0.0.1:8765",
    "http://localhost:8765",
    "http://[::1]:8765",
    # The Vite development server proxies /api requests to the same local app.
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://[::1]:5173",
}
_LOCAL_BROWSER_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _hostname_from_host_header(value: str) -> str:
    """Return a normalized hostname while rejecting malformed port syntax."""
    host = value.strip().lower()
    if host.startswith("["):
        closing = host.find("]")
        suffix = host[closing + 1:] if closing >= 0 else ""
        if closing < 0 or (
            suffix and not (suffix.startswith(":") and suffix[1:].isdigit())
        ):
            return ""
        return host[1:closing]
    if host.count(":") == 1:
        hostname, port = host.rsplit(":", 1)
        if not port.isdigit():
            return ""
        return hostname
    return host if ":" not in host else ""


@app.middleware("http")
async def require_local_http_context(request, call_next):
    """Reject DNS-rebinding reads and browser-driven cross-site writes.

    Every request must retain a localhost Host header. Origin-less local requests
    remain available to the command line and native folder picker. Browsers attach
    an Origin to cross-site POST/PUT/PATCH/DELETE requests, which prevents an
    arbitrary website from closing AQDA or changing its data.
    """
    hostname = _hostname_from_host_header(request.headers.get("host", ""))
    if hostname not in _LOCAL_BROWSER_HOSTS:
        return JSONResponse(
            {"detail": "AQDA only accepts requests addressed to localhost"},
            status_code=400,
        )
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
