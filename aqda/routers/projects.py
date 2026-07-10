"""Project management routes."""

import asyncio
import os
import tempfile
import uuid
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from aqda.db import create_backup, get_db

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str
    description: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


@router.get("")
async def list_projects():
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM document WHERE project_id=p.id) as doc_count, "
            "(SELECT COUNT(*) FROM code WHERE project_id=p.id AND deleted_at IS NULL) as code_count "
            "FROM project p WHERE p.deleted_at IS NULL ORDER BY p.modified_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("", status_code=201)
async def create_project(data: ProjectCreate):
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO project (name, description, lineage_id) VALUES (?, ?, ?)",
            (data.name, data.description, str(uuid.uuid4())),
        )
        await db.commit()
        project_id = cursor.lastrowid
        cursor = await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.get("/trash/list")
async def list_trash():
    """List soft-deleted projects."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT p.*, "
            "(SELECT COUNT(*) FROM document WHERE project_id=p.id) as doc_count, "
            "(SELECT COUNT(*) FROM code WHERE project_id=p.id AND deleted_at IS NULL) as code_count "
            "FROM project p WHERE p.deleted_at IS NOT NULL ORDER BY p.deleted_at DESC"
        )
        return [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()


@router.get("/{project_id}")
async def get_project(project_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Project not found")
        return dict(row)
    finally:
        await db.close()


ALLOWED_PROJECT_FIELDS = {"name", "description"}


@router.patch("/{project_id}")
async def update_project(project_id: int, data: ProjectUpdate):
    db = await get_db()
    try:
        current = await (
            await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        ).fetchone()
        if not current:
            raise HTTPException(404, "Project not found")
        updates = []
        values = []
        for field, val in data.model_dump(exclude_none=True).items():
            if field not in ALLOWED_PROJECT_FIELDS:
                continue
            if current[field] == val:
                continue
            updates.append(f"{field}=?")
            values.append(val)
        if not updates:
            return dict(current)
        updates.append("modified_at=datetime('now')")
        updates.append("revision=revision+1")
        values.append(project_id)
        await db.execute(
            f"UPDATE project SET {', '.join(updates)} WHERE id=?", values
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.delete("/{project_id}", status_code=204)
async def delete_project(project_id: int):
    """Soft-delete a project (moves to trash)."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE project SET deleted_at=datetime('now'), modified_at=datetime('now') "
            "WHERE id=? AND deleted_at IS NULL",
            (project_id,),
        )
        await db.commit()
    finally:
        await db.close()


@router.post("/{project_id}/restore")
async def restore_project(project_id: int):
    """Restore a soft-deleted project."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE project SET deleted_at=NULL, modified_at=datetime('now') "
            "WHERE id=? AND deleted_at IS NOT NULL",
            (project_id,),
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.delete("/{project_id}/permanent", status_code=204)
async def delete_project_permanent(project_id: int):
    """Permanently delete a project (no recovery)."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM project WHERE id=?", (project_id,))
        await db.commit()
    finally:
        await db.close()


def _value(row, key: str, default=None):
    """Read an optional column from an aiosqlite.Row (for legacy packages)."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


async def _source_snapshots(src, project_id: int) -> list[dict]:
    try:
        cursor = await src.execute(
            "SELECT snapshot_id, parent_snapshot_id, revision, created_at, created_by "
            "FROM project_snapshot WHERE project_id=? ORDER BY created_at",
            (project_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


async def _unique_project_name(dst, requested: str, suffix: str = "") -> str:
    base = requested.strip() or "Imported project"
    candidate = f"{base}{suffix}"
    counter = 2
    while True:
        cursor = await dst.execute("SELECT 1 FROM project WHERE name=?", (candidate,))
        if not await cursor.fetchone():
            return candidate
        candidate = f"{base}{suffix} {counter}"
        counter += 1


def _incoming_ancestor_ids(head_snapshot_id: str | None, snapshots: list[dict]) -> set[str]:
    by_id = {item["snapshot_id"]: item for item in snapshots}
    ancestors: set[str] = set()
    current = head_snapshot_id
    while current and current not in ancestors:
        ancestors.add(current)
        item = by_id.get(current)
        current = item.get("parent_snapshot_id") if item else None
    return ancestors


async def _classify_same_lineage(
    dst,
    local_project,
    incoming_head: str | None,
    incoming_revision: int,
    incoming_snapshots: list[dict],
) -> str:
    """Return update, unchanged, local_newer, or conflict for a known lineage."""
    if not incoming_head:
        return "conflict"

    cursor = await dst.execute(
        "SELECT snapshot_id, revision FROM project_snapshot WHERE project_id=?",
        (local_project["id"],),
    )
    local_snapshots = {row["snapshot_id"]: row["revision"] for row in await cursor.fetchall()}

    if incoming_head in local_snapshots:
        if local_project["revision"] == incoming_revision:
            return "unchanged"
        return "local_newer"

    incoming_ancestors = _incoming_ancestor_ids(incoming_head, incoming_snapshots)
    for snapshot_id in incoming_ancestors:
        if local_snapshots.get(snapshot_id) == local_project["revision"]:
            return "update"
    return "conflict"


async def _copy_project_contents(
    src,
    dst,
    source_project,
    target_project_id: int,
    snapshots: list[dict],
):
    """Copy one project's child records while remapping every internal ID."""
    source_project_id = source_project["id"]

    cursor = await src.execute("SELECT * FROM document WHERE project_id=?", (source_project_id,))
    docs = await cursor.fetchall()
    doc_map: dict[int, int] = {}
    for doc in docs:
        cursor = await dst.execute(
            "INSERT INTO document (project_id, name, content, source_type, transcript, label, "
            "exclude_from_ai, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_project_id,
                doc["name"],
                doc["content"],
                _value(doc, "source_type", "text"),
                _value(doc, "transcript"),
                _value(doc, "label", "") or "",
                _value(doc, "exclude_from_ai", 0) or 0,
                _value(doc, "created_at"),
                _value(doc, "modified_at"),
            ),
        )
        doc_map[doc["id"]] = cursor.lastrowid

    try:
        for old_doc_id, new_doc_id in doc_map.items():
            cursor = await src.execute(
                "SELECT key, value FROM document_variable WHERE document_id=?", (old_doc_id,)
            )
            for variable in await cursor.fetchall():
                await dst.execute(
                    "INSERT INTO document_variable (document_id, key, value) VALUES (?, ?, ?)",
                    (new_doc_id, variable["key"], variable["value"]),
                )
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise

    source_batches = []
    try:
        cursor = await src.execute(
            "SELECT id, root_code_id, deleted_at FROM code_deletion_batch "
            "WHERE project_id=?",
            (source_project_id,),
        )
        source_batches = await cursor.fetchall()
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
    batch_map = {batch["id"]: str(uuid.uuid4()) for batch in source_batches}

    cursor = await src.execute("SELECT * FROM code WHERE project_id=?", (source_project_id,))
    source_codes = await cursor.fetchall()
    code_map: dict[int, int] = {}
    for code in source_codes:
        cursor = await dst.execute(
            "INSERT INTO code (project_id, parent_id, name, description, color, sort_order, "
            "created_at, deleted_at, deletion_batch_id) "
            "VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_project_id,
                code["name"],
                _value(code, "description", "") or "",
                _value(code, "color", "#6366f1") or "#6366f1",
                _value(code, "sort_order", 0) or 0,
                _value(code, "created_at"),
                _value(code, "deleted_at"),
                batch_map.get(_value(code, "deletion_batch_id")),
            ),
        )
        code_map[code["id"]] = cursor.lastrowid
    for code in source_codes:
        parent_id = _value(code, "parent_id")
        if parent_id in code_map:
            await dst.execute(
                "UPDATE code SET parent_id=? WHERE id=?",
                (code_map[parent_id], code_map[code["id"]]),
            )

    for batch in source_batches:
        root_code_id = code_map.get(batch["root_code_id"])
        if root_code_id is not None:
            await dst.execute(
                "INSERT INTO code_deletion_batch "
                "(id, project_id, root_code_id, deleted_at) VALUES (?, ?, ?, ?)",
                (
                    batch_map[batch["id"]],
                    target_project_id,
                    root_code_id,
                    batch["deleted_at"],
                ),
            )

    coding_map: dict[int, int] = {}
    if doc_map:
        placeholders = ",".join("?" * len(doc_map))
        cursor = await src.execute(
            f"SELECT * FROM coding WHERE document_id IN ({placeholders})",
            list(doc_map),
        )
        for coding in await cursor.fetchall():
            new_doc = doc_map.get(coding["document_id"])
            new_code = code_map.get(coding["code_id"])
            if new_doc is None or new_code is None:
                continue
            cursor = await dst.execute(
                "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
                "coder, created_at, deleted_at, deletion_batch_id, offset_unit, repair_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_doc,
                    new_code,
                    coding["start_pos"],
                    coding["end_pos"],
                    coding["selected_text"],
                    _value(coding, "coder", "") or "",
                    _value(coding, "created_at"),
                    _value(coding, "deleted_at"),
                    batch_map.get(_value(coding, "deletion_batch_id")),
                    _value(coding, "offset_unit", "legacy_utf16") or "legacy_utf16",
                    _value(coding, "repair_status"),
                ),
            )
            coding_map[coding["id"]] = cursor.lastrowid

    try:
        cursor = await src.execute("SELECT * FROM memo WHERE project_id=?", (source_project_id,))
        for memo in await cursor.fetchall():
            await dst.execute(
                "INSERT INTO memo (project_id, document_id, code_id, coding_id, start_pos, "
                "end_pos, title, content, created_at, modified_at, offset_unit, repair_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target_project_id,
                    doc_map.get(_value(memo, "document_id")),
                    code_map.get(_value(memo, "code_id")),
                    coding_map.get(_value(memo, "coding_id")),
                    _value(memo, "start_pos"),
                    _value(memo, "end_pos"),
                    _value(memo, "title", "") or "",
                    _value(memo, "content", "") or "",
                    _value(memo, "created_at"),
                    _value(memo, "modified_at"),
                    _value(memo, "offset_unit", "legacy_utf16") or "legacy_utf16",
                    _value(memo, "repair_status"),
                ),
            )
    except aiosqlite.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise

    for snapshot in snapshots:
        await dst.execute(
            "INSERT OR IGNORE INTO project_snapshot "
            "(snapshot_id, project_id, parent_snapshot_id, revision, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                snapshot["snapshot_id"],
                target_project_id,
                snapshot.get("parent_snapshot_id"),
                snapshot["revision"],
                snapshot["created_at"],
                snapshot.get("created_by", "") or "",
            ),
        )


@router.post("/import-db", status_code=201)
async def import_from_db(
    file: UploadFile = File(...),
    mode: str = Form("auto"),
    target_lineage_id: str | None = Form(None),
):
    """Import or safely fast-forward projects from an AQDA snapshot.

    Collaboration snapshots retain a stable lineage and snapshot ancestry. An
    incoming descendant updates the existing project only when its current
    revision still matches a shared ancestor. Divergent work is never overwritten.
    Legacy databases without lineage metadata continue to import as new projects.
    """
    content = await file.read()
    return await import_package_bytes(content, mode, target_lineage_id)


async def import_package_bytes(
    content: bytes,
    mode: str = "auto",
    target_lineage_id: str | None = None,
):
    """Internal import entry point used by uploads and shared-folder sync."""
    if mode not in {"auto", "copy", "replace"}:
        raise HTTPException(400, "Import mode must be 'auto', 'copy', or 'replace'")
    if mode in {"copy", "replace"} and not target_lineage_id:
        raise HTTPException(400, "A target project lineage is required for conflict resolution")
    if not content:
        raise HTTPException(400, "The project file is empty")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.write(content)
    tmp.close()

    imported: list[dict] = []
    conflicts: list[dict] = []
    unchanged: list[dict] = []
    backup_path: str | None = None
    src = dst = None
    try:
        src = await aiosqlite.connect(tmp.name)
        src.row_factory = aiosqlite.Row
        dst = await get_db()

        try:
            check = await (await src.execute("PRAGMA quick_check")).fetchone()
            if not check or check[0] != "ok":
                raise HTTPException(400, "The project snapshot is incomplete or corrupted")
            try:
                try:
                    cursor = await src.execute("SELECT * FROM project WHERE deleted_at IS NULL")
                except aiosqlite.OperationalError as exc:
                    if "no such column" not in str(exc).lower():
                        raise
                    cursor = await src.execute("SELECT * FROM project")
                source_projects = await cursor.fetchall()
            except aiosqlite.DatabaseError:
                raise HTTPException(
                    400,
                    "Not an AQDA project file. Expected an .aqda file "
                    "(from Export → Share Project) or an AQDA database.",
                )

            # Reserve the write slot before inspecting local ancestry. This keeps
            # classification, the pre-update backup, and replacement atomic with
            # respect to every other browser request that could edit the project.
            await dst.execute("BEGIN IMMEDIATE")
            plans = []
            for source_project in source_projects:
                lineage_id = _value(source_project, "lineage_id")
                if mode in {"copy", "replace"} and lineage_id != target_lineage_id:
                    continue
                incoming_revision = int(_value(source_project, "revision", 0) or 0)
                incoming_head = _value(source_project, "head_snapshot_id")
                snapshots = await _source_snapshots(src, source_project["id"])

                local_project = None
                if lineage_id and mode in {"auto", "replace"}:
                    cursor = await dst.execute(
                        "SELECT * FROM project WHERE lineage_id=?", (lineage_id,)
                    )
                    local_project = await cursor.fetchone()

                if local_project:
                    if mode == "replace":
                        plans.append(("update", source_project, local_project, snapshots))
                        continue
                    if local_project["deleted_at"] is not None:
                        latest = next(
                            (s for s in snapshots if s["snapshot_id"] == incoming_head), {}
                        )
                        conflicts.append({
                            "id": local_project["id"],
                            "name": local_project["name"],
                            "incoming_name": source_project["name"],
                            "lineage_id": lineage_id,
                            "local_revision": local_project["revision"],
                            "incoming_revision": incoming_revision,
                            "incoming_coder": latest.get("created_by", ""),
                            "trashed": True,
                        })
                        continue
                    classification = await _classify_same_lineage(
                        dst, local_project, incoming_head, incoming_revision, snapshots
                    )
                    if classification == "update":
                        plans.append(("update", source_project, local_project, snapshots))
                    elif classification in {"unchanged", "local_newer"}:
                        unchanged.append({
                            "id": local_project["id"],
                            "name": local_project["name"],
                            "lineage_id": lineage_id,
                            "reason": classification,
                        })
                    else:
                        latest = next(
                            (s for s in snapshots if s["snapshot_id"] == incoming_head), {}
                        )
                        conflicts.append({
                            "id": local_project["id"],
                            "name": local_project["name"],
                            "incoming_name": source_project["name"],
                            "lineage_id": lineage_id,
                            "local_revision": local_project["revision"],
                            "incoming_revision": incoming_revision,
                            "incoming_coder": latest.get("created_by", ""),
                            "trashed": local_project["deleted_at"] is not None,
                        })
                else:
                    plans.append(("copy" if mode == "copy" else "create", source_project, None, snapshots))

            if any(action == "update" for action, *_ in plans):
                backup_path = str(await asyncio.to_thread(create_backup, "before-shared-update"))

            for action, source_project, local_project, snapshots in plans:
                incoming_lineage = _value(source_project, "lineage_id")
                incoming_revision = int(_value(source_project, "revision", 0) or 0)
                incoming_head = _value(source_project, "head_snapshot_id")

                if action == "update":
                    project_id = local_project["id"]
                    await dst.execute("DELETE FROM memo WHERE project_id=?", (project_id,))
                    await dst.execute("DELETE FROM document WHERE project_id=?", (project_id,))
                    await dst.execute(
                        "DELETE FROM code_deletion_batch WHERE project_id=?", (project_id,)
                    )
                    await dst.execute("DELETE FROM code WHERE project_id=?", (project_id,))
                    await dst.execute(
                        "UPDATE project SET name=?, description=?, created_at=?, modified_at=?, "
                        "deleted_at=NULL, revision=?, head_snapshot_id=? WHERE id=?",
                        (
                            source_project["name"],
                            _value(source_project, "description", "") or "",
                            _value(source_project, "created_at"),
                            _value(source_project, "modified_at"),
                            incoming_revision,
                            incoming_head,
                            project_id,
                        ),
                    )
                    await _copy_project_contents(
                        src, dst, source_project, project_id, snapshots
                    )
                    name = source_project["name"]
                else:
                    if action == "copy":
                        lineage_id = str(uuid.uuid4())
                        revision = 0
                        head_snapshot_id = None
                        history = []
                        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                        head = next(
                            (item for item in snapshots if item["snapshot_id"] == incoming_head),
                            {},
                        )
                        creator = head.get("created_by", "")
                        source = f" from {creator}" if creator else ""
                        suffix = f" (conflicting copy{source}, {stamp})"
                    else:
                        lineage_id = incoming_lineage or str(uuid.uuid4())
                        revision = incoming_revision
                        head_snapshot_id = incoming_head
                        history = snapshots
                        suffix = ""
                    name = await _unique_project_name(dst, source_project["name"], suffix)
                    cursor = await dst.execute(
                        "INSERT INTO project (name, description, created_at, modified_at, "
                        "lineage_id, revision, head_snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            name,
                            _value(source_project, "description", "") or "",
                            _value(source_project, "created_at"),
                            datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                            lineage_id,
                            revision,
                            head_snapshot_id,
                        ),
                    )
                    project_id = cursor.lastrowid
                    await _copy_project_contents(
                        src, dst, source_project, project_id, history
                    )

                imported.append({
                    "id": project_id,
                    "name": name,
                    "action": action,
                    "lineage_id": incoming_lineage if action == "update" else lineage_id,
                })

            if imported:
                from aqda.services.offsets import repair_legacy_offsets

                await repair_legacy_offsets(dst, [item["id"] for item in imported])
            await dst.commit()
        except Exception:
            if dst and dst.in_transaction:
                await dst.rollback()
            raise
    finally:
        if src:
            await src.close()
        if dst:
            await dst.close()
        os.unlink(tmp.name)

    return {
        "imported": imported,
        "conflicts": conflicts,
        "unchanged": unchanged,
        "count": len(imported),
        "backup_path": backup_path,
    }
