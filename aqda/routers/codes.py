"""Code management routes — hierarchical qualitative codes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db

router = APIRouter()

ALLOWED_CODE_FIELDS = {"name", "parent_id", "description", "color", "sort_order"}


class CodeCreate(BaseModel):
    project_id: int
    name: str
    parent_id: int | None = None
    description: str = ""
    color: str = "#6366f1"


class CodeUpdate(BaseModel):
    name: str | None = None
    parent_id: int | None = None
    description: str | None = None
    color: str | None = None
    sort_order: int | None = None


async def _would_create_cycle(db, code_id: int, new_parent_id: int | None) -> bool:
    """Check if setting parent_id would create a cycle in the code hierarchy."""
    if new_parent_id is None:
        return False
    if new_parent_id == code_id:
        return True
    visited = {code_id}
    current = new_parent_id
    while current is not None:
        if current in visited:
            return True
        visited.add(current)
        cursor = await db.execute("SELECT parent_id FROM code WHERE id=?", (current,))
        row = await cursor.fetchone()
        if not row:
            break
        current = row["parent_id"]
    return False


@router.get("")
async def list_codes(project_id: int):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM coding WHERE code_id=c.id AND deleted_at IS NULL) as coding_count "
            "FROM code c WHERE c.project_id=? AND c.deleted_at IS NULL "
            "ORDER BY c.parent_id NULLS FIRST, c.sort_order, c.name",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


@router.post("", status_code=201)
async def create_code(data: CodeCreate):
    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO code (project_id, parent_id, name, description, color) "
            "VALUES (?, ?, ?, ?, ?)",
            (data.project_id, data.parent_id, data.name, data.description, data.color),
        )
        await db.commit()
        code_id = cursor.lastrowid
        cursor = await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.patch("/{code_id}")
async def update_code(code_id: int, data: CodeUpdate):
    db = await get_db()
    try:
        fields = data.model_dump(exclude_none=True)

        # Prevent circular hierarchy
        if "parent_id" in fields:
            if await _would_create_cycle(db, code_id, fields["parent_id"]):
                raise HTTPException(400, "Cannot set parent: would create a circular hierarchy")

        updates = []
        values = []
        for field, val in fields.items():
            if field not in ALLOWED_CODE_FIELDS:
                continue
            updates.append(f"{field}=?")
            values.append(val)
        if not updates:
            raise HTTPException(400, "No fields to update")
        values.append(code_id)
        await db.execute(
            f"UPDATE code SET {', '.join(updates)} WHERE id=?", values
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.delete("/{code_id}", status_code=204)
async def delete_code(code_id: int):
    """Soft-delete a code and its codings."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE code SET deleted_at=datetime('now') WHERE id=?", (code_id,)
        )
        await db.execute(
            "UPDATE coding SET deleted_at=datetime('now') WHERE code_id=?", (code_id,)
        )
        await db.commit()
    finally:
        await db.close()


@router.post("/{code_id}/restore", status_code=200)
async def restore_code(code_id: int):
    """Restore a soft-deleted code and its codings."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE code SET deleted_at=NULL WHERE id=?", (code_id,)
        )
        await db.execute(
            "UPDATE coding SET deleted_at=NULL WHERE code_id=?", (code_id,)
        )
        await db.commit()
        cursor = await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Code not found")
        return dict(row)
    finally:
        await db.close()


@router.get("/trash")
async def list_deleted_codes(project_id: int):
    """List soft-deleted codes for a project."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT c.*, "
            "(SELECT COUNT(*) FROM coding WHERE code_id=c.id) as coding_count "
            "FROM code c WHERE c.project_id=? AND c.deleted_at IS NOT NULL "
            "ORDER BY c.deleted_at DESC",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
