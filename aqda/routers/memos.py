"""Memo/notes routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db, touch_project

router = APIRouter()


class MemoCreate(BaseModel):
    project_id: int
    document_id: int | None = None
    code_id: int | None = None
    coding_id: int | None = None
    start_pos: int | None = None
    end_pos: int | None = None
    title: str = ""
    content: str = ""


class MemoUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


async def _validate_memo_references(db, data: MemoCreate) -> None:
    project = await (
        await db.execute("SELECT id FROM project WHERE id=?", (data.project_id,))
    ).fetchone()
    if not project:
        raise HTTPException(404, "Project not found")

    document = None
    if data.document_id is not None:
        document = await (
            await db.execute(
                "SELECT id, project_id, content, transcript, source_type FROM document WHERE id=?",
                (data.document_id,),
            )
        ).fetchone()
        if not document or document["project_id"] != data.project_id:
            raise HTTPException(400, "Memo document must belong to the same project")
    if data.code_id is not None:
        code = await (
            await db.execute("SELECT project_id FROM code WHERE id=?", (data.code_id,))
        ).fetchone()
        if not code or code["project_id"] != data.project_id:
            raise HTTPException(400, "Memo code must belong to the same project")
    if data.coding_id is not None:
        coding = await (
            await db.execute(
                "SELECT cg.document_id, d.project_id FROM coding cg "
                "JOIN document d ON d.id=cg.document_id WHERE cg.id=?",
                (data.coding_id,),
            )
        ).fetchone()
        if not coding or coding["project_id"] != data.project_id:
            raise HTTPException(400, "Memo coding must belong to the same project")
        if data.document_id is not None and coding["document_id"] != data.document_id:
            raise HTTPException(400, "Memo coding must belong to the selected document")
    if (data.start_pos is None) != (data.end_pos is None):
        raise HTTPException(422, "Memo boundaries must include both start and end")
    if data.start_pos is not None and data.end_pos is not None:
        if document is None:
            raise HTTPException(422, "Passage memos require a document")
        text = (
            document["transcript"] or ""
            if document["source_type"] == "audio"
            else document["content"] or ""
        )
        if data.start_pos < 0 or data.end_pos <= data.start_pos or data.end_pos > len(text):
            raise HTTPException(422, "Memo boundaries are outside the document text")


@router.get("")
async def list_memos(
    project_id: int | None = None,
    document_id: int | None = None,
    code_id: int | None = None,
):
    db = await get_db()
    try:
        conditions = []
        params = []
        if project_id:
            conditions.append("m.project_id=?")
            params.append(project_id)
        if document_id is not None:
            conditions.append("m.document_id=?")
            params.append(document_id)
        if code_id is not None:
            conditions.append("m.code_id=?")
            params.append(code_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        cursor = await db.execute(
            f"SELECT m.*, d.name as document_name, c.name as code_name "
            f"FROM memo m "
            f"LEFT JOIN document d ON d.id=m.document_id "
            f"LEFT JOIN code c ON c.id=m.code_id "
            f"{where} "
            f"ORDER BY m.modified_at DESC",
            params,
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("", status_code=201)
async def create_memo(data: MemoCreate):
    db = await get_db()
    try:
        await _validate_memo_references(db, data)
        cursor = await db.execute(
            "INSERT INTO memo (project_id, document_id, code_id, coding_id, start_pos, end_pos, "
            "title, content, offset_unit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'codepoint')",
            (data.project_id, data.document_id, data.code_id, data.coding_id,
             data.start_pos, data.end_pos, data.title, data.content),
        )
        await touch_project(db, data.project_id)
        await db.commit()
        memo_id = cursor.lastrowid
        cursor = await db.execute("SELECT * FROM memo WHERE id=?", (memo_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


ALLOWED_MEMO_FIELDS = {"title", "content"}


@router.patch("/{memo_id}")
async def update_memo(memo_id: int, data: MemoUpdate):
    db = await get_db()
    try:
        current = await (
            await db.execute("SELECT * FROM memo WHERE id=?", (memo_id,))
        ).fetchone()
        if not current:
            raise HTTPException(404, "Memo not found")
        updates = []
        values = []
        for field, val in data.model_dump(exclude_none=True).items():
            if field not in ALLOWED_MEMO_FIELDS:
                continue
            if current[field] == val:
                continue
            updates.append(f"{field}=?")
            values.append(val)
        if not updates:
            return dict(current)
        updates.append("modified_at=datetime('now')")
        values.append(memo_id)
        await db.execute(
            f"UPDATE memo SET {', '.join(updates)} WHERE id=?", values
        )
        cursor = await db.execute("SELECT * FROM memo WHERE id=?", (memo_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Memo not found")
        await touch_project(db, row["project_id"])
        await db.commit()
        return dict(row)
    finally:
        await db.close()


@router.delete("/{memo_id}", status_code=204)
async def delete_memo(memo_id: int):
    db = await get_db()
    try:
        cursor = await db.execute("SELECT project_id FROM memo WHERE id=?", (memo_id,))
        memo = await cursor.fetchone()
        await db.execute("DELETE FROM memo WHERE id=?", (memo_id,))
        if memo:
            await touch_project(db, memo["project_id"])
        await db.commit()
    finally:
        await db.close()
