"""Canonical Unicode code-point offsets and legacy annotation repair."""

from __future__ import annotations

import aiosqlite


def utf16_to_codepoint(text: str, offset: int) -> int:
    """Translate a JavaScript/DOM UTF-16 offset into a Python code-point offset."""
    target = max(0, offset)
    units = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > target:
            return index
        units += width
        if units == target:
            return index + 1
    return len(text)


def _occurrences(text: str, needle: str) -> list[int]:
    if not needle:
        return []
    positions = []
    start = 0
    while True:
        found = text.find(needle, start)
        if found < 0:
            return positions
        positions.append(found)
        start = found + 1


async def repair_legacy_offsets(
    db: aiosqlite.Connection,
    project_ids: list[int] | None = None,
) -> dict[str, int]:
    """Convert legacy offsets and re-anchor corrupt codings conservatively.

    Unique text matches are repaired automatically. Multiple matches use the one
    nearest to the old position and remain flagged for researcher review.
    """
    project_filter = ""
    params: list[int] = []
    if project_ids:
        placeholders = ",".join("?" * len(project_ids))
        project_filter = f"AND d.project_id IN ({placeholders})"
        params = project_ids

    rows = await (
        await db.execute(
            "SELECT cg.id, cg.start_pos, cg.end_pos, cg.selected_text, d.project_id, "
            "d.content, d.transcript, d.source_type "
            "FROM coding cg JOIN document d ON d.id=cg.document_id "
            "WHERE cg.offset_unit='legacy_utf16' " + project_filter,
            params,
        )
    ).fetchall()
    counts = {"converted": 0, "reanchored": 0, "flagged": 0}
    affected_projects = {row["project_id"] for row in rows}
    for row in rows:
        text = (
            row["transcript"] or ""
            if row["source_type"] == "audio"
            else row["content"] or ""
        )
        old_start = max(0, int(row["start_pos"]))
        old_end = max(old_start, int(row["end_pos"]))
        selected = row["selected_text"] or ""

        # AI-created annotations already used Python/code-point offsets. Preserve
        # them when they verify before attempting the DOM UTF-16 conversion.
        if old_end <= len(text) and text[old_start:old_end] == selected:
            start, end, status = old_start, old_end, "converted_verified"
            counts["converted"] += 1
        else:
            converted_start = utf16_to_codepoint(text, old_start)
            converted_end = utf16_to_codepoint(text, old_end)
            if text[converted_start:converted_end] == selected:
                start, end, status = converted_start, converted_end, "converted_utf16"
                counts["converted"] += 1
            else:
                matches = _occurrences(text, selected)
                if len(matches) == 1:
                    start = matches[0]
                    end = start + len(selected)
                    status = "reanchored_unique"
                    counts["reanchored"] += 1
                elif matches:
                    start = min(matches, key=lambda position: abs(position - converted_start))
                    end = start + len(selected)
                    status = "review_reanchored_multiple"
                    counts["flagged"] += 1
                else:
                    start = min(converted_start, len(text))
                    end = min(max(start, converted_end), len(text))
                    status = "review_text_not_found"
                    counts["flagged"] += 1
        await db.execute(
            "UPDATE coding SET start_pos=?, end_pos=?, offset_unit='codepoint', "
            "repair_status=? WHERE id=?",
            (start, end, status, row["id"]),
        )

    memo_filter = ""
    memo_params: list[int] = []
    if project_ids:
        placeholders = ",".join("?" * len(project_ids))
        memo_filter = f"AND m.project_id IN ({placeholders})"
        memo_params = project_ids
    memos = await (
        await db.execute(
            "SELECT m.id, m.project_id, m.start_pos, m.end_pos, d.content, d.transcript, "
            "d.source_type "
            "FROM memo m JOIN document d ON d.id=m.document_id "
            "WHERE m.offset_unit='legacy_utf16' AND m.start_pos IS NOT NULL "
            "AND m.end_pos IS NOT NULL " + memo_filter,
            memo_params,
        )
    ).fetchall()
    affected_projects.update(row["project_id"] for row in memos)
    for row in memos:
        text = (
            row["transcript"] or ""
            if row["source_type"] == "audio"
            else row["content"] or ""
        )
        start = utf16_to_codepoint(text, int(row["start_pos"]))
        end = utf16_to_codepoint(text, int(row["end_pos"]))
        await db.execute(
            "UPDATE memo SET start_pos=?, end_pos=?, offset_unit='codepoint', "
            "repair_status='converted_utf16_unverified' WHERE id=?",
            (start, end, row["id"]),
        )
    await db.execute(
        "UPDATE memo SET offset_unit='codepoint' "
        "WHERE offset_unit='legacy_utf16' AND (start_pos IS NULL OR end_pos IS NULL)"
    )
    for project_id in affected_projects:
        await db.execute(
            "UPDATE project SET revision=revision+1, modified_at=datetime('now') WHERE id=?",
            (project_id,),
        )
    return counts
