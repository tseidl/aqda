"""Coding routes — text segment annotations."""

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db, touch_project
from aqda.services.coding_fields import (
    CodingFieldError,
    field_answers_by_id,
    field_answers_by_name,
    find_duplicate_coding,
    project_fields,
    save_field_values,
    validate_field_values,
)
from aqda.services.document_text import codeable_text

router = APIRouter()

# Columns every coding response carries: the coding, its code and document, the run that
# made it (NULL for a hand coding, hence the LEFT JOIN), and, for a model coding the
# researcher accepted, the run it came from.
_CODING_SELECT = (
    "SELECT cg.*, c.name as code_name, c.color as code_color, "
    "d.name as document_name, r.label as run_label, r.model as run_model, "
    "ar.label as accepted_from_run_label, ar.model as accepted_from_run_model "
    "FROM coding cg "
    "JOIN code c ON c.id=cg.code_id "
    "JOIN document d ON d.id=cg.document_id "
    "LEFT JOIN coding_run r ON r.id=cg.run_id "
    "LEFT JOIN coding_run ar ON ar.id=cg.accepted_from_run_id "
)


class CodingCreate(BaseModel):
    document_id: int
    code_id: int
    start_pos: int
    end_pos: int
    selected_text: str
    fields: dict[str, Any] | None = None


class CodingFieldsUpdate(BaseModel):
    fields: dict[str, Any] = {}


# Attach each coding's field answers (by field name) to its response row.
async def _with_fields(db, rows) -> list[dict]:
    codings = [dict(row) for row in rows]
    answers = await field_answers_by_name(db, [coding["id"] for coding in codings])
    for coding in codings:
        coding["fields"] = answers.get(coding["id"], {})
    return codings


# Load one coding in the shape the list returns.
async def _coding_response(db, coding_id: int) -> dict:
    cursor = await db.execute(f"{_CODING_SELECT}WHERE cg.id=?", (coding_id,))
    return (await _with_fields(db, [await cursor.fetchone()]))[0]


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
            f"{_CODING_SELECT}{where} ORDER BY cg.document_id, cg.start_pos",
            params,
        )
        return await _with_fields(db, await cursor.fetchall())
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
        text = codeable_text(document)
        if data.start_pos < 0 or data.end_pos <= data.start_pos or data.end_pos > len(text):
            raise HTTPException(422, "Coding boundaries are outside the document text")
        if text[data.start_pos:data.end_pos] != data.selected_text:
            raise HTTPException(
                422,
                "Selected text does not match the document at those boundaries. Reload and try again.",
            )
        try:
            answers = await validate_field_values(db, document["project_id"], data.fields)
        except CodingFieldError as exc:
            raise HTTPException(422, str(exc)) from exc

        # Stamp the coding with the current coder identity (per-coding attribution).
        cursor = await db.execute("SELECT value FROM setting WHERE key='coder_name'")
        row = await cursor.fetchone()
        coder = (row["value"] if row else "") or ""

        # Several codings on one passage are normal (other codes, other coders, other
        # field answers); only an exact repetition is an accidental double application.
        duplicate = await find_duplicate_coding(
            db,
            document_id=data.document_id,
            code_id=data.code_id,
            start_pos=data.start_pos,
            end_pos=data.end_pos,
            run_id=None,
            coder=coder,
            answers=answers,
        )
        if duplicate:
            if await project_fields(db, document["project_id"]):
                raise HTTPException(
                    409,
                    "This code is already applied to this passage with exactly these answers.",
                )
            raise HTTPException(409, "This code is already applied to exactly this passage.")

        cursor = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "coder, offset_unit) VALUES (?, ?, ?, ?, ?, ?, 'codepoint')",
            (data.document_id, data.code_id, data.start_pos, data.end_pos, data.selected_text, coder),
        )
        coding_id = cursor.lastrowid
        await save_field_values(db, coding_id, answers)
        await touch_project(db, document["project_id"])
        await db.commit()
        return await _coding_response(db, coding_id)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.put("/{coding_id}/fields")
async def replace_coding_fields(coding_id: int, data: CodingFieldsUpdate):
    """Replace the field answers of a hand coding; fields left out become unanswered."""
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        coding = await (
            await db.execute(
                "SELECT cg.*, d.project_id FROM coding cg "
                "JOIN document d ON d.id=cg.document_id "
                "WHERE cg.id=? AND cg.deleted_at IS NULL",
                (coding_id,),
            )
        ).fetchone()
        if not coding:
            raise HTTPException(404, "Coding not found")
        if coding["run_id"] is not None:
            # A model's answers stay as the run recorded them until the researcher
            # accepts the coding, which makes it their own.
            raise HTTPException(
                409,
                "The answers of a model coding cannot be changed. Accept the coding first; "
                "it is then yours to change.",
            )
        try:
            answers = await validate_field_values(db, coding["project_id"], data.fields)
        except CodingFieldError as exc:
            raise HTTPException(422, str(exc)) from exc
        duplicate = await find_duplicate_coding(
            db,
            document_id=coding["document_id"],
            code_id=coding["code_id"],
            start_pos=coding["start_pos"],
            end_pos=coding["end_pos"],
            run_id=None,
            coder=coding["coder"],
            answers=answers,
            exclude_coding_id=coding_id,
        )
        if duplicate:
            raise HTTPException(
                409, "Another coding of this passage already has exactly these answers."
            )
        await save_field_values(db, coding_id, answers)
        await touch_project(db, coding["project_id"])
        await db.commit()
        return await _coding_response(db, coding_id)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.post("/{coding_id}/accept")
async def accept_coding(coding_id: int):
    """Accept a model coding: it becomes the researcher's own coding.

    The coding keeps its passage, code, and answers, takes the current coder name, and
    leaves its run (`run_id` becomes NULL), so everything treats it as a hand coding from
    then on: it stays when the run is removed and its answers can be changed.
    `accepted_from_run_id` remembers where it came from.
    """
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        coding = await (
            await db.execute(
                "SELECT cg.*, d.project_id FROM coding cg "
                "JOIN document d ON d.id=cg.document_id "
                "WHERE cg.id=? AND cg.deleted_at IS NULL",
                (coding_id,),
            )
        ).fetchone()
        if not coding:
            raise HTTPException(404, "Coding not found")
        if coding["run_id"] is None:
            raise HTTPException(409, "Only a model coding can be accepted.")

        cursor = await db.execute("SELECT value FROM setting WHERE key='coder_name'")
        row = await cursor.fetchone()
        coder = (row["value"] if row else "") or ""

        # The researcher may have coded the same passage independently; two identical
        # hand codings by one coder would be an accidental double application.
        answers = (await field_answers_by_id(db, [coding_id])).get(coding_id, {})
        duplicate = await find_duplicate_coding(
            db,
            document_id=coding["document_id"],
            code_id=coding["code_id"],
            start_pos=coding["start_pos"],
            end_pos=coding["end_pos"],
            run_id=None,
            coder=coder,
            answers=answers,
            exclude_coding_id=coding_id,
        )
        if duplicate:
            raise HTTPException(
                409,
                "You have already coded this passage with this code yourself. Remove the "
                "model coding if you no longer need it.",
            )
        await db.execute(
            "UPDATE coding SET run_id=NULL, accepted_from_run_id=?, coder=? WHERE id=?",
            (coding["run_id"], coder, coding_id),
        )
        await touch_project(db, coding["project_id"])
        await db.commit()
        return await _coding_response(db, coding_id)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.delete("/{coding_id}", status_code=204)
async def delete_coding(coding_id: int):
    # Soft-delete for consistency with code deletion and to keep it recoverable.
    # List queries filter `deleted_at IS NULL`, so it disappears from the UI.
    # A coding removed on its own leaves any code deletion batch, and its stamp (whole
    # seconds) never equals a model run's removal stamp (microseconds), so neither
    # restoring a code nor restoring a run brings it back.
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT d.project_id FROM coding cg "
            "JOIN document d ON d.id=cg.document_id WHERE cg.id=?",
            (coding_id,),
        )
        project = await cursor.fetchone()
        await db.execute(
            "UPDATE coding SET deleted_at=datetime('now'), deletion_batch_id=NULL WHERE id=?",
            (coding_id,),
        )
        if project:
            await touch_project(db, project["project_id"])
        await db.commit()
    finally:
        await db.close()
