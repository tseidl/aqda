"""Coding routes — text segment annotations."""

from fastapi import APIRouter
from pydantic import BaseModel

from aqda.db import get_db

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
        cursor = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text) "
            "VALUES (?, ?, ?, ?, ?)",
            (data.document_id, data.code_id, data.start_pos, data.end_pos, data.selected_text),
        )
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
    finally:
        await db.close()


@router.delete("/{coding_id}", status_code=204)
async def delete_coding(coding_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM coding WHERE id=?", (coding_id,))
        await db.commit()
    finally:
        await db.close()
