"""Project management routes."""

import asyncio
import hashlib
import os
import tempfile
import uuid
from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from aqda.db import (
    SNAPSHOT_FORMAT,
    create_shared_update_backup,
    get_db,
    read_snapshot_format_async,
)
from aqda.services.coding_fields import project_has_runs_or_fields
from aqda.services.coding_runs import refuse_while_run_in_progress

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
            "(SELECT COUNT(*) FROM code WHERE project_id=p.id AND deleted_at IS NULL) as code_count, "
            "EXISTS(SELECT 1 FROM shared_conflict_branch b "
            "WHERE b.conflict_project_id=p.id AND b.status='active') AS is_conflict_mirror "
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
        cursor = await db.execute(
            "SELECT p.*, EXISTS(SELECT 1 FROM shared_conflict_branch b "
            "WHERE b.conflict_project_id=p.id AND b.status='active') "
            "AS is_conflict_mirror FROM project p WHERE p.id=?",
            (project_id,),
        )
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
        cursor = await db.execute(
            "SELECT p.*, EXISTS(SELECT 1 FROM shared_conflict_branch b "
            "WHERE b.conflict_project_id=p.id AND b.status='active') "
            "AS is_conflict_mirror FROM project p WHERE p.id=?",
            (project_id,),
        )
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
async def delete_project_permanent(project_id: int, force: bool = False):
    """Permanently delete a project (no recovery).

    ``force`` deletes a shared project even when its collaboration folder is unreachable
    and this computer's snapshot therefore cannot be removed from it.
    """
    db = await get_db()
    try:
        project = await (
            await db.execute("SELECT shared_folder FROM project WHERE id=?", (project_id,))
        ).fetchone()
    finally:
        await db.close()
    if project and project["shared_folder"]:
        from aqda.services.shared_projects import unlink_shared_project

        await unlink_shared_project(project_id, force=force)

    db = await get_db()
    try:
        await db.execute("DELETE FROM project WHERE id=?", (project_id,))
        await db.commit()
    finally:
        await db.close()


class SnapshotFormatError(HTTPException):
    """A project file this AQDA must not apply because of its content format.

    Shared-folder sync catches it, shows the message in the project's status, and carries
    on with other work instead of failing the whole pass.
    """

    def __init__(self, detail: str):
        super().__init__(409, detail)


NEWER_FORMAT_MESSAGE = (
    "This project was saved by a newer version of AQDA. Update AQDA to open it or to "
    "receive these changes."
)


# Message for a project whose runs or fields an older AQDA's version would drop.
def older_format_message(project_name: str) -> str:
    return (
        f"“{project_name}” has model coding runs or coding fields, but the incoming version "
        "was saved by an older AQDA that cannot keep them. It was not applied, so nothing "
        "was lost. The collaborator needs to update AQDA."
    )


# Refuse a package written in a content format newer than this AQDA understands.
async def _check_newer_format(src) -> int:
    incoming_format = await read_snapshot_format_async(src)
    if incoming_format > SNAPSHOT_FORMAT:
        raise SnapshotFormatError(NEWER_FORMAT_MESSAGE)
    return incoming_format


# Refuse to replace a project whose runs or fields an older-format package cannot carry.
async def _check_older_format(dst, incoming_format: int, project) -> None:
    if incoming_format < SNAPSHOT_FORMAT and await project_has_runs_or_fields(
        dst, project["id"]
    ):
        raise SnapshotFormatError(older_format_message(project["name"]))


# Delete a project's content before a newer version is copied in (rows only, not the
# project itself). Every table holding project content must be named here.
async def _delete_project_contents(dst, project_id: int) -> None:
    await dst.execute("DELETE FROM memo WHERE project_id=?", (project_id,))
    # Documents take their codings (and those codings' field answers), variables, and
    # run records with them.
    await dst.execute("DELETE FROM document WHERE project_id=?", (project_id,))
    await dst.execute("DELETE FROM coding_field WHERE project_id=?", (project_id,))
    await dst.execute("DELETE FROM coding_run WHERE project_id=?", (project_id,))
    await dst.execute("DELETE FROM code_deletion_batch WHERE project_id=?", (project_id,))
    await dst.execute("DELETE FROM code WHERE project_id=?", (project_id,))


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
        cursor = await dst.execute(
            "SELECT 1 FROM project WHERE name=? AND deleted_at IS NULL", (candidate,)
        )
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


async def _rows_if_table(src, sql: str, params) -> list:
    """Run a query on a source package; a table it predates yields no rows."""
    try:
        return await (await src.execute(sql, params)).fetchall()
    except aiosqlite.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return []
        raise


async def _copy_runs(
    src, dst, source_project_id: int, target_project_id: int, doc_map: dict, code_map: dict
) -> dict[int, int]:
    """Copy model coding runs and their read documents; return the run ID map.

    The run's uuid stays the same (programs address a run by it). Its captured codebook
    is a historical record and is copied unchanged, including any IDs inside it.
    """
    run_map: dict[int, int] = {}
    runs = await _rows_if_table(
        src, "SELECT * FROM coding_run WHERE project_id=? ORDER BY id", (source_project_id,)
    )
    for run in runs:
        cursor = await dst.execute(
            "INSERT INTO coding_run (project_id, uuid, label, model, instructions, settings, "
            "codebook, allow_new_codes, proposed_parent_code_id, status, note, started_at, "
            "last_activity_at, finished_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                target_project_id,
                _value(run, "uuid") or str(uuid.uuid4()),
                _value(run, "label", "") or "",
                _value(run, "model", "") or "",
                _value(run, "instructions", "") or "",
                _value(run, "settings", "{}") or "{}",
                _value(run, "codebook"),
                _value(run, "allow_new_codes", 0) or 0,
                code_map.get(_value(run, "proposed_parent_code_id")),
                _value(run, "status", "finished") or "finished",
                _value(run, "note", "") or "",
                _value(run, "started_at"),
                _value(run, "last_activity_at"),
                _value(run, "finished_at"),
                _value(run, "deleted_at"),
            ),
        )
        run_map[run["id"]] = cursor.lastrowid

    if run_map:
        placeholders = ",".join("?" * len(run_map))
        rows = await _rows_if_table(
            src,
            f"SELECT * FROM coding_run_document WHERE run_id IN ({placeholders})",
            list(run_map),
        )
        for row in rows:
            new_doc = doc_map.get(row["document_id"])
            if new_doc is None:
                continue
            await dst.execute(
                "INSERT INTO coding_run_document (run_id, document_id, text_hash, "
                "processed_at, submitted, accepted, refused, raw_response) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_map[row["run_id"]],
                    new_doc,
                    _value(row, "text_hash"),
                    _value(row, "processed_at"),
                    _value(row, "submitted", 0) or 0,
                    _value(row, "accepted", 0) or 0,
                    _value(row, "refused", "[]") or "[]",
                    _value(row, "raw_response"),
                ),
            )
    return run_map


async def _copy_coding_fields(
    src, dst, source_project_id: int, target_project_id: int, coding_map: dict
) -> None:
    """Copy a project's coding fields and the answers of the copied codings."""
    fields = await _rows_if_table(
        src, "SELECT * FROM coding_field WHERE project_id=? ORDER BY id", (source_project_id,)
    )
    field_map: dict[int, int] = {}
    for field in fields:
        cursor = await dst.execute(
            "INSERT INTO coding_field (project_id, name, kind, sort_order, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                target_project_id,
                field["name"],
                _value(field, "kind", "text") or "text",
                _value(field, "sort_order", 0) or 0,
                _value(field, "created_at"),
            ),
        )
        field_map[field["id"]] = cursor.lastrowid
    if not field_map:
        return
    placeholders = ",".join("?" * len(field_map))
    rows = await _rows_if_table(
        src,
        f"SELECT coding_id, field_id, value FROM coding_field_value "
        f"WHERE field_id IN ({placeholders})",
        list(field_map),
    )
    await dst.executemany(
        "INSERT INTO coding_field_value (coding_id, field_id, value) VALUES (?, ?, ?)",
        [
            (coding_map[row["coding_id"]], field_map[row["field_id"]], row["value"])
            for row in rows
            if row["coding_id"] in coding_map
        ],
    )


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

    run_map = await _copy_runs(src, dst, source_project_id, target_project_id, doc_map, code_map)

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
                "coder, created_at, deleted_at, deletion_batch_id, offset_unit, repair_status, "
                "run_id, accepted_from_run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    run_map.get(_value(coding, "run_id")),
                    run_map.get(_value(coding, "accepted_from_run_id")),
                ),
            )
            coding_map[coding["id"]] = cursor.lastrowid

    await _copy_coding_fields(src, dst, source_project_id, target_project_id, coding_map)

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


# The text AQDA embeds for a document: the transcript for audio, else the content.
_EMBEDDED_TEXT_SQL = "CASE WHEN source_type='audio' THEN transcript ELSE content END"


def _text_key(text: str | None) -> str:
    """Identify a document text, to recognize it again under a new document ID."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def _set_embeddings_aside(dst, project_id: int) -> dict[str, int]:
    """Keep a project's cached embeddings while a newer version replaces its documents.

    Replacing a project re-creates every document under a new ID, and the cache is deleted
    with the old documents. The rows are copied to a temporary table and the old document
    IDs returned by text, so ``_restore_embeddings`` can hand them to the new documents
    whose text is unchanged. Embeddings stay on this computer; snapshots never carry them.
    """
    await dst.execute("DROP TABLE IF EXISTS temp.aqda_kept_embedding")
    rows = await (
        await dst.execute(
            f"SELECT id, {_EMBEDDED_TEXT_SQL} AS text FROM document WHERE project_id=? "
            "AND id IN (SELECT DISTINCT document_id FROM embedding_cache WHERE project_id=?)",
            (project_id, project_id),
        )
    ).fetchall()
    if not rows:
        return {}
    await dst.execute(
        "CREATE TEMP TABLE aqda_kept_embedding AS "
        "SELECT * FROM embedding_cache WHERE project_id=?",
        (project_id,),
    )
    return {_text_key(row["text"]): row["id"] for row in rows}


async def _restore_embeddings(dst, project_id: int, kept: dict[str, int]) -> None:
    """Give the embeddings set aside to the new documents that have the same text."""
    if not kept:
        return
    from aqda.routers.ai import _chunk_id

    documents = await (
        await dst.execute(
            f"SELECT id, {_EMBEDDED_TEXT_SQL} AS text FROM document WHERE project_id=?",
            (project_id,),
        )
    ).fetchall()
    for document in documents:
        old_id = kept.get(_text_key(document["text"]))
        if old_id is None:
            continue
        chunks = await (
            await dst.execute(
                "SELECT model, start_pos, end_pos, chunk_text, embedding "
                "FROM temp.aqda_kept_embedding WHERE document_id=?",
                (old_id,),
            )
        ).fetchall()
        await dst.executemany(
            "INSERT OR IGNORE INTO embedding_cache "
            "(id, document_id, project_id, model, start_pos, end_pos, chunk_text, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    # The chunk ID includes the document ID, so it is derived again.
                    _chunk_id(
                        document["id"], chunk["start_pos"], chunk["end_pos"],
                        chunk["model"], chunk["chunk_text"],
                    ),
                    document["id"], project_id, chunk["model"], chunk["start_pos"],
                    chunk["end_pos"], chunk["chunk_text"], chunk["embedding"],
                )
                for chunk in chunks
            ],
        )
    await dst.execute("DROP TABLE IF EXISTS temp.aqda_kept_embedding")


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
    copy_label: str = "conflicting copy",
):
    """Internal import entry point used by uploads and shared-folder sync.

    ``copy_label`` names a project kept beside the local one (mode "copy"): a file the
    researcher imported stays a "conflicting copy", while shared-folder sync passes the
    name its copy carries in the interface, "collaborator reference".
    """
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
            try:
                check = await (await src.execute("PRAGMA quick_check")).fetchone()
                if not check or check[0] != "ok":
                    raise HTTPException(400, "The project snapshot is incomplete or corrupted")
                try:
                    cursor = await src.execute("SELECT * FROM project WHERE deleted_at IS NULL")
                except aiosqlite.OperationalError as exc:
                    if "no such column" not in str(exc).lower():
                        raise
                    cursor = await src.execute("SELECT * FROM project")
                source_projects = await cursor.fetchall()
            except aiosqlite.DatabaseError:
                # Raised by the very first statement when the file is not a database at all.
                raise HTTPException(
                    400,
                    "Not an AQDA project file. Expected an .aqda file "
                    "(from Export → Save a copy to send) or an AQDA database.",
                )

            incoming_format = await _check_newer_format(src)

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

            # Checked before the backup, so a refused update leaves no trace at all.
            # Replacing a project re-creates every record ID, which a model run in
            # progress would not survive; a new project from the file is always fine.
            for action, _, local_project, _ in plans:
                if action == "update":
                    await refuse_while_run_in_progress(
                        dst, [local_project["id"]],
                        f"replace the contents of “{local_project['name']}”",
                    )
                    await _check_older_format(dst, incoming_format, local_project)

            if any(action == "update" for action, *_ in plans):
                keep_row = await (
                    await dst.execute(
                        "SELECT value FROM setting WHERE key='shared_update_backups'"
                    )
                ).fetchone()
                keep = int(keep_row["value"]) if keep_row and keep_row["value"].isdigit() else 10
                backup_path = str(await asyncio.to_thread(create_shared_update_backup, keep))

            for action, source_project, local_project, snapshots in plans:
                incoming_lineage = _value(source_project, "lineage_id")
                incoming_revision = int(_value(source_project, "revision", 0) or 0)
                incoming_head = _value(source_project, "head_snapshot_id")

                if action == "update":
                    project_id = local_project["id"]
                    kept_embeddings = await _set_embeddings_aside(dst, project_id)
                    await _delete_project_contents(dst, project_id)
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
                    await _restore_embeddings(dst, project_id, kept_embeddings)
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
                        suffix = f" ({copy_label}{source}, {stamp})"
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


async def refresh_conflict_copy(
    content: bytes,
    target_project_id: int,
    source_lineage_id: str,
) -> dict:
    """Refresh an untouched local conflict mirror from a newer remote snapshot.

    Conflict mirrors deliberately have their own local lineage, so the normal
    import classifier must not mistake them for the shared project itself. This
    targeted replacement preserves the mirror's ID, name, and local-only status
    while remapping all child-record IDs atomically.
    """
    if not content:
        raise HTTPException(400, "The project snapshot is empty")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.write(content)
    tmp.close()

    src = dst = None
    try:
        src = await aiosqlite.connect(tmp.name)
        src.row_factory = aiosqlite.Row
        dst = await get_db()

        check = await (await src.execute("PRAGMA quick_check")).fetchone()
        if not check or check[0] != "ok":
            raise HTTPException(400, "The collaboration snapshot is incomplete or corrupted")
        try:
            source_project = await (
                await src.execute(
                    "SELECT * FROM project WHERE lineage_id=? AND deleted_at IS NULL",
                    (source_lineage_id,),
                )
            ).fetchone()
        except aiosqlite.OperationalError as exc:
            raise HTTPException(400, "The collaboration snapshot is missing lineage data") from exc
        if not source_project:
            raise HTTPException(400, "The expected project is missing from the collaboration snapshot")
        incoming_format = await _check_newer_format(src)

        await dst.execute("BEGIN IMMEDIATE")
        target = await (
            await dst.execute("SELECT * FROM project WHERE id=?", (target_project_id,))
        ).fetchone()
        if not target:
            raise HTTPException(404, "Conflict copy no longer exists")
        await refuse_while_run_in_progress(
            dst, [target_project_id], f"update the reference copy “{target['name']}”"
        )
        await _check_older_format(dst, incoming_format, target)

        kept_embeddings = await _set_embeddings_aside(dst, target_project_id)
        await _delete_project_contents(dst, target_project_id)
        await dst.execute(
            "DELETE FROM project_snapshot WHERE project_id=?", (target_project_id,)
        )
        await dst.execute(
            "UPDATE project SET description=?, modified_at=datetime('now'), "
            "deleted_at=NULL, revision=revision+1, head_snapshot_id=NULL, "
            "shared_folder=NULL, shared_last_published_revision=NULL, "
            "shared_last_snapshot_id=NULL, shared_last_sync_at=NULL WHERE id=?",
            (
                _value(source_project, "description", "") or "",
                target_project_id,
            ),
        )
        await _copy_project_contents(src, dst, source_project, target_project_id, [])
        await _restore_embeddings(dst, target_project_id, kept_embeddings)

        from aqda.services.offsets import repair_legacy_offsets

        await repair_legacy_offsets(dst, [target_project_id])
        await dst.commit()
        refreshed = await (
            await dst.execute("SELECT * FROM project WHERE id=?", (target_project_id,))
        ).fetchone()
        return dict(refreshed)
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
