"""Automatic shared-folder collaboration using immutable project snapshots."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from aqda.db import get_db

SYNC_INTERVAL_SECONDS = 3
PUBLISH_IDLE_SECONDS = 5


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    lineage_id: str
    head_snapshot_id: str
    revision: int
    name: str
    created_at: str
    created_by: str
    parent_by_id: dict[str, str | None]
    mtime_ns: int


_sync_lock = asyncio.Lock()
_sync_task: asyncio.Task | None = None
_stop_event: asyncio.Event | None = None
_snapshot_cache: dict[Path, tuple[int, int, SnapshotInfo]] = {}


def _inspect_snapshot(path: Path) -> SnapshotInfo:
    """Read and verify snapshot metadata without modifying the shared file."""
    uri = f"file:{quote(str(path))}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("snapshot integrity check failed")
        project = connection.execute(
            "SELECT id, name, lineage_id, revision, head_snapshot_id FROM project "
            "WHERE deleted_at IS NULL ORDER BY id LIMIT 1"
        ).fetchone()
        if not project or not project["lineage_id"] or not project["head_snapshot_id"]:
            raise ValueError("not a collaboration snapshot")
        rows = connection.execute(
            "SELECT snapshot_id, parent_snapshot_id, created_at, created_by "
            "FROM project_snapshot WHERE project_id=?",
            (project["id"],),
        ).fetchall()
        parent_by_id = {row["snapshot_id"]: row["parent_snapshot_id"] for row in rows}
        head = next(
            (row for row in rows if row["snapshot_id"] == project["head_snapshot_id"]),
            None,
        )
        if head is None:
            raise ValueError("snapshot head is missing from its history")
        return SnapshotInfo(
            path=path,
            lineage_id=project["lineage_id"],
            head_snapshot_id=project["head_snapshot_id"],
            revision=int(project["revision"] or 0),
            name=project["name"],
            created_at=head["created_at"] or "",
            created_by=head["created_by"] or "",
            parent_by_id=parent_by_id,
            mtime_ns=path.stat().st_mtime_ns,
        )
    finally:
        connection.close()


async def _snapshot_infos(folder: Path) -> list[SnapshotInfo]:
    snapshot_dir = folder / "snapshots"
    if not snapshot_dir.is_dir():
        return []
    infos: list[SnapshotInfo] = []
    for path in sorted(snapshot_dir.glob("*.aqda")):
        try:
            stat = path.stat()
            cached = _snapshot_cache.get(path)
            if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
                infos.append(cached[2])
                continue
            info = await asyncio.to_thread(_inspect_snapshot, path)
            _snapshot_cache[path] = (stat.st_size, stat.st_mtime_ns, info)
            infos.append(info)
        except (OSError, sqlite3.DatabaseError, ValueError):
            # A cloud provider may briefly expose an incomplete download. It is
            # ignored now and retried on the next sync pass.
            continue
    return infos


def _head_infos(infos: list[SnapshotInfo], lineage_id: str | None = None) -> list[SnapshotInfo]:
    relevant = [item for item in infos if not lineage_id or item.lineage_id == lineage_id]
    by_head: dict[str, SnapshotInfo] = {}
    for item in relevant:
        previous = by_head.get(item.head_snapshot_id)
        if previous is None or item.mtime_ns > previous.mtime_ns:
            by_head[item.head_snapshot_id] = item

    ancestor_ids: set[str] = set()
    for item in by_head.values():
        current = item.parent_by_id.get(item.head_snapshot_id)
        seen: set[str] = set()
        while current and current not in seen:
            seen.add(current)
            ancestor_ids.add(current)
            current = item.parent_by_id.get(current)

    heads = [item for snapshot_id, item in by_head.items() if snapshot_id not in ancestor_ids]
    return sorted(heads, key=lambda item: (item.created_at, item.mtime_ns), reverse=True)


async def get_shared_root() -> Path | None:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT value FROM setting WHERE key='shared_folder'")
        ).fetchone()
        value = (row["value"] if row else "").strip()
        return Path(value).expanduser().resolve() if value else None
    finally:
        await db.close()


async def set_shared_root(path: str) -> Path:
    root = Path(path).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise HTTPException(400, "The collaboration location must be a folder")
    test_path = root / ".aqda-write-test"
    try:
        test_path.write_text("ok", encoding="utf-8")
        test_path.unlink()
    except OSError as exc:
        raise HTTPException(400, f"AQDA cannot write to that folder: {exc}") from exc

    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('shared_folder', ?)",
            (str(root),),
        )
        await db.commit()
    finally:
        await db.close()
    return root


def _folder_component(name: str) -> str:
    cleaned = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "-", name).strip(" .")
    return cleaned[:80] or "AQDA Project"


def _new_project_folder(root: Path, name: str) -> Path:
    base = _folder_component(name)
    candidate = root / f"{base}.aqda-project"
    counter = 2
    while candidate.exists():
        candidate = root / f"{base} ({counter}).aqda-project"
        counter += 1
    (candidate / "snapshots").mkdir(parents=True)
    return candidate


async def _project_row(project_id: int):
    db = await get_db()
    try:
        return await (
            await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        ).fetchone()
    finally:
        await db.close()


async def _update_sync_state(project_id: int, **fields) -> None:
    if not fields:
        return
    db = await get_db()
    try:
        assignments = ", ".join(f"{key}=?" for key in fields)
        await db.execute(
            f"UPDATE project SET {assignments} WHERE id=?",
            [*fields.values(), project_id],
        )
        await db.commit()
    finally:
        await db.close()


async def _device_id() -> str:
    db = await get_db()
    try:
        row = await (
            await db.execute("SELECT value FROM setting WHERE key='device_id'")
        ).fetchone()
        if not row or not row["value"]:
            raise RuntimeError("AQDA installation ID is missing")
        return row["value"]
    finally:
        await db.close()


def _write_snapshot(
    folder: Path,
    writer_id: str,
    snapshot_id: str,
    revision: int,
    data: bytes,
) -> Path:
    snapshot_dir = folder / "snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    safe_writer = re.sub(r"[^A-Za-z0-9_-]", "-", writer_id)
    final_path = snapshot_dir / f"writer-{safe_writer}.aqda"
    temp_path = snapshot_dir / f".{snapshot_id}.{os.getpid()}.tmp"
    try:
        with temp_path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, final_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return final_path


async def _publish_project(project_id: int, force: bool = False) -> dict:
    project = await _project_row(project_id)
    if not project or not project["shared_folder"]:
        raise HTTPException(404, "Project is not linked to a collaboration folder")
    if (
        not force
        and project["shared_last_published_revision"] is not None
        and project["shared_last_published_revision"] == project["revision"]
    ):
        return {"published": False, "revision": project["revision"]}

    from aqda.routers.export import build_aqda_snapshot

    data, exported_project, snapshot_id = await build_aqda_snapshot(project_id)
    folder = Path(project["shared_folder"])
    writer_id = await _device_id()
    await asyncio.to_thread(
        _write_snapshot,
        folder,
        writer_id,
        snapshot_id,
        int(exported_project["revision"]),
        data,
    )
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await _update_sync_state(
        project_id,
        shared_last_published_revision=exported_project["revision"],
        shared_last_snapshot_id=snapshot_id,
        shared_last_sync_at=now,
        shared_sync_error=None,
    )
    return {
        "published": True,
        "revision": exported_project["revision"],
        "snapshot_id": snapshot_id,
        "folder": str(folder),
    }


async def share_project(project_id: int) -> dict:
    async with _sync_lock:
        root = await get_shared_root()
        if root is None:
            raise HTTPException(400, "Choose a collaboration folder in Settings first")
        project = await _project_row(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        folder = Path(project["shared_folder"]) if project["shared_folder"] else None
        if folder is None:
            folder = await asyncio.to_thread(_new_project_folder, root, project["name"])
            await _update_sync_state(
                project_id,
                shared_folder=str(folder),
                shared_last_published_revision=None,
                shared_last_snapshot_id=None,
                shared_sync_error=None,
            )
        result = await _publish_project(project_id, force=True)
        result["project_id"] = project_id
        return result


async def _mark_ignored(project_id: int, snapshot_id: str) -> None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO shared_ignored_head (project_id, snapshot_id) VALUES (?, ?)",
            (project_id, snapshot_id),
        )
        await db.commit()
    finally:
        await db.close()


async def _ignored_heads(project_id: int) -> set[str]:
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT snapshot_id FROM shared_ignored_head WHERE project_id=?",
                (project_id,),
            )
        ).fetchall()
        return {row["snapshot_id"] for row in rows}
    finally:
        await db.close()


async def _create_conflict_copy(project_id: int, info: SnapshotInfo) -> dict | None:
    from aqda.routers.projects import import_package_bytes

    result = await import_package_bytes(
        await asyncio.to_thread(info.path.read_bytes),
        mode="copy",
        target_lineage_id=info.lineage_id,
    )
    if not result["imported"]:
        return None
    copied = result["imported"][0]
    root = await get_shared_root()
    if root is not None:
        copied_project = await _project_row(copied["id"])
        folder = await asyncio.to_thread(_new_project_folder, root, copied_project["name"])
        await _update_sync_state(
            copied["id"],
            shared_folder=str(folder),
            shared_last_published_revision=None,
            shared_last_snapshot_id=None,
        )
        await _publish_project(copied["id"], force=True)
    await _mark_ignored(project_id, info.head_snapshot_id)
    await _update_sync_state(
        project_id,
        shared_sync_error=f'Concurrent edits detected; kept both versions as "{copied["name"]}".',
    )
    return copied


def _ready_to_publish(project, force: bool) -> bool:
    if force:
        return True
    try:
        modified = datetime.fromisoformat(project["modified_at"]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - modified).total_seconds() >= PUBLISH_IDLE_SECONDS
    except (TypeError, ValueError):
        return True


async def _sync_project(
    project_id: int,
    publish: bool = True,
    force_publish: bool = False,
) -> dict:
    project = await _project_row(project_id)
    if not project or not project["shared_folder"]:
        return {"project_id": project_id, "linked": False}
    folder = Path(project["shared_folder"])
    if not folder.is_dir():
        await _update_sync_state(project_id, shared_sync_error="Collaboration folder is unavailable")
        return {"project_id": project_id, "linked": True, "error": "folder unavailable"}

    infos = await _snapshot_infos(folder)
    heads = _head_infos(infos, project["lineage_id"])
    ignored = await _ignored_heads(project_id)
    imported = []
    conflicts = []

    from aqda.routers.projects import import_package_bytes

    for info in heads:
        if info.head_snapshot_id in ignored or info.head_snapshot_id == project["head_snapshot_id"]:
            continue
        result = await import_package_bytes(
            await asyncio.to_thread(info.path.read_bytes), mode="auto"
        )
        if result["conflicts"]:
            copied = await _create_conflict_copy(project_id, info)
            if copied:
                conflicts.append(copied)
        elif result["imported"]:
            imported.extend(result["imported"])
            await _update_sync_state(
                project_id,
                shared_last_published_revision=info.revision,
                shared_last_snapshot_id=info.head_snapshot_id,
                shared_last_sync_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                shared_sync_error=None,
            )
        project = await _project_row(project_id)

    published = None
    if publish:
        project = await _project_row(project_id)
        if (
            project
            and project["revision"] != project["shared_last_published_revision"]
            and _ready_to_publish(project, force_publish)
        ):
            published = await _publish_project(project_id)
    if conflicts:
        await _update_sync_state(
            project_id,
            shared_sync_error=(
                f'Concurrent edits detected; kept both versions as "{conflicts[-1]["name"]}".'
            ),
        )
    return {
        "project_id": project_id,
        "linked": True,
        "imported": imported,
        "conflicts": conflicts,
        "published": published,
    }


async def sync_project(project_id: int) -> dict:
    async with _sync_lock:
        try:
            return await _sync_project(project_id, force_publish=True)
        except Exception as exc:
            await _update_sync_state(project_id, shared_sync_error=str(exc))
            raise


async def sync_all_shared_projects(
    publish: bool = True,
    force_publish: bool = False,
) -> list[dict]:
    async with _sync_lock:
        db = await get_db()
        try:
            rows = await (
                await db.execute(
                    "SELECT id FROM project WHERE shared_folder IS NOT NULL "
                    "AND deleted_at IS NULL ORDER BY id"
                )
            ).fetchall()
        finally:
            await db.close()
        results = []
        for row in rows:
            try:
                results.append(
                    await _sync_project(
                        row["id"], publish=publish, force_publish=force_publish
                    )
                )
            except Exception as exc:
                await _update_sync_state(row["id"], shared_sync_error=str(exc))
        return results


async def discover_shared_projects() -> list[dict]:
    root = await get_shared_root()
    if root is None or not root.is_dir():
        return []
    db = await get_db()
    try:
        linked_rows = await (
            await db.execute("SELECT id, lineage_id, shared_folder FROM project")
        ).fetchall()
        linked_by_lineage = {row["lineage_id"]: row["id"] for row in linked_rows}
    finally:
        await db.close()

    discovered = []
    for folder in sorted(root.glob("*.aqda-project")):
        infos = await _snapshot_infos(folder)
        for lineage_id in sorted({item.lineage_id for item in infos}):
            heads = _head_infos(infos, lineage_id)
            if not heads:
                continue
            latest = heads[0]
            discovered.append({
                "folder": str(folder),
                "name": latest.name,
                "lineage_id": lineage_id,
                "revision": latest.revision,
                "updated_at": latest.created_at,
                "updated_by": latest.created_by,
                "head_count": len(heads),
                "linked_project_id": linked_by_lineage.get(lineage_id),
            })
    return discovered


async def open_shared_project(folder_path: str) -> dict:
    async with _sync_lock:
        root = await get_shared_root()
        if root is None:
            raise HTTPException(400, "Choose a collaboration folder in Settings first")
        folder = Path(folder_path).expanduser().resolve()
        if not folder.is_relative_to(root) or not folder.is_dir():
            raise HTTPException(400, "That project is not inside the configured collaboration folder")
        infos = await _snapshot_infos(folder)
        heads = _head_infos(infos)
        if not heads:
            raise HTTPException(400, "No valid AQDA snapshots were found in that project")
        primary = heads[0]

        from aqda.routers.projects import import_package_bytes

        result = await import_package_bytes(
            await asyncio.to_thread(primary.path.read_bytes), mode="auto"
        )
        if result["conflicts"]:
            local_id = result["conflicts"][0]["id"]
            copied = await _create_conflict_copy(local_id, primary)
            if not copied:
                raise HTTPException(409, "Concurrent versions were detected")
            project_id = copied["id"]
            return {
                "project_id": project_id,
                "name": copied["name"],
                "conflicts": result["conflicts"],
            }
        elif result["imported"]:
            project_id = result["imported"][0]["id"]
        elif result["unchanged"]:
            project_id = result["unchanged"][0]["id"]
        else:
            raise HTTPException(400, "The shared project could not be opened")

        await _update_sync_state(
            project_id,
            shared_folder=str(folder),
            shared_last_published_revision=primary.revision,
            shared_last_snapshot_id=primary.head_snapshot_id,
            shared_last_sync_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            shared_sync_error=None,
        )
        return {"project_id": project_id, "name": primary.name, "conflicts": result["conflicts"]}


async def _sync_loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        await sync_all_shared_projects()
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=SYNC_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def start_sync_service() -> None:
    global _sync_task, _stop_event
    if _sync_task and not _sync_task.done():
        return
    _stop_event = asyncio.Event()
    _sync_task = asyncio.create_task(_sync_loop(), name="aqda-shared-project-sync")


async def stop_sync_service() -> None:
    global _sync_task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _sync_task is not None:
        await _sync_task
    # A final publish catches changes made immediately before AQDA shuts down.
    await sync_all_shared_projects(publish=True, force_publish=True)
    _sync_task = None
    _stop_event = None


def write_project_metadata(folder: Path, name: str, lineage_id: str) -> None:
    """Optional human-readable marker; snapshots remain the source of truth."""
    metadata = {"format": "aqda-shared-project", "name": name, "lineage_id": lineage_id}
    path = folder / "project.json"
    path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
