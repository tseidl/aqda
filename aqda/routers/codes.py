"""Code management routes — hierarchical qualitative codes."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db, touch_project

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
        project = await (
            await db.execute("SELECT id FROM project WHERE id=?", (data.project_id,))
        ).fetchone()
        if not project:
            raise HTTPException(404, "Project not found")
        if data.parent_id is not None:
            parent = await (
                await db.execute(
                    "SELECT project_id, deleted_at FROM code WHERE id=?", (data.parent_id,)
                )
            ).fetchone()
            if not parent or parent["deleted_at"] is not None:
                raise HTTPException(404, "Parent code not found")
            if parent["project_id"] != data.project_id:
                raise HTTPException(400, "Parent code must belong to the same project")
        cursor = await db.execute(
            "INSERT INTO code (project_id, parent_id, name, description, color) "
            "VALUES (?, ?, ?, ?, ?)",
            (data.project_id, data.parent_id, data.name, data.description, data.color),
        )
        await touch_project(db, data.project_id)
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
        current = await (
            await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
        ).fetchone()
        if not current:
            raise HTTPException(404, "Code not found")
        # exclude_unset (not exclude_none) so an explicit parent_id=null — moving a
        # code back to the top level — is applied rather than silently dropped.
        fields = data.model_dump(exclude_unset=True)

        # Prevent circular hierarchy
        if "parent_id" in fields:
            new_parent_id = fields["parent_id"]
            if new_parent_id is not None:
                parent = await (
                    await db.execute(
                        "SELECT project_id, deleted_at FROM code WHERE id=?", (new_parent_id,)
                    )
                ).fetchone()
                if not parent or parent["deleted_at"] is not None:
                    raise HTTPException(404, "Parent code not found")
                if parent["project_id"] != current["project_id"]:
                    raise HTTPException(400, "Parent code must belong to the same project")
            if await _would_create_cycle(db, code_id, fields["parent_id"]):
                raise HTTPException(400, "Cannot set parent: would create a circular hierarchy")

        updates = []
        values = []
        for field, val in fields.items():
            if field not in ALLOWED_CODE_FIELDS:
                continue
            if current[field] == val:
                continue
            updates.append(f"{field}=?")
            values.append(val)
        if not updates:
            return dict(current)
        values.append(code_id)
        await db.execute(
            f"UPDATE code SET {', '.join(updates)} WHERE id=?", values
        )
        cursor = await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
        row = await cursor.fetchone()
        await touch_project(db, row["project_id"])
        await db.commit()
        return dict(row)
    finally:
        await db.close()


@router.delete("/{code_id}", status_code=204)
async def delete_code(code_id: int):
    """Soft-delete a complete code subtree and its currently active codings."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT project_id, deleted_at FROM code WHERE id=?", (code_id,)
        )
        code = await cursor.fetchone()
        if not code or code["deleted_at"] is not None:
            raise HTTPException(404, "Code not found")
        rows = await (
            await db.execute(
                "WITH RECURSIVE subtree(id) AS ("
                "SELECT id FROM code WHERE id=? UNION ALL "
                "SELECT c.id FROM code c JOIN subtree s ON c.parent_id=s.id"
                ") SELECT id FROM subtree",
                (code_id,),
            )
        ).fetchall()
        code_ids = [row["id"] for row in rows]
        placeholders = ",".join("?" * len(code_ids))
        batch_id = str(uuid.uuid4())
        deleted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
        await db.execute(
            "INSERT INTO code_deletion_batch (id, project_id, root_code_id, deleted_at) "
            "VALUES (?, ?, ?, ?)",
            (batch_id, code["project_id"], code_id, deleted_at),
        )
        await db.execute(
            f"UPDATE code SET deleted_at=?, deletion_batch_id=? "
            f"WHERE id IN ({placeholders}) AND deleted_at IS NULL",
            [deleted_at, batch_id, *code_ids],
        )
        await db.execute(
            f"UPDATE coding SET deleted_at=?, deletion_batch_id=? "
            f"WHERE code_id IN ({placeholders}) AND deleted_at IS NULL",
            [deleted_at, batch_id, *code_ids],
        )
        await touch_project(db, code["project_id"])
        await db.commit()
    finally:
        await db.close()


@router.post("/{code_id}/restore", status_code=200)
async def restore_code(code_id: int):
    """Restore exactly the code subtree and codings from its deletion operation."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT project_id, deleted_at, deletion_batch_id FROM code WHERE id=?", (code_id,)
        )
        code = await cursor.fetchone()
        if not code:
            raise HTTPException(404, "Code not found")
        batch_id = code["deletion_batch_id"]
        if batch_id:
            await db.execute(
                "UPDATE code SET deleted_at=NULL, deletion_batch_id=NULL "
                "WHERE deletion_batch_id=? AND project_id=?",
                (batch_id, code["project_id"]),
            )
            await db.execute(
                "UPDATE coding SET deleted_at=NULL, deletion_batch_id=NULL "
                "WHERE deletion_batch_id=? AND code_id IN "
                "(SELECT id FROM code WHERE project_id=?)",
                (batch_id, code["project_id"]),
            )
            await db.execute(
                "DELETE FROM code_deletion_batch WHERE id=? AND project_id=?",
                (batch_id, code["project_id"]),
            )
        else:
            # Legacy deletion: restore only codings stamped at the same moment.
            await db.execute("UPDATE code SET deleted_at=NULL WHERE id=?", (code_id,))
            await db.execute(
                "UPDATE coding SET deleted_at=NULL WHERE code_id=? AND deleted_at=?",
                (code_id, code["deleted_at"]),
            )
        await touch_project(db, code["project_id"])
        await db.commit()
        cursor = await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Code not found")
        return dict(row)
    finally:
        await db.close()


@router.get("/{code_id}/delete-impact")
async def code_delete_impact(code_id: int):
    db = await get_db()
    try:
        code = await (
            await db.execute(
                "SELECT id, name FROM code WHERE id=? AND deleted_at IS NULL", (code_id,)
            )
        ).fetchone()
        if not code:
            raise HTTPException(404, "Code not found")
        row = await (
            await db.execute(
                "WITH RECURSIVE subtree(id) AS ("
                "SELECT id FROM code WHERE id=? UNION ALL "
                "SELECT c.id FROM code c JOIN subtree s ON c.parent_id=s.id "
                "WHERE c.deleted_at IS NULL"
                ") SELECT COUNT(*) AS code_count, "
                "(SELECT COUNT(*) FROM coding WHERE code_id IN (SELECT id FROM subtree) "
                "AND deleted_at IS NULL) AS coding_count FROM subtree",
                (code_id,),
            )
        ).fetchone()
        return {
            "name": code["name"],
            "code_count": row["code_count"],
            "child_count": max(0, row["code_count"] - 1),
            "coding_count": row["coding_count"],
        }
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
            "FROM code c LEFT JOIN code_deletion_batch b ON b.id=c.deletion_batch_id "
            "WHERE c.project_id=? AND c.deleted_at IS NOT NULL "
            "AND (c.deletion_batch_id IS NULL OR b.root_code_id=c.id) "
            "ORDER BY c.deleted_at DESC",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
