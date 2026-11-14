"""Coding field routes — per-project questions answered for each coding."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db, touch_project
from aqda.services.coding_fields import (
    CodingFieldError,
    check_field_kind,
    clean_field_name,
    normalize_field_value,
)

router = APIRouter()

ALLOWED_CODING_FIELD_FIELDS = {"name", "kind", "sort_order"}

# A field with the number of active codings that answer it.
_FIELD_SELECT = (
    "SELECT f.*, (SELECT COUNT(*) FROM coding_field_value v "
    "JOIN coding cg ON cg.id=v.coding_id "
    "WHERE v.field_id=f.id AND cg.deleted_at IS NULL) AS value_count "
    "FROM coding_field f "
)


class CodingFieldCreate(BaseModel):
    project_id: int
    name: str
    kind: str = "text"
    sort_order: int | None = None


class CodingFieldUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    sort_order: int | None = None


# Refuse a name that another field of the project already uses (ignoring case).
async def _check_name_free(db, project_id: int, name: str, field_id: int | None = None):
    rows = await (
        await db.execute(
            "SELECT id, name FROM coding_field WHERE project_id=?", (project_id,)
        )
    ).fetchall()
    if any(row["id"] != field_id and row["name"].casefold() == name.casefold() for row in rows):
        raise HTTPException(409, f"This project already has a coding field named “{name}”.")


# Load one field as the list returns it.
async def _field_response(db, field_id: int) -> dict:
    row = await (await db.execute(f"{_FIELD_SELECT}WHERE f.id=?", (field_id,))).fetchone()
    return dict(row)


@router.get("")
async def list_coding_fields(project_id: int):
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                f"{_FIELD_SELECT}WHERE f.project_id=? ORDER BY f.sort_order, f.id",
                (project_id,),
            )
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()


@router.post("", status_code=201)
async def create_coding_field(data: CodingFieldCreate):
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        project = await (
            await db.execute(
                "SELECT id FROM project WHERE id=? AND deleted_at IS NULL", (data.project_id,)
            )
        ).fetchone()
        if not project:
            raise HTTPException(404, "Project not found")
        try:
            name = clean_field_name(data.name)
            kind = check_field_kind(data.kind)
        except CodingFieldError as exc:
            raise HTTPException(422, str(exc)) from exc
        await _check_name_free(db, data.project_id, name)
        sort_order = data.sort_order
        if sort_order is None:
            row = await (
                await db.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM coding_field "
                    "WHERE project_id=?",
                    (data.project_id,),
                )
            ).fetchone()
            sort_order = row[0]
        cursor = await db.execute(
            "INSERT INTO coding_field (project_id, name, kind, sort_order) VALUES (?, ?, ?, ?)",
            (data.project_id, name, kind, sort_order),
        )
        field_id = cursor.lastrowid
        await touch_project(db, data.project_id)
        await db.commit()
        return await _field_response(db, field_id)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.patch("/{field_id}")
async def update_coding_field(field_id: int, data: CodingFieldUpdate):
    """Rename, reorder, or change the kind of a field.

    A new kind is accepted only when every existing answer can be read in it; the answers
    are then stored in the new kind's form (for example "yes" becomes "1").
    """
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        current = await (
            await db.execute("SELECT * FROM coding_field WHERE id=?", (field_id,))
        ).fetchone()
        if not current:
            raise HTTPException(404, "Coding field not found")
        changes = {
            key: value
            for key, value in data.model_dump(exclude_none=True).items()
            if key in ALLOWED_CODING_FIELD_FIELDS
        }
        try:
            if "name" in changes:
                changes["name"] = clean_field_name(changes["name"])
            if "kind" in changes:
                changes["kind"] = check_field_kind(changes["kind"])
        except CodingFieldError as exc:
            raise HTTPException(422, str(exc)) from exc
        changes = {key: value for key, value in changes.items() if current[key] != value}
        if not changes:
            await db.rollback()
            return await _field_response(db, field_id)
        if "name" in changes:
            await _check_name_free(db, current["project_id"], changes["name"], field_id)

        converted: list[tuple[str, int]] = []
        if "kind" in changes:
            rows = await (
                await db.execute(
                    "SELECT rowid, value FROM coding_field_value WHERE field_id=?", (field_id,)
                )
            ).fetchall()
            unreadable = 0
            for row in rows:
                try:
                    value = normalize_field_value(changes["kind"], current["name"], row["value"])
                except CodingFieldError:
                    unreadable += 1
                    continue
                if value != row["value"]:
                    converted.append((value, row["rowid"]))
            if unreadable:
                raise HTTPException(
                    409,
                    f"{unreadable} existing answer(s) of “{current['name']}” do not fit the "
                    "new kind. Change or remove them first.",
                )

        assignments = ", ".join(f"{key}=?" for key in changes)
        await db.execute(
            f"UPDATE coding_field SET {assignments} WHERE id=?", [*changes.values(), field_id]
        )
        await db.executemany(
            "UPDATE coding_field_value SET value=? WHERE rowid=?", converted
        )
        await touch_project(db, current["project_id"])
        await db.commit()
        return await _field_response(db, field_id)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.delete("/{field_id}", status_code=204)
async def delete_coding_field(field_id: int):
    """Delete a field together with all its answers."""
    db = await get_db()
    try:
        current = await (
            await db.execute("SELECT project_id FROM coding_field WHERE id=?", (field_id,))
        ).fetchone()
        if not current:
            raise HTTPException(404, "Coding field not found")
        await db.execute("DELETE FROM coding_field_value WHERE field_id=?", (field_id,))
        await db.execute("DELETE FROM coding_field WHERE id=?", (field_id,))
        await touch_project(db, current["project_id"])
        await db.commit()
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.get("/{field_id}/suggestions")
async def coding_field_suggestions(field_id: int, q: str = "", limit: int = 20):
    """Earlier answers of a text field, most used first, optionally containing ``q``."""
    db = await get_db()
    try:
        field = await (
            await db.execute("SELECT kind FROM coding_field WHERE id=?", (field_id,))
        ).fetchone()
        if not field:
            raise HTTPException(404, "Coding field not found")
        if field["kind"] != "text":
            return []
        needle = q.strip().casefold()
        rows = await (
            await db.execute(
                "SELECT v.value, COUNT(*) AS uses FROM coding_field_value v "
                "JOIN coding cg ON cg.id=v.coding_id "
                "WHERE v.field_id=? AND cg.deleted_at IS NULL "
                "GROUP BY v.value ORDER BY uses DESC, v.value",
                (field_id,),
            )
        ).fetchall()
        values = [row["value"] for row in rows if needle in row["value"].casefold()]
        return values[: max(1, min(limit, 200))]
    finally:
        await db.close()
