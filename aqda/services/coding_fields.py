"""Coding fields: per-project questions answered for each coding (yes/no, number, text).

The helpers take an open connection and plain values, never a request, so the codings
routes, model coding runs, and code merging share one set of rules. They do not commit;
the caller owns the transaction.
"""

import re
from collections.abc import Mapping
from typing import Any

FIELD_KINDS = ("yes_no", "number", "text")
MAX_FIELD_NAME_LENGTH = 100
_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]+")
_YES = {"1", "yes", "true"}
_NO = {"0", "no", "false"}
# Keep IN (...) lists well below SQLite's variable limit.
_CHUNK = 500


class CodingFieldError(ValueError):
    """A field name or value that cannot be saved; the message is meant for the researcher."""


# Trim a field name and refuse empty or overlong names.
def clean_field_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise CodingFieldError("A coding field needs a name.")
    cleaned = " ".join(name.split())
    if len(cleaned) > MAX_FIELD_NAME_LENGTH:
        raise CodingFieldError(
            f"A coding field name can be at most {MAX_FIELD_NAME_LENGTH} characters long."
        )
    return cleaned


# Refuse a field kind other than yes/no, number, or text.
def check_field_kind(kind: Any) -> str:
    if kind not in FIELD_KINDS:
        raise CodingFieldError("A coding field is a yes/no question, a number, or text.")
    return kind


# Return the stored form of one answer, or None when the answer is left empty.
def normalize_field_value(kind: str, name: str, value: Any) -> str | None:
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else None
    if text == "":
        return None
    if kind == "yes_no":
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int) and value in (0, 1):
            return str(value)
        if text is not None and text.casefold() in _YES:
            return "1"
        if text is not None and text.casefold() in _NO:
            return "0"
        raise CodingFieldError(f"“{name}” is answered with yes or no.")
    if kind == "number":
        if isinstance(value, bool):
            raise CodingFieldError(f"“{name}” is answered with a whole number.")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        if text is not None and _WHOLE_NUMBER.fullmatch(text):
            return str(int(text))
        raise CodingFieldError(f"“{name}” is answered with a whole number.")
    if kind == "text":
        if text is not None:
            return text
        raise CodingFieldError(f"“{name}” is answered with text.")
    raise CodingFieldError(f"“{name}” cannot be answered here.")


# List a project's coding fields in their display order.
async def project_fields(db, project_id: int) -> list[dict]:
    rows = await (
        await db.execute(
            "SELECT id, project_id, name, kind, sort_order, created_at FROM coding_field "
            "WHERE project_id=? ORDER BY sort_order, id",
            (project_id,),
        )
    ).fetchall()
    return [dict(row) for row in rows]


# Check answers given by field name against a project's fields.
async def validate_field_values(
    db, project_id: int, values: Mapping[str, Any] | None
) -> dict[int, str]:
    """Return the answers as {field_id: stored value}; empty answers are left out.

    Field names match regardless of case and surrounding spaces. An unknown field name or
    an answer of the wrong kind raises ``CodingFieldError`` with a message for the
    researcher; nothing is saved here.
    """
    if not values:
        return {}
    if not isinstance(values, Mapping):
        raise CodingFieldError("Coding field answers must be given by field name.")
    by_name = {
        field["name"].casefold(): field for field in await project_fields(db, project_id)
    }
    answers: dict[int, str] = {}
    seen: set[int] = set()
    unknown: list[str] = []
    for key, raw in values.items():
        field = by_name.get(" ".join(str(key).split()).casefold())
        if field is None:
            unknown.append(str(key))
            continue
        if field["id"] in seen:
            raise CodingFieldError(f"“{field['name']}” is answered more than once.")
        seen.add(field["id"])
        normalized = normalize_field_value(field["kind"], field["name"], raw)
        if normalized is not None:
            answers[field["id"]] = normalized
    if unknown:
        names = ", ".join(f"“{name}”" for name in unknown)
        raise CodingFieldError(f"This project has no coding field named {names}.")
    return answers


# Replace all field answers of one coding.
async def save_field_values(db, coding_id: int, answers: Mapping[int, str]) -> None:
    """Store ``answers`` ({field_id: value}, from ``validate_field_values``) for a coding.

    Earlier answers of the coding are removed first, so a field left out is unanswered.
    """
    await db.execute("DELETE FROM coding_field_value WHERE coding_id=?", (coding_id,))
    await db.executemany(
        "INSERT INTO coding_field_value (coding_id, field_id, value) VALUES (?, ?, ?)",
        [(coding_id, field_id, value) for field_id, value in answers.items()],
    )


# Return the stored answers of codings, keyed by field ID.
async def field_answers_by_id(db, coding_ids) -> dict[int, dict[int, str]]:
    ids = list(dict.fromkeys(coding_ids))
    answers: dict[int, dict[int, str]] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = await (
            await db.execute(
                "SELECT coding_id, field_id, value FROM coding_field_value "
                f"WHERE coding_id IN ({placeholders})",
                chunk,
            )
        ).fetchall()
        for row in rows:
            answers.setdefault(row["coding_id"], {})[row["field_id"]] = row["value"]
    return answers


# Return the stored answers of codings, keyed by field name (for API responses).
async def field_answers_by_name(db, coding_ids) -> dict[int, dict[str, str]]:
    ids = list(dict.fromkeys(coding_ids))
    answers: dict[int, dict[str, str]] = {}
    for start in range(0, len(ids), _CHUNK):
        chunk = ids[start:start + _CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = await (
            await db.execute(
                "SELECT v.coding_id, f.name, v.value FROM coding_field_value v "
                "JOIN coding_field f ON f.id=v.field_id "
                f"WHERE v.coding_id IN ({placeholders}) ORDER BY f.sort_order, f.id",
                chunk,
            )
        ).fetchall()
        for row in rows:
            answers.setdefault(row["coding_id"], {})[row["name"]] = row["value"]
    return answers


# Find an active coding that the new one would merely repeat.
async def find_duplicate_coding(
    db,
    *,
    document_id: int,
    code_id: int,
    start_pos: int,
    end_pos: int,
    run_id: int | None,
    coder: str | None,
    answers: Mapping[int, str],
    exclude_coding_id: int | None = None,
    deletion_state: tuple[str | None, str | None] = (None, None),
) -> int | None:
    """Return the ID of a coding equal in every respect, or None.

    A duplicate has the same document, code, and span, the same run (for a hand coding:
    no run and the same coder name), and exactly the same field answers. Several codings
    on one span that differ in any of these are ordinary (two actors, or a researcher
    agreeing with a model).

    By default only active codings count. ``deletion_state`` (``deleted_at``,
    ``deletion_batch_id``) looks instead for a coding out of use in exactly the same way,
    which a later restore would bring back together with the one being checked.
    """
    deleted_at, batch_id = deletion_state
    sql = (
        "SELECT id FROM coding WHERE document_id=? AND code_id=? AND start_pos=? "
        "AND end_pos=?"
    )
    params: list[Any] = [document_id, code_id, start_pos, end_pos]
    if deleted_at is None:
        sql += " AND deleted_at IS NULL"
    else:
        sql += " AND deleted_at=? AND deletion_batch_id IS ?"
        params += [deleted_at, batch_id]
    if run_id is not None:
        sql += " AND run_id=?"
        params.append(run_id)
    else:
        sql += " AND run_id IS NULL AND COALESCE(coder, '')=?"
        params.append(coder or "")
    if exclude_coding_id is not None:
        sql += " AND id<>?"
        params.append(exclude_coding_id)
    candidates = [row["id"] for row in await (await db.execute(sql, params)).fetchall()]
    if not candidates:
        return None
    existing = await field_answers_by_id(db, candidates)
    wanted = dict(answers)
    for candidate in candidates:
        if existing.get(candidate, {}) == wanted:
            return candidate
    return None


# Tell whether a project holds content that only snapshot format 2 or later can carry.
async def project_has_runs_or_fields(db, project_id: int) -> bool:
    row = await (
        await db.execute(
            "SELECT EXISTS(SELECT 1 FROM coding_run WHERE project_id=?) "
            "OR EXISTS(SELECT 1 FROM coding_field WHERE project_id=?)",
            (project_id, project_id),
        )
    ).fetchone()
    return bool(row[0])
