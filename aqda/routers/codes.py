"""Code management routes — hierarchical qualitative codes."""

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda import db as db_module
from aqda.db import get_db, touch_project
from aqda.services.coding_fields import field_answers_by_id, find_duplicate_coding

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
        # Only active codings join the batch. Codings already removed (one by one, or
        # with their model run) keep their own stamp, so restoring this code cannot bring
        # them back; a removed run hands its codings to this batch when it is restored.
        await db.execute(
            f"UPDATE coding SET deleted_at=?, deletion_batch_id=? "
            f"WHERE code_id IN ({placeholders}) AND deleted_at IS NULL",
            [deleted_at, batch_id, *code_ids],
        )
        await touch_project(db, code["project_id"])
        await db.commit()
    finally:
        await db.close()


# The deletion stamp a restored code's coding takes: NULL (active) for a hand coding or a
# coding of a model run that is in place, the run's own removal stamp when that run is
# removed. Codings removed with a code keep that code's batch even if their run is removed
# later, so the run's state is only settled here.
_RUN_REMOVAL_STAMP_SQL = (
    "(SELECT r.deleted_at FROM coding_run r WHERE r.id=coding.run_id)"
)


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
        if code["deleted_at"] is None:
            return dict(await (
                await db.execute("SELECT * FROM code WHERE id=?", (code_id,))
            ).fetchone())
        batch_id = code["deletion_batch_id"]
        if batch_id:
            await db.execute(
                "UPDATE code SET deleted_at=NULL, deletion_batch_id=NULL "
                "WHERE deletion_batch_id=? AND project_id=?",
                (batch_id, code["project_id"]),
            )
            # A coding from a model run that was removed meanwhile stays removed with
            # its run: it takes the run's removal stamp, so restoring the run brings it
            # back (see `_RUN_REMOVAL_STAMP_SQL`).
            await db.execute(
                f"UPDATE coding SET deleted_at={_RUN_REMOVAL_STAMP_SQL}, "
                "deletion_batch_id=NULL "
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
                f"UPDATE coding SET deleted_at={_RUN_REMOVAL_STAMP_SQL} "
                "WHERE code_id=? AND deleted_at=?",
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
    """List each code deletion once, with what restoring it brings back.

    A coding whose model run is removed stays out of use when its code is restored
    (`_RUN_REMOVAL_STAMP_SQL`), so it is not counted.
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT c.*, parent.name AS parent_name, "
            "CASE WHEN c.deletion_batch_id IS NULL "
            "THEN (SELECT COUNT(*) FROM coding WHERE code_id=c.id AND deleted_at=c.deleted_at "
            f"AND {_RUN_REMOVAL_STAMP_SQL} IS NULL) "
            "ELSE (SELECT COUNT(*) FROM coding WHERE deletion_batch_id=c.deletion_batch_id "
            f"AND {_RUN_REMOVAL_STAMP_SQL} IS NULL) "
            "END AS coding_count, "
            "CASE WHEN c.deletion_batch_id IS NULL THEN 0 "
            "ELSE (SELECT COUNT(*) FROM code WHERE deletion_batch_id=c.deletion_batch_id "
            "AND id<>c.id) END AS child_count "
            "FROM code c LEFT JOIN code_deletion_batch b ON b.id=c.deletion_batch_id "
            "LEFT JOIN code parent ON parent.id=c.parent_id "
            "WHERE c.project_id=? AND c.deleted_at IS NOT NULL "
            "AND (c.deletion_batch_id IS NULL OR b.root_code_id=c.id) "
            "ORDER BY c.deleted_at DESC",
            (project_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


# --- Merging codes ---


class CodeMerge(BaseModel):
    target_id: int


# Describe one code for merge responses.
def _code_summary(code) -> dict:
    return {"id": code["id"], "name": code["name"], "description": code["description"] or ""}


# Decide what happens to the source code's definition when it is merged into the target.
def _description_outcome(source, target) -> str:
    """Return "none", "carried_over", "same", or "saved_as_memo".

    A definition is never discarded: when both codes have different definitions, the
    target keeps its own and the source's definition is kept as a memo on the target.
    """
    source_text = (source["description"] or "").strip()
    target_text = (target["description"] or "").strip()
    if not source_text:
        return "none"
    if not target_text:
        return "carried_over"
    if source_text == target_text:
        return "same"
    return "saved_as_memo"


# Check that a merge is possible and work out everything it would do, without changing data.
async def _plan_merge(db, source_id: int, target_id: int) -> dict:
    """Return the merge plan; refuse impossible merges with a message for the researcher.

    Merge impact and the merge itself both use this plan, so the confirmation shows
    exactly what the merge then does.
    """
    if source_id == target_id:
        raise HTTPException(400, "A code cannot be merged into itself.")
    source = await (await db.execute("SELECT * FROM code WHERE id=?", (source_id,))).fetchone()
    target = await (await db.execute("SELECT * FROM code WHERE id=?", (target_id,))).fetchone()
    if not source or source["deleted_at"] is not None:
        raise HTTPException(404, "The code to merge was not found or is in the trash.")
    if not target or target["deleted_at"] is not None:
        raise HTTPException(404, "The code to merge into was not found or is in the trash.")
    if source["project_id"] != target["project_id"]:
        raise HTTPException(400, "Only codes of the same project can be merged.")
    if await _would_create_cycle(db, source_id, target_id):
        raise HTTPException(
            400,
            f"“{source['name']}” cannot be merged into one of its own child codes. "
            "Merge the child code into it instead, or move the child code first.",
        )

    source_codings = await (
        await db.execute(
            "SELECT id, document_id, start_pos, end_pos, run_id, coder, deleted_at, "
            "deletion_batch_id FROM coding WHERE code_id=? ORDER BY id",
            (source_id,),
        )
    ).fetchall()
    active = [row for row in source_codings if row["deleted_at"] is None]
    answers = await field_answers_by_id(db, [row["id"] for row in source_codings])
    # A source coding is a duplicate when the target already holds an equal coding
    # (shared rule: same document, span, run or hand coder, and field answers) in the same
    # state: both active, or both out of use with the same stamp and deletion batch, so
    # that a later restore of a run or code cannot bring back two equal codings. Other
    # codings move unchanged and come back as they were when restored.
    dropped: dict[int, int] = {}
    dropped_out_of_use = 0
    for row in source_codings:
        kept_id = await find_duplicate_coding(
            db,
            document_id=row["document_id"],
            code_id=target_id,
            start_pos=row["start_pos"],
            end_pos=row["end_pos"],
            run_id=row["run_id"],
            coder=row["coder"],
            answers=answers.get(row["id"], {}),
            deletion_state=(row["deleted_at"], row["deletion_batch_id"]),
        )
        if kept_id is not None:
            dropped[row["id"]] = kept_id
            dropped_out_of_use += row["deleted_at"] is not None

    relinked_memos = 0
    dropped_ids = list(dropped)
    for start in range(0, len(dropped_ids), 500):
        chunk = dropped_ids[start:start + 500]
        placeholders = ",".join("?" * len(chunk))
        row = await (
            await db.execute(
                f"SELECT COUNT(*) FROM memo WHERE coding_id IN ({placeholders})", chunk
            )
        ).fetchone()
        relinked_memos += row[0]
    code_memos = (
        await (
            await db.execute("SELECT COUNT(*) FROM memo WHERE code_id=?", (source_id,))
        ).fetchone()
    )[0]
    children = await (
        await db.execute(
            "SELECT id, deleted_at FROM code WHERE parent_id=? ORDER BY sort_order, name, id",
            (source_id,),
        )
    ).fetchall()
    return {
        "source": source,
        "target": target,
        "dropped": dropped,
        "child_ids": [row["id"] for row in children],
        "counts": {
            "codings_moved": len(active) - (len(dropped) - dropped_out_of_use),
            "trashed_codings_moved": len(source_codings) - len(active) - dropped_out_of_use,
            "duplicates_dropped": len(dropped),
            "memos_moved": code_memos,
            "passage_memos_relinked": relinked_memos,
            "children_moved": sum(1 for row in children if row["deleted_at"] is None),
            "trashed_children_moved": sum(1 for row in children if row["deleted_at"] is not None),
        },
        "description": _description_outcome(source, target),
    }


@router.get("/{code_id}/merge-impact")
async def code_merge_impact(code_id: int, target_id: int):
    """Report what merging this code into ``target_id`` would do, for the confirmation."""
    db = await get_db()
    try:
        plan = await _plan_merge(db, code_id, target_id)
        return {
            "source": _code_summary(plan["source"]),
            "target": _code_summary(plan["target"]),
            **plan["counts"],
            "description": plan["description"],
        }
    finally:
        await db.close()


@router.post("/{code_id}/merge")
async def merge_code(code_id: int, data: CodeMerge):
    """Merge this code into another one, after a verified backup; the source code is removed.

    Codings (also trashed ones, with their field answers), memos, and child codes (also
    trashed ones) move to the target. Only exact duplicates of a coding on the target
    that is in the same state (active, or out of use in the same way) are dropped; their
    memos move to the coding that is kept. There is no undo in the app, hence the backup
    (the newest merge backups are kept, see `db.create_merge_backup`).
    """
    target_id = data.target_id
    db = await get_db()
    try:
        # Refuse impossible merges before spending time on a backup.
        await _plan_merge(db, code_id, target_id)
        try:
            backup_path = await asyncio.to_thread(db_module.create_merge_backup)
        except Exception as exc:
            raise HTTPException(
                500, "AQDA could not make a backup before merging, so nothing was changed."
            ) from exc

        await db.execute("BEGIN IMMEDIATE")
        # Plan again under the write lock, so the merge matches the data as it is now.
        plan = await _plan_merge(db, code_id, target_id)
        source, target = plan["source"], plan["target"]
        project_id = target["project_id"]

        # Exact duplicates go; memos written on them move to the coding that is kept.
        await db.executemany(
            "UPDATE memo SET coding_id=? WHERE coding_id=?",
            [(kept_id, dropped_id) for dropped_id, kept_id in plan["dropped"].items()],
        )
        await db.executemany(
            "DELETE FROM coding WHERE id=?", [(dropped_id,) for dropped_id in plan["dropped"]]
        )
        # Every other coding moves as it is: deletion state, coder, run, answers, dates.
        await db.execute("UPDATE coding SET code_id=? WHERE code_id=?", (target_id, code_id))
        await db.execute("UPDATE memo SET code_id=? WHERE code_id=?", (target_id, code_id))

        # Child codes keep their order and follow the target's existing children.
        last = await (
            await db.execute(
                "SELECT COALESCE(MAX(sort_order), -1) FROM code WHERE parent_id=?", (target_id,)
            )
        ).fetchone()
        await db.executemany(
            "UPDATE code SET parent_id=?, sort_order=? WHERE id=?",
            [
                (target_id, last[0] + position, child_id)
                for position, child_id in enumerate(plan["child_ids"], start=1)
            ],
        )

        description_memo_id = None
        if plan["description"] == "carried_over":
            await db.execute(
                "UPDATE code SET description=? WHERE id=?", (source["description"], target_id)
            )
        elif plan["description"] == "saved_as_memo":
            cursor = await db.execute(
                "INSERT INTO memo (project_id, code_id, title, content) VALUES (?, ?, ?, ?)",
                (
                    project_id,
                    target_id,
                    f"Definition of the merged code “{source['name']}”",
                    source["description"],
                ),
            )
            description_memo_id = cursor.lastrowid

        # Nothing may keep pointing at the source code once it is gone.
        await db.execute(
            "UPDATE code_deletion_batch SET root_code_id=? WHERE root_code_id=?",
            (target_id, code_id),
        )
        await db.execute(
            "UPDATE coding_run SET proposed_parent_code_id=? WHERE proposed_parent_code_id=?",
            (target_id, code_id),
        )
        await db.execute("DELETE FROM code WHERE id=?", (code_id,))
        await touch_project(db, project_id)
        await db.commit()

        merged = await (
            await db.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM coding WHERE code_id=c.id "
                "AND deleted_at IS NULL) AS coding_count FROM code c WHERE c.id=?",
                (target_id,),
            )
        ).fetchone()
        return {
            "code": dict(merged),
            "source": _code_summary(source),
            **plan["counts"],
            "description": plan["description"],
            "description_memo_id": description_memo_id,
            "backup": backup_path.name,
        }
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()
