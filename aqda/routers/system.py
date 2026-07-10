"""Application lifecycle routes for the local AQDA server."""

import asyncio

from fastapi import APIRouter, Request

router = APIRouter()


@router.post("/shutdown")
async def shutdown_aqda(request: Request):
    """Return first, then ask Uvicorn for a graceful lifespan shutdown."""
    server = getattr(request.app.state, "uvicorn_server", None)
    if server is None:
        return {"closing": False, "message": "Shutdown is managed by the host process"}
    asyncio.get_running_loop().call_later(0.25, setattr, server, "should_exit", True)
    return {
        "closing": True,
        "message": "AQDA is saving shared projects and shutting down safely.",
    }
