"""FastAPI application — serves both API and the frontend SPA."""

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

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

_LOCAL_BROWSER_HOSTS = {"127.0.0.1", "localhost", "::1"}
# The Vite development server proxies /api requests to the local app.
_DEV_SERVER_PORT = 5173


def _parse_port(text: str) -> int | None:
    """Return a valid TCP port from ASCII digits only; str.isdigit also accepts '²'."""
    if not text or not text.isascii() or not text.isdigit():
        return None
    port = int(text)
    return port if 1 <= port <= 65535 else None


def _split_host_header(value: str) -> tuple[str, int | None] | None:
    """Return (hostname, port) from a Host header, or None when it is malformed."""
    host = value.strip().lower()
    if host.startswith("["):
        closing = host.find("]")
        if closing < 0 or ":" not in host[1:closing]:
            return None
        suffix = host[closing + 1:]
        if not suffix:
            return host[1:closing], None
        port = _parse_port(suffix[1:]) if suffix.startswith(":") else None
        return (host[1:closing], port) if port else None
    if host.count(":") == 1:
        hostname, port_text = host.rsplit(":", 1)
        port = _parse_port(port_text)
        return (hostname, port) if port else None
    return (host, None) if ":" not in host else None


def _is_local_origin(origin: str, request_port: int | None) -> bool:
    """Accept only the origin of this app itself (whatever port it runs on) or the dev server.

    Browsers send an Origin header on every same-origin POST too, so this must
    follow the port chosen with ``aqda --port`` instead of a fixed list.
    """
    try:
        parts = urlsplit(origin.strip().lower())
        origin_port = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or parts.hostname not in _LOCAL_BROWSER_HOSTS:
        return False
    return (origin_port or 80) in {request_port or 80, _DEV_SERVER_PORT}


@app.middleware("http")
async def require_local_http_context(request, call_next):
    """Reject DNS-rebinding reads and browser-driven cross-site requests.

    Every request must retain a localhost Host header. Origin-less local requests
    remain available to the command line and native folder picker. Browsers attach
    an Origin to cross-site POST/PUT/PATCH/DELETE requests, which prevents an
    arbitrary website from closing AQDA or changing its data. They also label every
    cross-site request, including image or iframe loads of GET routes such as the
    snapshot export, with Sec-Fetch-Site, so those are refused for all methods.
    """
    host = _split_host_header(request.headers.get("host", ""))
    if host is None or host[0] not in _LOCAL_BROWSER_HOSTS:
        return JSONResponse(
            {"detail": "AQDA only accepts requests addressed to localhost"},
            status_code=400,
        )
    if request.headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return JSONResponse(
            {"detail": "Cross-site requests to AQDA are not allowed"},
            status_code=403,
        )
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if origin and not _is_local_origin(origin, host[1]):
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
