"""Coding routes — text segment annotations."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db, touch_project

router = APIRouter()


class CodingCreate(BaseModel):
    document_id: int
    code_id: int
    start_pos: int
    end_pos: int
    selected_text: str


@router.get("")
async def list_codings(document_id: int | None = None, code_id: int | None = None, project_id: int | None = None):
    db = await get_db()
    try:
        conditions = []
        params = []
        if document_id:
            conditions.append("cg.document_id=?")
            params.append(document_id)
        if code_id:
            conditions.append("cg.code_id=?")
            params.append(code_id)
        if project_id:
            conditions.append("d.project_id=?")
            params.append(project_id)

        conditions.append("cg.deleted_at IS NULL")
        where = f"WHERE {' AND '.join(conditions)}"

        cursor = await db.execute(
            f"SELECT cg.*, c.name as code_name, c.color as code_color, "
            f"d.name as document_name "
            f"FROM coding cg "
            f"JOIN code c ON c.id=cg.code_id "
            f"JOIN document d ON d.id=cg.document_id "
            f"{where} "
            f"ORDER BY cg.document_id, cg.start_pos",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("", status_code=201)
async def create_coding(data: CodingCreate):
    db = await get_db()
    try:
        # Take the write lock first so the duplicate check and the insert are one
        # atomic step even when two tabs submit the same coding at the same time.
        await db.execute("BEGIN IMMEDIATE")
        document = await (
            await db.execute(
                "SELECT id, project_id, content, transcript, source_type FROM document WHERE id=?",
                (data.document_id,),
            )
        ).fetchone()
        code = await (
            await db.execute(
                "SELECT id, project_id, deleted_at FROM code WHERE id=?", (data.code_id,)
            )
        ).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        if not code or code["deleted_at"] is not None:
            raise HTTPException(404, "Code not found")
        if document["project_id"] != code["project_id"]:
            raise HTTPException(400, "The document and code must belong to the same project")
        text = (
            document["transcript"] or ""
            if document["source_type"] == "audio"
            else document["content"] or ""
        )
        if data.start_pos < 0 or data.end_pos <= data.start_pos or data.end_pos > len(text):
            raise HTTPException(422, "Coding boundaries are outside the document text")
        if text[data.start_pos:data.end_pos] != data.selected_text:
            raise HTTPException(
                422,
                "Selected text does not match the document at those boundaries. Reload and try again.",
            )

        # Two different codes on one passage are normal; the same code twice on the
        # exact same span is only ever an accidental double application.
        duplicate = await (
            await db.execute(
                "SELECT id FROM coding WHERE document_id=? AND code_id=? AND start_pos=? "
                "AND end_pos=? AND deleted_at IS NULL",
                (data.document_id, data.code_id, data.start_pos, data.end_pos),
            )
        ).fetchone()
        if duplicate:
            raise HTTPException(409, "This code is already applied to exactly this passage.")

        # Stamp the coding with the current coder identity (per-coding attribution).
        cursor = await db.execute("SELECT value FROM setting WHERE key='coder_name'")
        row = await cursor.fetchone()
        coder = (row["value"] if row else "") or ""
        cursor = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "coder, offset_unit) VALUES (?, ?, ?, ?, ?, ?, 'codepoint')",
            (data.document_id, data.code_id, data.start_pos, data.end_pos, data.selected_text, coder),
        )
        await touch_project(db, document["project_id"])
        await db.commit()
        coding_id = cursor.lastrowid
        cursor = await db.execute(
            "SELECT cg.*, c.name as code_name, c.color as code_color, "
            "d.name as document_name "
            "FROM coding cg "
            "JOIN code c ON c.id=cg.code_id "
            "JOIN document d ON d.id=cg.document_id "
            "WHERE cg.id=?",
            (coding_id,),
        )
        return dict(await cursor.fetchone())
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.get("/audit")
async def coding_audit(project_id: int | None = None):
    db = await get_db()
    try:
        conditions = ["cg.repair_status LIKE 'review_%'"]
        params = []
        if project_id is not None:
            conditions.append("d.project_id=?")
            params.append(project_id)
        rows = await (
            await db.execute(
                "SELECT cg.id, cg.document_id, cg.start_pos, cg.end_pos, cg.selected_text, "
                "cg.repair_status, d.name AS document_name, c.name AS code_name "
                "FROM coding cg JOIN document d ON d.id=cg.document_id "
                "JOIN code c ON c.id=cg.code_id WHERE " + " AND ".join(conditions),
                params,
            )
        ).fetchall()
        return {"count": len(rows), "items": [dict(row) for row in rows]}
    finally:
        await db.close()


@router.delete("/{coding_id}", status_code=204)
async def delete_coding(coding_id: int):
    # Soft-delete for consistency with code deletion and to keep it recoverable.
    # List queries filter `deleted_at IS NULL`, so it disappears from the UI.
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT d.project_id FROM coding cg "
            "JOIN document d ON d.id=cg.document_id WHERE cg.id=?",
            (coding_id,),
        )
        project = await cursor.fetchone()
        await db.execute(
            "UPDATE coding SET deleted_at=datetime('now') WHERE id=?", (coding_id,)
        )
        if project:
            await touch_project(db, project["project_id"])
        await db.commit()
    finally:
        await db.close()
