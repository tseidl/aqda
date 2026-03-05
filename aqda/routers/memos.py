"""Memo/notes routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db

router = APIRouter()


class MemoCreate(BaseModel):
    project_id: int
    document_id: int | None = None
    code_id: int | None = None
    coding_id: int | None = None
    title: str = ""
    content: str = ""


class MemoUpdate(BaseModel):
    title: str | None = None
    content: str | None = None


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
        cursor = await db.execute(
            "INSERT INTO memo (project_id, document_id, code_id, coding_id, title, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (data.project_id, data.document_id, data.code_id, data.coding_id, data.title, data.content),
        )
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
        updates = []
        values = []
        for field, val in data.model_dump(exclude_none=True).items():
            if field not in ALLOWED_MEMO_FIELDS:
                continue
            updates.append(f"{field}=?")
            values.append(val)
        if not updates:
            raise HTTPException(400, "No fields to update")
        updates.append("modified_at=datetime('now')")
        values.append(memo_id)
        await db.execute(
            f"UPDATE memo SET {', '.join(updates)} WHERE id=?", values
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM memo WHERE id=?", (memo_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.delete("/{memo_id}", status_code=204)
async def delete_memo(memo_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM memo WHERE id=?", (memo_id,))
        await db.commit()
    finally:
        await db.close()
