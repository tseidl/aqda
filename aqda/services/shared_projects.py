"""Automatic shared-folder collaboration using immutable project snapshots."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from aqda.db import create_daily_backup, get_db

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
_global_sync_error: str | None = None
_last_sync_check_at: str | None = None
_last_daily_backup_day: str | None = None
logger = logging.getLogger(__name__)


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


def _inspect_snapshot_bytes(data: bytes) -> SnapshotInfo:
    """Inspect the exact bytes read from a writer file, not a later replacement."""
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".aqda")
    try:
        temp.write(data)
        temp.close()
        return _inspect_snapshot(Path(temp.name))
    finally:
        if not temp.closed:
            temp.close()
        Path(temp.name).unlink(missing_ok=True)


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
        if value:
            return Path(value).expanduser().resolve()
    finally:
        await db.close()
    roots = await get_shared_roots()
    return roots[0] if roots else None


async def get_shared_roots() -> list[Path]:
    """Return every saved collaboration location plus roots used by linked projects."""
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT key, value FROM setting "
                "WHERE key IN ('shared_folder', 'shared_folders')"
            )
        ).fetchall()
        settings = {row["key"]: row["value"] for row in rows}
        project_rows = await (
            await db.execute(
                "SELECT shared_folder FROM project WHERE shared_folder IS NOT NULL "
                "AND deleted_at IS NULL"
            )
        ).fetchall()
    finally:
        await db.close()

    values: list[str] = []
    try:
        saved = json.loads(settings.get("shared_folders", "[]"))
        if isinstance(saved, list):
            values.extend(str(item) for item in saved if isinstance(item, str))
    except (TypeError, ValueError):
        pass
    legacy = settings.get("shared_folder", "").strip()
    if legacy:
        values.append(legacy)
    for row in project_rows:
        values.append(str(Path(row["shared_folder"]).expanduser().resolve().parent))

    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value.strip():
            continue
        root = Path(value).expanduser().resolve()
        key = str(root)
        if key not in seen:
            seen.add(key)
            roots.append(root)
    return roots


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

    existing = await get_shared_roots()
    saved = [str(root), *(str(item) for item in existing if item != root)]
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('shared_folder', ?)",
            (str(root),),
        )
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('shared_folders', ?)",
            (json.dumps(saved),),
        )
        await db.commit()
    finally:
        await db.close()
    return root


async def remove_shared_root(path: str) -> None:
    target = Path(path).expanduser().resolve()
    db = await get_db()
    try:
        linked = await (
            await db.execute(
                "SELECT id, name, shared_folder FROM project "
                "WHERE shared_folder IS NOT NULL AND deleted_at IS NULL"
            )
        ).fetchall()
        in_use = [
            row
            for row in linked
            if Path(row["shared_folder"]).expanduser().resolve().parent == target
        ]
        if in_use:
            names = ", ".join(row["name"] for row in in_use[:3])
            raise HTTPException(
                409,
                f"Stop sharing linked projects from this location first: {names}",
            )
    finally:
        await db.close()

    remaining = [root for root in await get_shared_roots() if root != target]
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('shared_folders', ?)",
            (json.dumps([str(root) for root in remaining]),),
        )
        current = await (
            await db.execute("SELECT value FROM setting WHERE key='shared_folder'")
        ).fetchone()
        if current and Path(current["value"]).expanduser().resolve() == target:
            replacement = str(remaining[0]) if remaining else ""
            await db.execute(
                "INSERT OR REPLACE INTO setting (key, value) "
                "VALUES ('shared_folder', ?)",
                (replacement,),
            )
        await db.commit()
    finally:
        await db.close()


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


def _project_folder_for_share(root: Path, project) -> Path:
    previous = project["shared_previous_folder"]
    if previous:
        candidate = Path(previous).expanduser().resolve()
        if candidate.is_relative_to(root) and candidate.name.endswith(".aqda-project"):
            (candidate / "snapshots").mkdir(parents=True, exist_ok=True)
            return candidate
    return _new_project_folder(root, project["name"])


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


def _writer_snapshot_path(folder: Path, writer_id: str) -> Path:
    safe_writer = re.sub(r"[^A-Za-z0-9_-]", "-", writer_id)
    return folder / "snapshots" / f"writer-{safe_writer}.aqda"


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
    await asyncio.to_thread(
        write_project_metadata,
        folder,
        exported_project["name"],
        exported_project["lineage_id"],
    )
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


async def share_project(project_id: int, root_path: str | None = None) -> dict:
    async with _sync_lock:
        project = await _project_row(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        db = await get_db()
        try:
            mirror = await (
                await db.execute(
                    "SELECT 1 FROM shared_conflict_branch "
                    "WHERE conflict_project_id=? AND status='active'",
                    (project_id,),
                )
            ).fetchone()
        finally:
            await db.close()
        if mirror:
            raise HTTPException(
                409,
                "This is a reference copy of concurrent work and cannot be shared separately.",
            )
        folder = Path(project["shared_folder"]) if project["shared_folder"] else None
        if folder is None:
            roots = await get_shared_roots()
            if root_path:
                requested = Path(root_path).expanduser().resolve()
                root = requested if requested in roots else await set_shared_root(root_path)
            elif len(roots) == 1:
                root = roots[0]
            elif not roots:
                raise HTTPException(400, "Add a collaboration location first")
            else:
                raise HTTPException(400, "Choose which collaboration location to use")
            folder = await asyncio.to_thread(_project_folder_for_share, root, project)
            await _update_sync_state(
                project_id,
                shared_folder=str(folder),
                shared_previous_folder=str(folder),
                shared_last_published_revision=None,
                shared_last_snapshot_id=None,
                shared_sync_error=None,
            )
        await asyncio.to_thread(
            write_project_metadata,
            folder,
            project["name"],
            project["lineage_id"],
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


def _descends_from(info: SnapshotInfo, ancestor_snapshot_id: str) -> bool:
    current: str | None = info.head_snapshot_id
    seen: set[str] = set()
    while current and current not in seen:
        if current == ancestor_snapshot_id:
            return True
        seen.add(current)
        current = info.parent_by_id.get(current)
    return False


async def _active_conflict_branches(project_id: int) -> list[dict]:
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT b.*, p.name AS conflict_name, p.revision AS conflict_revision, "
                "p.deleted_at AS conflict_deleted_at FROM shared_conflict_branch b "
                "LEFT JOIN project p ON p.id=b.conflict_project_id "
                "WHERE b.project_id=? AND b.status='active' ORDER BY b.id",
                (project_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


async def _update_conflict_branch(branch_id: int, **fields) -> None:
    if not fields:
        return
    db = await get_db()
    try:
        assignments = ", ".join(f"{key}=?" for key in fields)
        await db.execute(
            f"UPDATE shared_conflict_branch SET {assignments}, "
            "updated_at=datetime('now') WHERE id=?",
            [*fields.values(), branch_id],
        )
        await db.commit()
    finally:
        await db.close()


async def _refresh_known_conflict_branch(
    project_id: int,
    info: SnapshotInfo,
) -> dict | None:
    """Update one existing conflict mirror when the remote branch advances.

    Returning ``None`` means this head belongs to no known branch and should go
    through normal conflict classification. Any returned dictionary means the
    head was handled (including a dismissed or locally edited mirror).
    """
    branches = await _active_conflict_branches(project_id)
    branch = next(
        (
            item
            for item in branches
            if item["source_lineage_id"] == info.lineage_id
            and _descends_from(info, item["latest_snapshot_id"])
        ),
        None,
    )
    if branch is None:
        return None

    await _mark_ignored(project_id, info.head_snapshot_id)
    common_updates = {
        "latest_snapshot_id": info.head_snapshot_id,
        "latest_snapshot_path": str(info.path),
    }
    conflict_id = branch["conflict_project_id"]
    if conflict_id is None or branch["conflict_deleted_at"] is not None:
        message = (
            "Concurrent work still exists in the shared folder. Its local reference "
            "copy was removed, so AQDA is no longer recreating it automatically."
        )
        await _update_conflict_branch(branch["id"], **common_updates)
        await _update_sync_state(project_id, shared_sync_error=message)
        return {
            "handled": True,
            "updated": False,
            "reason": "mirror_removed",
            "sync_error": message,
        }

    if branch["conflict_revision"] != branch["conflict_base_revision"]:
        message = (
            f'Concurrent work continues, but “{branch["conflict_name"]}” was edited '
            "locally. AQDA will not overwrite those edits; agree which version to keep."
        )
        await _update_conflict_branch(branch["id"], **common_updates)
        await _update_sync_state(project_id, shared_sync_error=message)
        return {
            "handled": True,
            "updated": False,
            "reason": "mirror_edited",
            "sync_error": message,
        }

    from aqda.routers.projects import refresh_conflict_copy

    refreshed = await refresh_conflict_copy(
        await asyncio.to_thread(info.path.read_bytes),
        conflict_id,
        info.lineage_id,
    )
    mirror_message = (
        "Reference copy of concurrent collaborator work. It updates automatically; "
        "agree which version to keep before coding here."
    )
    await _update_sync_state(conflict_id, shared_sync_error=mirror_message)
    await _update_conflict_branch(
        branch["id"],
        **common_updates,
        conflict_base_revision=refreshed["revision"],
    )
    await _update_sync_state(
        project_id,
        shared_sync_error=(
            f'Concurrent edits detected; “{refreshed["name"]}” is the single local '
            "reference copy of the collaborator's branch and will keep updating."
        ),
    )
    return {"handled": True, "updated": True, "project": refreshed}


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
    copied_project = await _project_row(copied["id"])
    mirror_message = (
        "Reference copy of concurrent collaborator work. It updates automatically; "
        "agree which version to keep before coding here."
    )
    await _update_sync_state(copied["id"], shared_sync_error=mirror_message)
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO shared_conflict_branch "
            "(project_id, source_lineage_id, anchor_snapshot_id, latest_snapshot_id, "
            "latest_snapshot_path, conflict_project_id, conflict_base_revision) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                info.lineage_id,
                info.head_snapshot_id,
                info.head_snapshot_id,
                str(info.path),
                copied["id"],
                copied_project["revision"],
            ),
        )
        await db.commit()
    finally:
        await db.close()
    await _mark_ignored(project_id, info.head_snapshot_id)
    await _update_sync_state(
        project_id,
        shared_sync_error=(
            f'Concurrent edits detected; “{copied["name"]}” is the single local '
            "reference copy of the collaborator's branch and will keep updating."
        ),
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
    branch_errors: list[str] = []

    from aqda.routers.projects import import_package_bytes

    for info in heads:
        if info.head_snapshot_id in ignored or info.head_snapshot_id == project["head_snapshot_id"]:
            continue
        handled_branch = await _refresh_known_conflict_branch(project_id, info)
        if handled_branch is not None:
            if handled_branch.get("updated"):
                conflicts.append(handled_branch["project"])
            if handled_branch.get("sync_error"):
                branch_errors.append(handled_branch["sync_error"])
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
                f'Concurrent edits detected; “{conflicts[-1]["name"]}” is the single local '
                "reference copy of the collaborator's branch and will keep updating."
            ),
        )
    elif branch_errors:
        await _update_sync_state(project_id, shared_sync_error=branch_errors[-1])
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
    roots = [root for root in await get_shared_roots() if root.is_dir()]
    if not roots:
        return []
    db = await get_db()
    try:
        local_rows = await (
            await db.execute(
                "SELECT id, lineage_id, shared_folder FROM project "
                "WHERE deleted_at IS NULL"
            )
        ).fetchall()
        local_by_lineage = {row["lineage_id"]: row for row in local_rows}
    finally:
        await db.close()

    discovered = []
    seen_folders: set[str] = set()
    for root in roots:
        for folder in sorted(root.glob("*.aqda-project")):
            folder_key = str(folder.resolve())
            if folder_key in seen_folders:
                continue
            seen_folders.add(folder_key)
            infos = await _snapshot_infos(folder)
            for lineage_id in sorted({item.lineage_id for item in infos}):
                heads = _head_infos(infos, lineage_id)
                if not heads:
                    continue
                latest = heads[0]
                local = local_by_lineage.get(lineage_id)
                local_shared_folder = (
                    Path(local["shared_folder"]).expanduser().resolve()
                    if local and local["shared_folder"]
                    else None
                )
                discovered.append({
                    "root": str(root),
                    "folder": str(folder),
                    "name": latest.name,
                    "lineage_id": lineage_id,
                    "revision": latest.revision,
                    "updated_at": latest.created_at,
                    "updated_by": latest.created_by,
                    "head_count": len(heads),
                    "local_project_id": local["id"] if local else None,
                    "linked_project_id": (
                        local["id"] if local_shared_folder == folder.resolve() else None
                    ),
                })
    return discovered


async def open_shared_project(
    folder_path: str,
    local_newer_choice: str | None = None,
) -> dict:
    async with _sync_lock:
        if local_newer_choice not in {None, "use_shared", "use_local"}:
            raise HTTPException(400, "Choose 'use_shared' or 'use_local'")
        roots = await get_shared_roots()
        if not roots:
            raise HTTPException(400, "Add a collaboration location in Settings first")
        folder = Path(folder_path).expanduser().resolve()
        if not any(folder.is_relative_to(root) for root in roots) or not folder.is_dir():
            raise HTTPException(400, "That project is not inside a saved collaboration location")
        infos = await _snapshot_infos(folder)
        heads = _head_infos(infos)
        if not heads:
            raise HTTPException(400, "No valid AQDA snapshots were found in that project")
        primary = heads[0]

        db = await get_db()
        try:
            already_local = await (
                await db.execute(
                    "SELECT id, name, shared_folder FROM project "
                    "WHERE lineage_id=? AND deleted_at IS NULL LIMIT 1",
                    (primary.lineage_id,),
                )
            ).fetchone()
        finally:
            await db.close()
        if already_local and already_local["shared_folder"]:
            current_folder = Path(already_local["shared_folder"]).expanduser().resolve()
            if current_folder != folder:
                raise HTTPException(
                    409,
                    f'“{already_local["name"]}” is already collaborating from '
                    f'"{current_folder.parent}". Stop sharing it there before connecting '
                    "the copy in this location.",
                )

        from aqda.routers.projects import import_package_bytes

        snapshot_data = await asyncio.to_thread(primary.path.read_bytes)
        result = await import_package_bytes(snapshot_data, mode="auto")
        local_newer = next(
            (
                item
                for item in result["unchanged"]
                if item.get("reason") == "local_newer"
            ),
            None,
        )
        backup_path = result.get("backup_path")
        if local_newer and local_newer_choice is None:
            return {
                "project_id": local_newer["id"],
                "name": local_newer["name"],
                "needs_local_newer_choice": True,
                "shared_name": primary.name,
                "folder": str(folder),
                "conflicts": [],
            }
        if local_newer and local_newer_choice == "use_shared":
            result = await import_package_bytes(
                snapshot_data,
                mode="replace",
                target_lineage_id=primary.lineage_id,
            )
            backup_path = result.get("backup_path")
        trashed_conflict = next(
            (item for item in result["conflicts"] if item.get("trashed")),
            None,
        )
        if trashed_conflict:
            result = await import_package_bytes(
                snapshot_data,
                mode="replace",
                target_lineage_id=primary.lineage_id,
            )
            backup_path = result.get("backup_path")
        if result["conflicts"]:
            local_id = result["conflicts"][0]["id"]
            await _update_sync_state(
                local_id,
                shared_folder=str(folder),
                shared_last_published_revision=None,
                shared_last_snapshot_id=None,
                shared_last_sync_at=None,
            )
            await _publish_project(local_id, force=True)
            copied = await _create_conflict_copy(local_id, primary)
            if not copied:
                raise HTTPException(409, "Concurrent versions were detected")
            project_id = local_id
            return {
                "project_id": project_id,
                "name": result["conflicts"][0]["name"],
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
        await _publish_project(project_id, force=True)
        return {
            "project_id": project_id,
            "name": primary.name,
            "conflicts": result["conflicts"],
            "needs_local_newer_choice": False,
            "backup_path": backup_path,
        }


def get_sync_health() -> dict[str, str | None]:
    return {
        "sync_error": _global_sync_error,
        "last_checked_at": _last_sync_check_at,
    }


async def unlink_shared_project(project_id: int) -> None:
    """Stop publishing this installation's writer snapshot, then clear the link."""
    async with _sync_lock:
        project = await _project_row(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        if project["shared_folder"]:
            folder = Path(project["shared_folder"])
            if not folder.is_dir():
                raise HTTPException(
                    409,
                    "The collaboration folder is unavailable. Reconnect it before stopping "
                    "sharing so AQDA can remove this computer's snapshot.",
                )
            path = _writer_snapshot_path(folder, await _device_id())
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
                _snapshot_cache.pop(path, None)
            except OSError as exc:
                raise HTTPException(
                    409,
                    "AQDA could not remove this computer's shared snapshot. "
                    "Reconnect the collaboration folder and try again.",
                ) from exc
        db = await get_db()
        try:
            mirrors = await (
                await db.execute(
                    "SELECT conflict_project_id FROM shared_conflict_branch "
                    "WHERE project_id=? AND conflict_project_id IS NOT NULL",
                    (project_id,),
                )
            ).fetchall()
            await db.execute(
                "UPDATE project SET shared_previous_folder=shared_folder, shared_folder=NULL, "
                "shared_last_published_revision=NULL, shared_last_snapshot_id=NULL, "
                "shared_last_sync_at=NULL, shared_sync_error=NULL WHERE id=?",
                (project_id,),
            )
            for mirror in mirrors:
                await db.execute(
                    "UPDATE project SET shared_sync_error=? WHERE id=?",
                    (
                        "Local archive of a former concurrent version. It no longer updates "
                        "because collaboration was stopped.",
                        mirror["conflict_project_id"],
                    ),
                )
            await db.execute(
                "DELETE FROM shared_conflict_branch WHERE project_id=?", (project_id,)
            )
            await db.execute("DELETE FROM shared_ignored_head WHERE project_id=?", (project_id,))
            await db.commit()
        finally:
            await db.close()


async def _rename_as_local_archive(project_id: int, original_name: str) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    base = f"{original_name} (previous local version, {stamp})"
    db = await get_db()
    try:
        candidate = base
        counter = 2
        while await (
            await db.execute(
                "SELECT 1 FROM project WHERE name=? AND id<>?", (candidate, project_id)
            )
        ).fetchone():
            candidate = f"{base} {counter}"
            counter += 1
        await db.execute(
            "UPDATE project SET name=?, shared_sync_error=? WHERE id=?",
            (
                candidate,
                "Local archive kept when collaboration switched to the other version. "
                "It no longer updates automatically.",
                project_id,
            ),
        )
        await db.commit()
        row = await (
            await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        ).fetchone()
        return dict(row)
    finally:
        await db.close()


async def resolve_conflict_copy(conflict_project_id: int, choice: str) -> dict:
    """Resolve one local collaboration branch without discarding either side's work."""
    if choice not in {"use_reference", "keep_current"}:
        raise HTTPException(400, "Choose 'use_reference' or 'keep_current'")

    async with _sync_lock:
        db = await get_db()
        try:
            branch = await (
                await db.execute(
                    "SELECT b.*, original.name AS original_name, "
                    "original.shared_folder AS original_shared_folder, "
                    "mirror.name AS mirror_name, mirror.revision AS mirror_revision, "
                    "mirror.deleted_at AS mirror_deleted_at "
                    "FROM shared_conflict_branch b "
                    "JOIN project original ON original.id=b.project_id "
                    "LEFT JOIN project mirror ON mirror.id=b.conflict_project_id "
                    "WHERE b.conflict_project_id=? AND b.status='active'",
                    (conflict_project_id,),
                )
            ).fetchone()
        finally:
            await db.close()
        if not branch or branch["mirror_deleted_at"] is not None:
            raise HTTPException(404, "This collaborator reference is no longer active")

        original_id = branch["project_id"]
        if choice == "keep_current":
            await _publish_project(original_id, force=True)
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE shared_conflict_branch SET status='resolved', "
                    "updated_at=datetime('now') WHERE id=?",
                    (branch["id"],),
                )
                await db.execute(
                    "UPDATE project SET deleted_at=datetime('now'), "
                    "modified_at=datetime('now'), shared_sync_error=? WHERE id=?",
                    (
                        "Local archive of a resolved concurrent version. Restore it from "
                        "Trash if you need to inspect it again.",
                        conflict_project_id,
                    ),
                )
                remaining = await (
                    await db.execute(
                        "SELECT COUNT(*) FROM shared_conflict_branch "
                        "WHERE project_id=? AND status='active'",
                        (original_id,),
                    )
                ).fetchone()
                if remaining[0] == 0:
                    await db.execute(
                        "UPDATE project SET shared_sync_error=NULL WHERE id=?", (original_id,)
                    )
                else:
                    await db.execute(
                        "UPDATE project SET shared_sync_error=? WHERE id=?",
                        (
                            f"{remaining[0]} other concurrent version(s) still need a "
                            "resolution choice.",
                            original_id,
                        ),
                    )
                await db.commit()
            finally:
                await db.close()
            return {
                "project_id": original_id,
                "choice": choice,
                "archived_project_id": conflict_project_id,
                "message": "Kept the current shared version; the reference is in Trash.",
            }

        if branch["mirror_revision"] != branch["conflict_base_revision"]:
            raise HTTPException(
                409,
                "This reference was edited locally, so AQDA will not replace either version "
                "automatically. Export it as an .aqda copy before resolving manually.",
            )
        if not branch["original_shared_folder"]:
            raise HTTPException(409, "The original project is no longer shared")

        snapshot_path = Path(branch["latest_snapshot_path"])
        if not snapshot_path.is_file():
            raise HTTPException(409, "The collaborator snapshot is currently unavailable")
        incoming_data = await asyncio.to_thread(snapshot_path.read_bytes)
        info = await asyncio.to_thread(_inspect_snapshot_bytes, incoming_data)
        if (
            info.lineage_id != branch["source_lineage_id"]
            or not _descends_from(info, branch["latest_snapshot_id"])
        ):
            raise HTTPException(
                409,
                "The collaborator snapshot changed unexpectedly. Wait for AQDA to sync and try again.",
            )
        from aqda.routers.export import build_aqda_snapshot
        from aqda.routers.projects import import_package_bytes

        local_data, _, _ = await build_aqda_snapshot(original_id)
        archive_id: int | None = None
        replaced = False
        try:
            archive_result = await import_package_bytes(
                local_data,
                mode="copy",
                target_lineage_id=branch["source_lineage_id"],
            )
            if not archive_result["imported"]:
                raise RuntimeError("AQDA could not preserve the previous local version")
            archive_id = archive_result["imported"][0]["id"]
            archive = await _rename_as_local_archive(archive_id, branch["original_name"])

            replace_result = await import_package_bytes(
                incoming_data,
                mode="replace",
                target_lineage_id=branch["source_lineage_id"],
            )
            if not replace_result["imported"]:
                raise RuntimeError("AQDA could not switch to the collaborator version")
            replaced = True

            remaining_count = 0
            db = await get_db()
            try:
                await db.execute(
                    "UPDATE shared_conflict_branch SET status='resolved', "
                    "updated_at=datetime('now') WHERE id=?",
                    (branch["id"],),
                )
                await db.execute("DELETE FROM project WHERE id=?", (conflict_project_id,))
                remaining = await (
                    await db.execute(
                        "SELECT COUNT(*) FROM shared_conflict_branch "
                        "WHERE project_id=? AND status='active'",
                        (original_id,),
                    )
                ).fetchone()
                remaining_count = remaining[0]
                await db.execute(
                    "UPDATE project SET shared_sync_error=? WHERE id=?",
                    (
                        (
                            f"{remaining_count} other concurrent version(s) still need a "
                            "resolution choice."
                            if remaining_count
                            else None
                        ),
                        original_id,
                    ),
                )
                await db.commit()
            finally:
                await db.close()
            await _publish_project(original_id, force=True)
            if remaining_count:
                await _update_sync_state(
                    original_id,
                    shared_sync_error=(
                        f"{remaining_count} other concurrent version(s) still need a "
                        "resolution choice."
                    ),
                )
            return {
                "project_id": original_id,
                "choice": choice,
                "archived_project_id": archive["id"],
                "archived_project_name": archive["name"],
                "backup_path": replace_result["backup_path"],
                "message": (
                    "Switched collaboration to this version and kept the previous local "
                    f'version as “{archive["name"]}”.'
                ),
            }
        except Exception:
            if archive_id is not None and not replaced:
                cleanup = await get_db()
                try:
                    await cleanup.execute("DELETE FROM project WHERE id=?", (archive_id,))
                    await cleanup.commit()
                finally:
                    await cleanup.close()
            raise


async def _sync_loop() -> None:
    global _global_sync_error, _last_sync_check_at, _last_daily_backup_day
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            await sync_all_shared_projects()
            day = datetime.now(timezone.utc).strftime("%Y%m%d")
            if _last_daily_backup_day != day:
                await asyncio.to_thread(create_daily_backup)
                _last_daily_backup_day = day
            _global_sync_error = None
            _last_sync_check_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _global_sync_error = str(exc)
            _last_sync_check_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            logger.exception("AQDA background synchronization failed; retrying")
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
