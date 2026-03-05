"""Settings routes and Ollama model discovery / lifecycle."""

import re
import shutil
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx

from aqda.db import get_db, DATA_DIR

router = APIRouter()

ALLOWED_SETTINGS = {
    "ollama_url", "llm_model", "embedding_model", "chunk_size", "chunk_overlap",
    "filename_pattern", "filename_variables", "whisper_model", "color_scheme",
}


class SettingUpdate(BaseModel):
    key: str
    value: str


def _validate_setting(key: str, value: str) -> str | None:
    """Return an error message if the setting value is invalid, else None."""
    if key == "filename_pattern" and value.strip():
        if len(value) > 500:
            return "Regex pattern too long (max 500 characters)"
        try:
            re.compile(value)
        except re.error as e:
            return f"Invalid regex: {e}"
    if key == "chunk_size":
        try:
            v = int(value)
            if v < 100 or v > 10000:
                return "Chunk size must be between 100 and 10000"
        except ValueError:
            return "Chunk size must be a number"
    if key == "chunk_overlap":
        try:
            v = int(value)
            if v < 0 or v > 500:
                return "Chunk overlap must be between 0 and 500"
        except ValueError:
            return "Chunk overlap must be a number"
    return None


async def _get_ollama_url() -> str:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT value FROM setting WHERE key='ollama_url'")
        row = await cursor.fetchone()
        return row["value"] if row else "http://localhost:11434"
    finally:
        await db.close()


@router.get("")
async def get_settings():
    db = await get_db()
    try:
        cursor = await db.execute("SELECT key, value FROM setting")
        rows = await cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        await db.close()


@router.put("")
async def update_settings(items: list[SettingUpdate]):
    errors = {}
    for item in items:
        err = _validate_setting(item.key, item.value)
        if err:
            errors[item.key] = err
    if errors:
        raise HTTPException(422, detail=errors)

    db = await get_db()
    try:
        for item in items:
            await db.execute(
                "INSERT OR REPLACE INTO setting (key, value) VALUES (?, ?)",
                (item.key, item.value),
            )
        await db.commit()
        cursor = await db.execute("SELECT key, value FROM setting")
        rows = await cursor.fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        await db.close()


@router.get("/data-dir")
async def get_data_dir():
    """Return the current data directory path."""
    return {"path": str(DATA_DIR), "db_file": str(DATA_DIR / "aqda.db")}


@router.get("/ollama/models")
async def list_ollama_models():
    """Discover available models from the local Ollama instance."""
    ollama_url = await _get_ollama_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return {"available": True, "models": models}
    except Exception:
        return {"available": False, "models": []}


@router.get("/ollama/status")
async def ollama_status():
    """Check if Ollama is running and reachable."""
    ollama_url = await _get_ollama_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(ollama_url)
            return {"running": resp.status_code == 200}
    except Exception:
        return {"running": False}


@router.post("/ollama/start")
async def start_ollama():
    """Attempt to start Ollama in the background."""
    ollama_path = shutil.which("ollama")
    if not ollama_path:
        raise HTTPException(404, "Ollama binary not found on PATH. Install from ollama.com/download")

    # Check if already running
    ollama_url = await _get_ollama_url()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(ollama_url)
            if resp.status_code == 200:
                return {"started": False, "message": "Ollama is already running"}
    except Exception:
        pass

    try:
        subprocess.Popen(
            [ollama_path, "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return {"started": True, "message": "Ollama started"}
    except Exception as e:
        raise HTTPException(500, f"Failed to start Ollama: {e}")
