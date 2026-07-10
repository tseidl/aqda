"""Shared-project folder configuration and synchronization routes."""

import asyncio
import platform
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import aqda.db as db_module
from aqda.db import get_db
from aqda.services.shared_projects import (
    discover_shared_projects,
    get_shared_root,
    get_shared_roots,
    get_sync_health,
    open_shared_project,
    remove_shared_root,
    resolve_conflict_copy,
    set_shared_root,
    share_project,
    sync_all_shared_projects,
    sync_project,
    unlink_shared_project,
)

router = APIRouter()


class FolderUpdate(BaseModel):
    path: str


class OpenSharedProject(BaseModel):
    folder: str
    local_newer_choice: str | None = None


class ConflictResolution(BaseModel):
    choice: str


class ShareProjectRequest(BaseModel):
    root: str | None = None


def _choose_folder_native() -> str | None:
    """Open the operating system's folder picker when one is available."""
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        result = subprocess.run(
            [
                "osascript",
                "-e",
                'POSIX path of (choose folder with prompt "Choose your AQDA collaboration folder")',
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    elif system == "Windows" and shutil.which("powershell"):
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
            "$d.Description='Choose your AQDA collaboration folder'; "
            "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
    elif shutil.which("zenity"):
        result = subprocess.run(
            ["zenity", "--file-selection", "--directory", "--title=Choose AQDA folder"],
            capture_output=True,
            text=True,
            check=False,
        )
    elif shutil.which("kdialog"):
        result = subprocess.run(
            ["kdialog", "--getexistingdirectory", str(Path.home())],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        return None
    selected = result.stdout.strip()
    return selected or None


@router.get("")
async def shared_status():
    root = await get_shared_root()
    roots = await get_shared_roots()
    db = await get_db()
    try:
        linked = await (
            await db.execute(
                "SELECT id, name, lineage_id, revision, shared_folder, "
                "shared_last_published_revision, shared_last_snapshot_id, "
                "shared_last_sync_at, shared_sync_error FROM project "
                "WHERE shared_folder IS NOT NULL AND deleted_at IS NULL ORDER BY name"
            )
        ).fetchall()
    finally:
        await db.close()
    backup_dir = db_module.DATA_DIR / "backups"
    backups = (
        sorted(backup_dir.glob("*.db"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
        if backup_dir.exists()
        else []
    )
    discovered = await discover_shared_projects()
    root_items = []
    for item in roots:
        available = item.is_dir()
        root_items.append({
            "path": str(item),
            "name": item.name or str(item),
            "available": available,
            "project_count": sum(
                1 for project in discovered if project.get("root") == str(item)
            ),
            "standalone_aqda_count": (
                len(list(item.glob("*.aqda"))) if available else 0
            ),
            "linked_project_count": sum(
                1
                for project in linked
                if Path(project["shared_folder"]).expanduser().resolve().parent == item
            ),
        })
    return {
        "root": str(root) if root else "",
        "roots": root_items,
        "linked": [dict(row) for row in linked],
        "discovered": discovered,
        "standalone_aqda_count": sum(
            item["standalone_aqda_count"] for item in root_items
        ),
        "backup_folder": str(backup_dir),
        "backup_count": len(backups),
        "latest_backup": str(backups[0]) if backups else None,
        **get_sync_health(),
    }


@router.put("/folder")
async def update_shared_folder(data: FolderUpdate):
    root = await set_shared_root(data.path)
    return {"path": str(root), "discovered": await discover_shared_projects()}


@router.delete("/folder", status_code=204)
async def delete_shared_folder(data: FolderUpdate):
    await remove_shared_root(data.path)


@router.post("/folder/pick")
async def pick_shared_folder():
    selected = await asyncio.to_thread(_choose_folder_native)
    if not selected:
        raise HTTPException(
            400,
            "No folder was selected. You can paste the folder path in Settings instead.",
        )
    root = await set_shared_root(selected)
    return {"path": str(root), "discovered": await discover_shared_projects()}


@router.post("/projects/{project_id}/share")
async def enable_project_sharing(
    project_id: int,
    data: ShareProjectRequest | None = None,
):
    return await share_project(project_id, data.root if data else None)


@router.post("/projects/{project_id}/sync")
async def sync_one_project(project_id: int):
    return await sync_project(project_id)


@router.post("/sync")
async def sync_everything():
    return {"projects": await sync_all_shared_projects(force_publish=True)}


@router.post("/open")
async def open_project(data: OpenSharedProject):
    return await open_shared_project(data.folder, data.local_newer_choice)


@router.post("/conflicts/{conflict_project_id}/resolve")
async def resolve_conflict(conflict_project_id: int, data: ConflictResolution):
    return await resolve_conflict_copy(conflict_project_id, data.choice)


@router.delete("/projects/{project_id}/link", status_code=204)
async def unlink_project(project_id: int):
    await unlink_shared_project(project_id)
