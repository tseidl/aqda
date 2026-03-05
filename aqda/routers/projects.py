"""Project management routes."""

from datetime import datetime, timezone

import aiosqlite
from fastapi import APIRouter, HTTPException, UploadFile, File
from pydantic import BaseModel

from aqda.db import get_db

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
            "INSERT INTO project (name, description) VALUES (?, ?)",
            (data.name, data.description),
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
        updates = []
        values = []
        for field, val in data.model_dump(exclude_none=True).items():
            if field not in ALLOWED_PROJECT_FIELDS:
                continue
            updates.append(f"{field}=?")
            values.append(val)
        if not updates:
            raise HTTPException(400, "No fields to update")
        updates.append("modified_at=datetime('now')")
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
            "UPDATE project SET deleted_at=datetime('now') WHERE id=?", (project_id,)
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
            "UPDATE project SET deleted_at=NULL WHERE id=?", (project_id,)
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


@router.post("/import-db", status_code=201)
async def import_from_db(file: UploadFile = File(...)):
    """Import projects from an external AQDA .db file.

    Copies all projects, documents, codes, codings, memos, and document variables
    from the uploaded database into the current one. IDs are remapped.
    """
    import tempfile
    import os

    content = await file.read()
    # Write to temp file for aiosqlite to open
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.write(content)
    tmp.close()

    imported = []
    try:
        src = await aiosqlite.connect(tmp.name)
        src.row_factory = aiosqlite.Row
        dst = await get_db()

        try:
            # Get all projects from source
            cursor = await src.execute("SELECT * FROM project")
            projects = await cursor.fetchall()

            for proj in projects:
                # Create project in destination
                stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                imported_name = f"{proj['name']} (imported {stamp})"
                c = await dst.execute(
                    "INSERT INTO project (name, description, created_at, modified_at) VALUES (?, ?, ?, ?)",
                    (imported_name, proj["description"], proj["created_at"], proj["modified_at"]),
                )
                new_project_id = c.lastrowid

                # Copy documents, remap IDs
                cursor = await src.execute(
                    "SELECT * FROM document WHERE project_id=?", (proj["id"],)
                )
                docs = await cursor.fetchall()
                doc_map: dict[int, int] = {}  # old_id -> new_id
                for doc in docs:
                    # transcript column may not exist in older exports
                    transcript = None
                    try:
                        transcript = doc["transcript"]
                    except (IndexError, KeyError):
                        pass
                    c = await dst.execute(
                        "INSERT INTO document (project_id, name, content, source_type, transcript, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (new_project_id, doc["name"], doc["content"], doc["source_type"], transcript, doc["created_at"], doc["modified_at"]),
                    )
                    doc_map[doc["id"]] = c.lastrowid

                # Copy document variables
                for old_doc_id, new_doc_id in doc_map.items():
                    try:
                        cursor = await src.execute(
                            "SELECT key, value FROM document_variable WHERE document_id=?", (old_doc_id,)
                        )
                        doc_vars = await cursor.fetchall()
                        for dv in doc_vars:
                            await dst.execute(
                                "INSERT INTO document_variable (document_id, key, value) VALUES (?, ?, ?)",
                                (new_doc_id, dv["key"], dv["value"]),
                            )
                    except Exception:
                        pass  # Source DB may not have this table

                # Copy codes, remap IDs (handle hierarchy)
                cursor = await src.execute(
                    "SELECT * FROM code WHERE project_id=? ORDER BY parent_id NULLS FIRST",
                    (proj["id"],),
                )
                src_codes = await cursor.fetchall()
                code_map: dict[int, int] = {}
                for code in src_codes:
                    new_parent = code_map.get(code["parent_id"]) if code["parent_id"] else None
                    c = await dst.execute(
                        "INSERT INTO code (project_id, parent_id, name, description, color, sort_order, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (new_project_id, new_parent, code["name"], code["description"], code["color"], code["sort_order"], code["created_at"]),
                    )
                    code_map[code["id"]] = c.lastrowid

                # Copy codings
                cursor = await src.execute(
                    "SELECT * FROM coding WHERE document_id IN ({})".format(
                        ",".join("?" * len(doc_map))
                    ),
                    list(doc_map.keys()),
                ) if doc_map else None
                if cursor:
                    codings = await cursor.fetchall()
                    for coding in codings:
                        new_doc = doc_map.get(coding["document_id"])
                        new_code = code_map.get(coding["code_id"])
                        if new_doc and new_code:
                            await dst.execute(
                                "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                (new_doc, new_code, coding["start_pos"], coding["end_pos"], coding["selected_text"], coding["created_at"]),
                            )

                # Copy memos
                try:
                    cursor = await src.execute(
                        "SELECT * FROM memo WHERE project_id=?", (proj["id"],)
                    )
                    memos = await cursor.fetchall()
                    for memo in memos:
                        await dst.execute(
                            "INSERT INTO memo (project_id, document_id, code_id, title, content, created_at, modified_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (new_project_id, doc_map.get(memo["document_id"]), code_map.get(memo["code_id"]),
                             memo["title"], memo["content"], memo["created_at"], memo["modified_at"]),
                        )
                except Exception:
                    pass

                await dst.commit()
                imported.append({"id": new_project_id, "name": imported_name})

        finally:
            await src.close()
            await dst.close()
    finally:
        os.unlink(tmp.name)

    return {"imported": imported, "count": len(imported)}
