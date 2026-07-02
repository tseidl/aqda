"""FastAPI application — serves both API and the frontend SPA."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from aqda.db import init_db
from aqda.routers import projects, documents, codes, codings, memos, settings, ai, export


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="AQDA", version="0.2.0", lifespan=lifespan)

# API routes
app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
app.include_router(documents.router, prefix="/api/documents", tags=["documents"])
app.include_router(codes.router, prefix="/api/codes", tags=["codes"])
app.include_router(codings.router, prefix="/api/codings", tags=["codings"])
app.include_router(memos.router, prefix="/api/memos", tags=["memos"])
app.include_router(settings.router, prefix="/api/settings", tags=["settings"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(export.router, prefix="/api/export", tags=["export"])

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
