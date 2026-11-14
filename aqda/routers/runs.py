"""Model coding runs: programs outside AQDA code documents through these routes.

A program (the "driver") starts a run, reads each document's text, asks a language model,
and sends back verbatim quotes with a code. AQDA places every quote itself
(`aqda/services/anchoring.py`), so each coding is an exact span of the document; a quote
that cannot be placed uniquely is refused and recorded with its reason.

Runs are addressed by their uuid, which stays the same across snapshots. A conflict copy
in the same database shares it, so every `{run}` route takes an optional `project_id`.

Removing a run takes its codings out of use and restoring it brings them back, together
with code deletion and restore keeping one rule: a coding is active exactly when it was
not removed on its own, its code is not in the trash, and its run is not removed.
"""

import asyncio
import json
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from aqda.db import get_db, touch_project
from aqda.routers.codings import _CODING_SELECT, _with_fields
from aqda.services.anchoring import AnchorText, cuts_character, same_text
from aqda.services.coding_fields import (
    CodingFieldError,
    find_duplicate_coding,
    save_field_values,
    validate_field_values,
)
from aqda.services.coding_runs import next_code_color
from aqda.services.document_text import codeable_text, text_hash

router = APIRouter()

RUN_STATUSES = ("running", "finished", "stopped")
FINISHED_STATUSES = ("finished", "stopped")
# Columns of a run that the routes below may change.
ALLOWED_RUN_FIELDS = {
    "status",
    "finished_at",
    "last_activity_at",
    "deleted_at",
    "proposed_parent_code_id",
}
MAX_LABEL_LENGTH = 200
MAX_CODE_NAME_LENGTH = 200
POSITION = "position"

# A run with its counts: documents read, active codings (without those the researcher
# accepted, which are hand codings from then on), accepted codings, refused quotes,
# passages sent,
# and the codings that restoring a removed run would bring back.
_RUN_SELECT = (
    "SELECT r.*, "
    "(SELECT COUNT(*) FROM coding_run_document rd WHERE rd.run_id=r.id) AS documents_read, "
    "(SELECT COUNT(*) FROM coding c WHERE c.run_id=r.id AND c.deleted_at IS NULL) "
    "AS codings, "
    "(SELECT COUNT(*) FROM coding c WHERE c.accepted_from_run_id=r.id "
    "AND c.deleted_at IS NULL) AS codings_accepted, "
    "(SELECT COALESCE(SUM(CASE WHEN json_valid(rd.refused) "
    "THEN json_array_length(rd.refused) ELSE 0 END), 0) "
    "FROM coding_run_document rd WHERE rd.run_id=r.id) AS refused, "
    "(SELECT COALESCE(SUM(rd.submitted), 0) FROM coding_run_document rd "
    "WHERE rd.run_id=r.id) AS passages_submitted, "
    "(SELECT COUNT(*) FROM coding c WHERE c.run_id=r.id AND r.deleted_at IS NOT NULL "
    "AND c.deleted_at=r.deleted_at AND c.deletion_batch_id IS NULL) "
    "AS codings_removed_with_run "
    "FROM coding_run r JOIN project p ON p.id=r.project_id "
)


class RunCreate(BaseModel):
    project_id: int
    label: str
    model: str = ""
    instructions: str = ""
    settings: dict[str, Any] = {}
    allow_new_codes: bool = False
    note: str = ""


class RunFinish(BaseModel):
    status: str = "finished"


class DocumentSubmission(BaseModel):
    text_hash: str | None = None
    raw_response: str | None = None
    # Checked passage by passage, so one malformed passage is refused on its own.
    passages: list[Any] = []


# Parse a JSON column, falling back to a default for empty or unreadable values.
def _json_or(value, default):
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# Shape a run row for responses.
def _run_response(row) -> dict:
    run = dict(row)
    run["settings"] = _json_or(run.get("settings"), {})
    run["codebook"] = _json_or(run.get("codebook"), [])
    run["allow_new_codes"] = bool(run.get("allow_new_codes"))
    run["removed"] = run.get("deleted_at") is not None
    return run


# A removal stamp with microseconds; single-coding deletions use whole seconds, so the two
# can never be equal.
def _removal_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


# Collapse whitespace in a code name for comparisons.
def _clean_name(name: str) -> str:
    return " ".join(name.split())


# Find the one run a uuid (and optional project) refers to, among non-deleted projects.
async def _find_run(db, run_uuid: str, project_id: int | None):
    sql = f"{_RUN_SELECT}WHERE r.uuid=? AND p.deleted_at IS NULL"
    params: list[Any] = [run_uuid]
    if project_id is not None:
        sql += " AND r.project_id=?"
        params.append(project_id)
    rows = await (await db.execute(sql, params)).fetchall()
    if not rows:
        raise HTTPException(404, "Model run not found")
    if len(rows) > 1:
        raise HTTPException(
            409,
            "This model run exists in more than one project (a project and a copy of it). "
            "Add project_id to say which one.",
        )
    return rows[0]


# Load a run by its row ID in the response shape.
async def _run_by_id(db, run_id: int) -> dict:
    row = await (await db.execute(f"{_RUN_SELECT}WHERE r.id=?", (run_id,))).fetchone()
    return _run_response(row)


# Change whitelisted columns of a run.
async def _update_run(db, run_id: int, **fields) -> None:
    columns = [key for key in fields if key in ALLOWED_RUN_FIELDS]
    if not columns:
        return
    await db.execute(
        f"UPDATE coding_run SET {', '.join(f'{key}=?' for key in columns)} WHERE id=?",
        [*(fields[key] for key in columns), run_id],
    )


# Active codes of a project grouped by parent (a code under a missing parent counts as top
# level), in the order the code tree shows them.
async def _active_code_tree(db, project_id: int) -> dict[int | None, list[dict]]:
    rows = await (
        await db.execute(
            "SELECT id, parent_id, name, description FROM code "
            "WHERE project_id=? AND deleted_at IS NULL ORDER BY sort_order, name, id",
            (project_id,),
        )
    ).fetchall()
    ids = {row["id"] for row in rows}
    children: dict[int | None, list[dict]] = {}
    for row in rows:
        parent = row["parent_id"] if row["parent_id"] in ids else None
        children.setdefault(parent, []).append(dict(row))
    return children


# Capture the codebook at the start of a run: path, name, and definition of every active
# code. The ID is the code's ID at that moment and is never remapped later.
def _codebook(children: dict[int | None, list[dict]]) -> list[dict]:
    entries: list[dict] = []

    def visit(parent: int | None, path: list[str]) -> None:
        for code in children.get(parent, []):
            code_path = [*path, code["name"]]
            entries.append({
                "path": code_path,
                "name": code["name"],
                "description": code["description"] or "",
                "id_at_start": code["id"],
            })
            visit(code["id"], code_path)

    visit(None, [])
    return entries


# Resolve a path of code names from the top level to exactly one active code.
def _resolve_code_path(
    children: dict[int | None, list[dict]], path: Any
) -> tuple[int | None, str | None]:
    if (
        not isinstance(path, list)
        or not path
        or not all(isinstance(name, str) and name.strip() for name in path)
    ):
        return None, "The code path must be a list of code names, starting at the top level."
    parent: int | None = None
    for depth, name in enumerate(path):
        wanted = _clean_name(name)
        candidates = children.get(parent, [])
        matches = [code for code in candidates if _clean_name(code["name"]) == wanted]
        if not matches:
            # Tolerate a change of case only when it still points to one code.
            matches = [
                code for code in candidates
                if _clean_name(code["name"]).casefold() == wanted.casefold()
            ]
        shown = " > ".join(path[:depth + 1])
        if not matches:
            return None, f"There is no code “{shown}” in this project."
        if len(matches) > 1:
            return None, f"More than one code is called “{shown}”; the code is unclear."
        parent = matches[0]["id"]
    return parent, None


# Check where a passage goes in the text, without touching the database.
def _place_passage(anchor_text: AnchorText, passage: Any, has_text_hash: bool) -> dict:
    """Return {"start", "end", "method"} or {"reason"} for one submitted passage.

    A position without a quote is taken only from a submission that carries the text's
    fingerprint (``text_hash``): nothing else shows that the program counted in the same
    text, so the offsets could point anywhere.
    """
    if not isinstance(passage, Mapping):
        return {"reason": "The passage is not in the expected form."}
    quote = passage.get("quote")
    start, end = passage.get("start_pos"), passage.get("end_pos")
    if start is not None or end is not None:
        text = anchor_text.text
        if not all(isinstance(value, int) and not isinstance(value, bool)
                   for value in (start, end)):
            return {"reason": "The position needs both start_pos and end_pos as whole numbers."}
        if start < 0 or end <= start or end > len(text):
            return {"reason": "The position lies outside the document text."}
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start >= end:
            return {"reason": "The position covers only blank space."}
        if cuts_character(text, start, end):
            return {"reason": "The position cuts through a character of the document."}
        if quote is not None and (
            not isinstance(quote, str) or not same_text(quote, text[start:end])
        ):
            return {"reason": "The quote does not match the document at the given position."}
        if quote is None and not has_text_hash:
            return {
                "reason": "A passage given only by position needs the text_hash of the text "
                "it was counted in; send the text_hash with the submission or add the quote."
            }
        return {"start": start, "end": end, "method": POSITION}
    if quote is None:
        return {"reason": "The passage gives neither a quote nor a position."}
    if not isinstance(quote, str):
        return {"reason": "The quote is not text."}
    before, after = passage.get("before"), passage.get("after")
    anchor = anchor_text.locate(
        quote,
        before if isinstance(before, str) else None,
        after if isinstance(after, str) else None,
    )
    if not anchor.found:
        return {"reason": anchor.reason}
    return {"start": anchor.start, "end": anchor.end, "method": anchor.method}


# Place every passage of a submission (pure; run in a worker thread for long texts).
def _place_passages(text: str, passages: list[Any], has_text_hash: bool) -> list[dict]:
    anchor_text = AnchorText(text)
    return [_place_passage(anchor_text, passage, has_text_hash) for passage in passages]


# The quote a refusal names: the submitted quote, if it is text.
def _submitted_quote(passage: Any) -> str | None:
    if isinstance(passage, Mapping) and isinstance(passage.get("quote"), str):
        return passage["quote"]
    return None


class _SubmissionCodes:
    """Code lookups for one submission, inside its transaction."""

    def __init__(self, db, run, children: dict[int | None, list[dict]]):
        self.db = db
        self.run = run
        self.children = children
        self.active_ids = {code["id"] for codes in children.values() for code in codes}
        self.parent_id: int | None = run["proposed_parent_code_id"]

    # Return (code ID, None), (None, reason), or ("new", (name, description)) for a passage.
    def choose(self, passage: Mapping) -> tuple[Any, Any]:
        given = [
            key for key in ("code_id", "code_path", "new_code") if passage.get(key) is not None
        ]
        if not given:
            return None, "The passage names no code."
        if len(given) > 1:
            return None, "The passage names a code in more than one way; give exactly one."
        if given == ["code_id"]:
            code_id = passage["code_id"]
            if isinstance(code_id, bool) or not isinstance(code_id, int):
                return None, "The code ID is not a whole number."
            if code_id not in self.active_ids:
                return None, "There is no such code in this project (or it is in the trash)."
            return code_id, None
        if given == ["code_path"]:
            return _resolve_code_path(self.children, passage["code_path"])
        if not self.run["allow_new_codes"]:
            return None, "This run was started without permission to propose new codes."
        new_code = passage["new_code"]
        if not isinstance(new_code, Mapping):
            return None, "A new code needs a name."
        name = new_code.get("name")
        if not isinstance(name, str) or not _clean_name(name):
            return None, "A new code needs a name."
        if len(_clean_name(name)) > MAX_CODE_NAME_LENGTH:
            return None, f"A code name can be at most {MAX_CODE_NAME_LENGTH} characters long."
        description = new_code.get("description", new_code.get("definition"))
        if description is not None and not isinstance(description, str):
            return None, "The definition of a new code must be text."
        return "new", (_clean_name(name), (description or "").strip())

    # Return the parent code that collects this run's new codes, creating it on first use.
    async def proposals_parent(self) -> tuple[int | None, str | None]:
        if self.parent_id is not None:
            row = await (
                await self.db.execute(
                    "SELECT name, deleted_at FROM code WHERE id=? AND project_id=?",
                    (self.parent_id, self.run["project_id"]),
                )
            ).fetchone()
            if row and row["deleted_at"] is None:
                return self.parent_id, None
            if row:
                return None, (
                    f"The code “{row['name']}”, which collects this run's new codes, is in "
                    "the trash. Restore it to accept new codes from this run."
                )
        base = f"Proposed by {_clean_name(self.run['label'])}"
        taken = {_clean_name(code["name"]) for code in self.children.get(None, [])}
        name, number = base, 2
        while name in taken:
            name, number = f"{base} ({number})", number + 1
        description = f"New codes proposed by the model run “{self.run['label']}”"
        description += f" ({self.run['model']})." if self.run["model"] else "."
        self.parent_id = await self._insert_code(None, name, description)
        await _update_run(self.db, self.run["id"], proposed_parent_code_id=self.parent_id)
        return self.parent_id, None

    # Return an active code of that name under the proposals parent, or create it.
    async def new_code(self, name: str, description: str) -> tuple[int | None, str | None]:
        parent_id, reason = await self.proposals_parent()
        if parent_id is None:
            return None, reason
        for code in self.children.get(parent_id, []):
            if _clean_name(code["name"]).casefold() == name.casefold():
                return code["id"], None
        return await self._insert_code(parent_id, name, description), None

    # Insert a code and keep the lookups current.
    async def _insert_code(self, parent_id: int | None, name: str, description: str) -> int:
        color = await next_code_color(self.db, self.run["project_id"])
        cursor = await self.db.execute(
            "INSERT INTO code (project_id, parent_id, name, description, color) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.run["project_id"], parent_id, name, description, color),
        )
        code_id = cursor.lastrowid
        self.children.setdefault(parent_id, []).append(
            {"id": code_id, "parent_id": parent_id, "name": name, "description": description}
        )
        self.active_ids.add(code_id)
        return code_id


@router.post("", status_code=201)
async def create_run(data: RunCreate):
    """Start a model run; the server records the current codebook with it."""
    label = " ".join(data.label.split())
    if not label:
        raise HTTPException(422, "A model run needs a label.")
    if len(label) > MAX_LABEL_LENGTH:
        raise HTTPException(
            422, f"A model run label can be at most {MAX_LABEL_LENGTH} characters long."
        )
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
        codebook = _codebook(await _active_code_tree(db, data.project_id))
        cursor = await db.execute(
            "INSERT INTO coding_run (project_id, uuid, label, model, instructions, settings, "
            "codebook, allow_new_codes, status, note, started_at, last_activity_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, datetime('now'), datetime('now'))",
            (
                data.project_id,
                str(uuid.uuid4()),
                label,
                data.model.strip(),
                data.instructions,
                json.dumps(data.settings, ensure_ascii=False),
                json.dumps(codebook, ensure_ascii=False),
                int(data.allow_new_codes),
                data.note,
            ),
        )
        run_id = cursor.lastrowid
        await touch_project(db, data.project_id)
        await db.commit()
        return await _run_by_id(db, run_id)
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.get("")
async def list_runs(project_id: int, include_removed: bool = False):
    """List a project's model runs, newest first, with their counts."""
    db = await get_db()
    try:
        sql = f"{_RUN_SELECT}WHERE r.project_id=?"
        if not include_removed:
            sql += " AND r.deleted_at IS NULL"
        rows = await (
            await db.execute(f"{sql} ORDER BY r.started_at DESC, r.id DESC", (project_id,))
        ).fetchall()
        return [_run_response(row) for row in rows]
    finally:
        await db.close()


@router.get("/{run_uuid}")
async def get_run(run_uuid: str, project_id: int | None = None):
    """Return a run with one row per document it read."""
    db = await get_db()
    try:
        run = _run_response(await _find_run(db, run_uuid, project_id))
        rows = await (
            await db.execute(
                "SELECT rd.document_id, d.name AS document_name, rd.text_hash, "
                "rd.processed_at, rd.submitted, rd.accepted, rd.refused, rd.raw_response, "
                "(SELECT COUNT(*) FROM coding c WHERE c.run_id=rd.run_id "
                "AND c.document_id=rd.document_id AND c.deleted_at IS NULL) AS codings "
                "FROM coding_run_document rd JOIN document d ON d.id=rd.document_id "
                "WHERE rd.run_id=? ORDER BY rd.processed_at, d.name, d.id",
                (run["id"],),
            )
        ).fetchall()
        documents = []
        for row in rows:
            document = dict(row)
            document["refused"] = _json_or(document["refused"], [])
            documents.append(document)
        run["documents"] = documents
        return run
    finally:
        await db.close()


@router.post("/{run_uuid}/documents/{document_id}")
async def submit_document(
    run_uuid: str, document_id: int, data: DocumentSubmission, project_id: int | None = None
):
    """Record what a model found in one document; replaces an earlier submission."""
    db = await get_db()
    try:
        run = await _find_run(db, run_uuid, project_id)
        _check_open(run)
        document = await (
            await db.execute(
                "SELECT id, project_id, name, content, transcript, source_type, "
                "exclude_from_ai FROM document WHERE id=?",
                (document_id,),
            )
        ).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        if document["project_id"] != run["project_id"]:
            raise HTTPException(400, "This document belongs to another project than the run.")
        if document["exclude_from_ai"]:
            raise HTTPException(
                409, "This document is excluded from AI features, so a model run cannot code it."
            )
        text = codeable_text(document)
        current_hash = text_hash(text)
        if data.text_hash is not None and data.text_hash != current_hash:
            raise HTTPException(
                409,
                "The document has changed since the program read it. Nothing was saved; read "
                "the text again and resubmit.",
            )
        # Placing quotes can take a moment on long documents: do it before the write lock.
        placements = await asyncio.to_thread(
            _place_passages, text, data.passages, data.text_hash is not None
        )

        await db.execute("BEGIN IMMEDIATE")
        # Re-check under the write lock: the run or the document may have changed meanwhile.
        run = await _find_run(db, run_uuid, project_id)
        _check_open(run)
        document = await (
            await db.execute(
                "SELECT content, transcript, source_type FROM document "
                "WHERE id=? AND project_id=?",
                (document_id, run["project_id"]),
            )
        ).fetchone()
        if not document:
            raise HTTPException(404, "Document not found")
        if text_hash(codeable_text(document)) != current_hash:
            raise HTTPException(
                409,
                "The document changed while the passages were being placed. Nothing was "
                "saved; read the text again and resubmit.",
            )

        # Replace an earlier submission of this document in this run. The stamp has whole
        # seconds, so restoring the run never brings these codings back, and leaving any
        # code deletion batch keeps a code restore from doing so either.
        await db.execute(
            "UPDATE coding SET deleted_at=datetime('now'), deletion_batch_id=NULL "
            "WHERE run_id=? AND document_id=?",
            (run["id"], document_id),
        )

        codes = _SubmissionCodes(db, run, await _active_code_tree(db, run["project_id"]))
        accepted: list[dict] = []
        refused: list[dict] = []
        for index, (passage, placement) in enumerate(zip(data.passages, placements)):
            reason = placement.get("reason")
            if reason is None:
                result = await _save_passage(db, run, document_id, text, passage, placement, codes)
                if isinstance(result, int):
                    accepted.append(
                        {"index": index, "method": placement["method"], "coding_id": result}
                    )
                    continue
                reason = result
            refused.append({"index": index, "quote": _submitted_quote(passage), "reason": reason})

        await db.execute(
            "INSERT INTO coding_run_document (run_id, document_id, text_hash, processed_at, "
            "submitted, accepted, refused, raw_response) "
            "VALUES (?, ?, ?, datetime('now'), ?, ?, ?, ?) "
            "ON CONFLICT(run_id, document_id) DO UPDATE SET text_hash=excluded.text_hash, "
            "processed_at=excluded.processed_at, submitted=excluded.submitted, "
            "accepted=excluded.accepted, refused=excluded.refused, "
            "raw_response=excluded.raw_response",
            (
                run["id"],
                document_id,
                current_hash,
                len(data.passages),
                len(accepted),
                json.dumps(refused, ensure_ascii=False),
                data.raw_response,
            ),
        )
        await db.execute(
            "UPDATE coding_run SET last_activity_at=datetime('now') WHERE id=?", (run["id"],)
        )
        await touch_project(db, run["project_id"])
        await db.commit()

        coding_ids = [item.pop("coding_id") for item in accepted]
        by_id = {}
        if coding_ids:
            placeholders = ",".join("?" * len(coding_ids))
            cursor = await db.execute(
                f"{_CODING_SELECT}WHERE cg.id IN ({placeholders})", coding_ids
            )
            loaded = await _with_fields(db, await cursor.fetchall())
            by_id = {coding["id"]: coding for coding in loaded}
        for item, coding_id in zip(accepted, coding_ids):
            item["coding"] = by_id[coding_id]
        return {
            "run_uuid": run["uuid"],
            "document_id": document_id,
            "text_hash": current_hash,
            "accepted": accepted,
            "refused": refused,
            "counts": {
                "submitted": len(data.passages),
                "accepted": len(accepted),
                "refused": len(refused),
            },
        }
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


# Refuse submissions to a run that is removed or no longer running.
def _check_open(run) -> None:
    if run["deleted_at"] is not None:
        raise HTTPException(409, "This model run was removed; restore it or start a new run.")
    if run["status"] != "running":
        raise HTTPException(
            409, f"This model run is {run['status']}; start a new run to code more documents."
        )


# Save one placed passage as a coding; return its ID or the reason it was refused.
async def _save_passage(
    db, run, document_id: int, text: str, passage: Mapping, placement: dict,
    codes: _SubmissionCodes,
) -> int | str:
    choice, detail = codes.choose(passage)
    if choice is None:
        return detail
    try:
        answers = await validate_field_values(db, run["project_id"], passage.get("fields"))
    except CodingFieldError as exc:
        return str(exc)
    if choice == "new":
        choice, reason = await codes.new_code(*detail)
        if choice is None:
            return reason
    start, end = placement["start"], placement["end"]
    duplicate = await find_duplicate_coding(
        db,
        document_id=document_id,
        code_id=choice,
        start_pos=start,
        end_pos=end,
        run_id=run["id"],
        coder=run["label"],
        answers=answers,
    )
    if duplicate:
        return (
            "The same passage, code, and answers were already submitted for this document "
            "in this run."
        )
    cursor = await db.execute(
        "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, coder, "
        "offset_unit, run_id) VALUES (?, ?, ?, ?, ?, ?, 'codepoint', ?)",
        (document_id, choice, start, end, text[start:end], run["label"], run["id"]),
    )
    await save_field_values(db, cursor.lastrowid, answers)
    return cursor.lastrowid


@router.post("/{run_uuid}/finish")
async def finish_run(run_uuid: str, data: RunFinish | None = None, project_id: int | None = None):
    """Mark a running run as finished (default) or stopped.

    A run that has already ended keeps its status, so repeating the call (or a program
    finishing a run the researcher stopped or removed) changes nothing.
    """
    status = (data or RunFinish()).status
    if status not in FINISHED_STATUSES:
        raise HTTPException(422, "A run can be marked as finished or stopped.")
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        run = await _find_run(db, run_uuid, project_id)
        if run["status"] == "running":
            await db.execute(
                "UPDATE coding_run SET status=?, finished_at=COALESCE(finished_at, ?) "
                "WHERE id=?",
                (status, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), run["id"]),
            )
            await touch_project(db, run["project_id"])
        await db.commit()
        return await _run_by_id(db, run["id"])
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.delete("/{run_uuid}")
async def remove_run(run_uuid: str, project_id: int | None = None):
    """Remove a run: its codings leave use until the run is restored."""
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        run = await _find_run(db, run_uuid, project_id)
        if run["deleted_at"] is None:
            stamp = _removal_stamp()
            # Active codings take the run's stamp and no deletion batch. Codings removed
            # earlier (one by one, superseded, or with their code) keep their own stamp.
            await db.execute(
                "UPDATE coding SET deleted_at=?, deletion_batch_id=NULL "
                "WHERE run_id=? AND deleted_at IS NULL",
                (stamp, run["id"]),
            )
            fields: dict[str, Any] = {"deleted_at": stamp}
            if run["status"] == "running":
                fields["status"] = "stopped"
                fields["finished_at"] = run["finished_at"] or stamp[:19]
            await _update_run(db, run["id"], **fields)
            await touch_project(db, run["project_id"])
        await db.commit()
        return await _run_by_id(db, run["id"])
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()


@router.post("/{run_uuid}/restore")
async def restore_run(run_uuid: str, project_id: int | None = None):
    """Restore a removed run and the codings removed with it."""
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        run = await _find_run(db, run_uuid, project_id)
        if run["deleted_at"] is not None:
            # Codings carrying the run's stamp come back, except those whose code is in the
            # trash meanwhile: they join that code's deletion (its batch and stamp), so
            # restoring the code brings them back.
            await db.execute(
                "UPDATE coding SET "
                "deleted_at=(SELECT c.deleted_at FROM code c WHERE c.id=coding.code_id), "
                "deletion_batch_id=(SELECT c.deletion_batch_id FROM code c "
                "WHERE c.id=coding.code_id) "
                "WHERE run_id=? AND deleted_at=? AND deletion_batch_id IS NULL",
                (run["id"], run["deleted_at"]),
            )
            await _update_run(db, run["id"], deleted_at=None)
            await touch_project(db, run["project_id"])
        await db.commit()
        return await _run_by_id(db, run["id"])
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()
