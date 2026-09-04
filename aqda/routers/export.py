"""Export routes — REFI-QDA (.qdpx), codebook (.qdc), CSV, JSON, standalone .aqda."""

import asyncio
import csv
import io
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from aqda.db import get_db, SCHEMA

router = APIRouter()


def _uuid() -> str:
    return str(uuid.uuid4())


def _is_xml_char(codepoint: int) -> bool:
    """The XML 1.0 ``Char`` production."""
    return (
        codepoint in (0x9, 0xA, 0xD)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def _safe_xml_text(text: str) -> str:
    """Remove characters XML 1.0 forbids entirely; ElementTree performs escaping."""
    return "".join(char for char in text if _is_xml_char(ord(char)))


def _download_headers(filename: str) -> dict[str, str]:
    """Standards-compliant Unicode download name with a conservative fallback."""
    clean = (
        filename.replace("\r", " ")
        .replace("\n", " ")
        .replace('"', "'")
        .replace("/", "-")
        .replace("\\", "-")
    )
    fallback = (
        unicodedata.normalize("NFKD", clean).encode("ascii", "ignore").decode("ascii")
        or "aqda-export"
    )
    return {
        "Content-Disposition": (
            f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(clean)}'
        )
    }


def _xs_datetime(ts: str | None) -> str | None:
    """Convert SQLite 'YYYY-MM-DD HH:MM:SS' to xs:dateTime 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not ts:
        return None
    return ts.replace(" ", "T") + "Z"


def _attrs(**kwargs) -> dict[str, str]:
    """Build an attribute dict that drops any None values (so optional xs:dateTime attrs are omitted, not emitted empty)."""
    return {k: v for k, v in kwargs.items() if v is not None}


async def _load_project_data(
    project_id: int,
    include_deleted: bool = False,
    db=None,
):
    """Load all project data for export.

    By default, soft-deleted (trashed) codes and codings are excluded so they
    never leak into interchange formats (REFI-QDA, codebook, CSV, JSON). The
    standalone .aqda backup passes include_deleted=True to preserve full state.
    """
    owns_connection = db is None
    if owns_connection:
        db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        project = await cursor.fetchone()
        if not project:
            raise HTTPException(404, "Project not found")

        cursor = await db.execute(
            "SELECT * FROM document WHERE project_id=? ORDER BY name", (project_id,)
        )
        documents = await cursor.fetchall()

        code_filter = "" if include_deleted else "AND deleted_at IS NULL"
        cursor = await db.execute(
            f"SELECT * FROM code WHERE project_id=? {code_filter} "
            "ORDER BY parent_id NULLS FIRST, sort_order",
            (project_id,),
        )
        codes = await cursor.fetchall()

        coding_filter = "" if include_deleted else "AND cg.deleted_at IS NULL AND c.deleted_at IS NULL"
        cursor = await db.execute(
            "SELECT cg.*, c.name as code_name FROM coding cg "
            "JOIN code c ON c.id=cg.code_id "
            "JOIN document d ON d.id=cg.document_id "
            f"WHERE d.project_id=? {coding_filter} ORDER BY cg.document_id, cg.start_pos",
            (project_id,),
        )
        codings = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT * FROM memo WHERE project_id=? ORDER BY modified_at DESC",
            (project_id,),
        )
        memos = await cursor.fetchall()

        return dict(project), [dict(d) for d in documents], [dict(c) for c in codes], [dict(cg) for cg in codings], [dict(m) for m in memos]
    finally:
        if owns_connection:
            await db.close()


async def _load_document_variables(project_id: int) -> dict[int, dict[str, str]]:
    """Document variables keyed by document ID."""
    db = await get_db()
    try:
        rows = await (
            await db.execute(
                "SELECT dv.document_id, dv.key, dv.value FROM document_variable dv "
                "JOIN document d ON d.id=dv.document_id WHERE d.project_id=? "
                "ORDER BY dv.document_id, dv.key",
                (project_id,),
            )
        ).fetchall()
    finally:
        await db.close()
    variables: dict[int, dict[str, str]] = {}
    for row in rows:
        variables.setdefault(row["document_id"], {})[row["key"]] = row["value"]
    return variables


async def _get_coder_name() -> str:
    """Configured coder identity for REFI-QDA <User>, falling back to a default."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT value FROM setting WHERE key='coder_name'")
        row = await cursor.fetchone()
        name = (row["value"] if row else "") or ""
        return name.strip() or "AQDA User"
    finally:
        await db.close()


# --- REFI-QDA Export ---

def _build_code_tree(codes: list[dict]) -> list[dict]:
    """Build the hierarchical code tree.

    A code whose parent is not in ``codes`` (for example a restored code whose
    parent is still in the trash) becomes a top-level code, as in the sidebar,
    instead of vanishing from the export along with its codings.
    """
    known = {code["id"] for code in codes}
    by_parent: dict[int | None, list[dict]] = {}
    for code in codes:
        parent_id = code["parent_id"] if code["parent_id"] in known else None
        by_parent.setdefault(parent_id, []).append(code)

    visited: set[int] = set()

    def attach(parent_id: int | None) -> list[dict]:
        children = []
        for child in by_parent.get(parent_id, []):
            if child["id"] in visited:
                continue
            visited.add(child["id"])
            child["children"] = attach(child["id"])
            children.append(child)
        return children

    roots = attach(None)
    # A parent cycle (only possible in imported data) is unreachable from the top.
    # Promote one member of each cycle to a top-level code rather than dropping it.
    for code in codes:
        if code["id"] not in visited:
            visited.add(code["id"])
            code["children"] = attach(code["id"])
            roots.append(code)
    return roots


def _add_codes_xml(
    parent_el,
    codes_tree: list[dict],
    guid_map: dict,
    notes_by_code: dict[int, list[str]] | None = None,
):
    """Recursively add codes to XML (CodeType order: Description, NoteRef, child Codes)."""
    for code in codes_tree:
        guid = _uuid()
        guid_map[("code", code["id"])] = guid
        code_el = SubElement(parent_el, "Code", {
            "guid": guid,
            "name": _safe_xml_text(code["name"]),
            "isCodable": "true",
            "color": code["color"],
        })
        if code.get("description"):
            desc = SubElement(code_el, "Description")
            desc.text = _safe_xml_text(code["description"])
        for note_guid in (notes_by_code or {}).get(code["id"], []):
            SubElement(code_el, "NoteRef", {"targetGUID": note_guid})
        if code.get("children"):
            _add_codes_xml(code_el, code["children"], guid_map, notes_by_code)


@router.get("/{project_id}/qdpx")
async def export_qdpx(project_id: int):
    """Export project as REFI-QDA .qdpx file."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)
    variables = await _load_document_variables(project_id)
    default_coder = await _get_coder_name()

    guid_map = {}

    # Memos become Notes; each is referenced from the code, the coded selection, the
    # passage, and/or the document it belongs to, or from the project when none of
    # those is exported (for example when its code is in the trash).
    note_guids = {memo["id"]: _uuid() for memo in memos}
    exported_codes = {code["id"] for code in codes}
    exported_codings = {cg["id"] for cg in codings}
    exported_docs = {doc["id"] for doc in documents}
    notes_by_code: dict[int, list[str]] = {}
    notes_by_coding: dict[int, list[str]] = {}
    notes_by_passage: dict[int, list[tuple[dict, str]]] = {}
    notes_by_doc: dict[int, list[str]] = {}
    project_notes: list[str] = []
    for memo in memos:
        guid = note_guids[memo["id"]]
        linked = False
        if memo.get("code_id") in exported_codes:
            notes_by_code.setdefault(memo["code_id"], []).append(guid)
            linked = True
        if memo.get("coding_id") in exported_codings:
            notes_by_coding.setdefault(memo["coding_id"], []).append(guid)
            linked = True
        elif memo.get("document_id") in exported_docs:
            if memo.get("start_pos") is not None and memo.get("end_pos") is not None:
                notes_by_passage.setdefault(memo["document_id"], []).append((memo, guid))
            else:
                notes_by_doc.setdefault(memo["document_id"], []).append(guid)
            linked = True
        if not linked:
            project_notes.append(guid)

    # Per-coding attribution: one <User> per distinct coder name. Codings with no
    # recorded coder fall back to the configured default.
    coder_to_guid: dict[str, str] = {default_coder: _uuid()}
    for cg in codings:
        name = (cg["coder"] or "").strip() or default_coder
        if name not in coder_to_guid:
            coder_to_guid[name] = _uuid()
    default_guid = coder_to_guid[default_coder]

    def _coder_guid(cg) -> str:
        return coder_to_guid.get((cg["coder"] or "").strip() or default_coder, default_guid)

    # Build XML. ProjectType sequence per REFI-QDA XSD: Users, CodeBook, ..., Sources, Notes, ..., Description (last).
    root = Element("Project", _attrs(
        xmlns="urn:QDA-XML:project:1.0",
        name=_safe_xml_text(project["name"]),
        origin="AQDA",
        creatingUserGUID=default_guid,
        creationDateTime=_xs_datetime(project["created_at"]),
    ))

    # Users (sorted for stable output)
    users_el = SubElement(root, "Users")
    for name in sorted(coder_to_guid):
        SubElement(users_el, "User", {"guid": coder_to_guid[name], "name": _safe_xml_text(name)})

    # CodeBook. The schema requires at least one Code inside Codes, while the
    # CodeBook element itself is optional, so an empty codebook is omitted.
    code_tree = _build_code_tree(codes)
    if code_tree:
        codebook_el = SubElement(root, "CodeBook")
        codes_el = SubElement(codebook_el, "Codes")
        _add_codes_xml(codes_el, code_tree, guid_map, notes_by_code)

    # Variables: one Text variable per distinct document-variable key.
    variable_guids = {
        key: _uuid()
        for key in sorted({key for values in variables.values() for key in values})
    }
    if variable_guids:
        variables_el = SubElement(root, "Variables")
        for key, guid in variable_guids.items():
            SubElement(variables_el, "Variable", {
                "guid": guid,
                "name": _safe_xml_text(key),
                "typeOfVariable": "Text",
            })

    # Sources (documents). Files go to "Sources/{guid}.txt"; XML references them as
    # "internal://{guid}.txt". Like CodeBook, an empty Sources element is invalid.
    sources_el = SubElement(root, "Sources") if documents else None
    codings_by_doc = {}
    for cg in codings:
        codings_by_doc.setdefault(cg["document_id"], []).append(cg)

    for doc in documents:
        doc_guid = _uuid()
        guid_map[("doc", doc["id"])] = doc_guid
        source_el = SubElement(sources_el, "TextSource", _attrs(
            guid=doc_guid,
            name=_safe_xml_text(doc["name"]),
            plainTextPath=f"internal://{doc_guid}.txt",
            creatingUser=default_guid,
            creationDateTime=_xs_datetime(doc["created_at"]),
        ))

        # TextSourceType order: PlainTextSelection*, Coding*, NoteRef*, VariableValue*.
        # Coded selections are attributed to the coder who made each one.
        for cg in codings_by_doc.get(doc["id"], []):
            sel_guid = _uuid()
            coder_guid = _coder_guid(cg)
            sel_el = SubElement(source_el, "PlainTextSelection", _attrs(
                guid=sel_guid,
                startPosition=str(cg["start_pos"]),
                endPosition=str(cg["end_pos"]),
                creatingUser=coder_guid,
                creationDateTime=_xs_datetime(cg["created_at"]),
            ))
            coding_el = SubElement(sel_el, "Coding", _attrs(
                guid=_uuid(),
                creatingUser=coder_guid,
                creationDateTime=_xs_datetime(cg["created_at"]),
            ))
            # Every exported coding belongs to an exported code; a missing entry
            # would be a bug and must fail rather than emit a dangling reference.
            SubElement(coding_el, "CodeRef", {"targetGUID": guid_map[("code", cg["code_id"])]})
            for note_guid in notes_by_coding.get(cg["id"], []):
                SubElement(sel_el, "NoteRef", {"targetGUID": note_guid})

        # A memo anchored to a passage keeps its span as a selection without a Coding.
        for memo, note_guid in notes_by_passage.get(doc["id"], []):
            passage_el = SubElement(source_el, "PlainTextSelection", _attrs(
                guid=_uuid(),
                startPosition=str(memo["start_pos"]),
                endPosition=str(memo["end_pos"]),
                creatingUser=default_guid,
                creationDateTime=_xs_datetime(memo["created_at"]),
            ))
            SubElement(passage_el, "NoteRef", {"targetGUID": note_guid})

        for note_guid in notes_by_doc.get(doc["id"], []):
            SubElement(source_el, "NoteRef", {"targetGUID": note_guid})
        for key, value in variables.get(doc["id"], {}).items():
            value_el = SubElement(source_el, "VariableValue")
            SubElement(value_el, "VariableRef", {"targetGUID": variable_guids[key]})
            SubElement(value_el, "TextValue").text = _safe_xml_text(value)

    # Notes (memos)
    if memos:
        notes_el = SubElement(root, "Notes")
        for memo in memos:
            note_el = SubElement(notes_el, "Note", _attrs(
                guid=note_guids[memo["id"]],
                name=_safe_xml_text(memo["title"] or "Memo"),
                creatingUser=default_guid,
                creationDateTime=_xs_datetime(memo["created_at"]),
            ))
            content_el = SubElement(note_el, "PlainTextContent")
            content_el.text = _safe_xml_text(memo["content"])

    # Description, then project-level NoteRefs, per the ProjectType sequence in the XSD.
    if project.get("description"):
        desc = SubElement(root, "Description")
        desc.text = _safe_xml_text(project["description"])
    for note_guid in project_notes:
        SubElement(root, "NoteRef", {"targetGUID": note_guid})

    # Build ZIP
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.qde", xml_bytes)
        for doc in documents:
            doc_guid = guid_map[("doc", doc["id"])]
            # Codings on audio index the transcript, and image "content" is a base64
            # data URI — write the human-readable text so .txt offsets stay valid.
            if doc["source_type"] == "audio":
                source_text = doc.get("transcript") or ""
            elif doc["source_type"] == "image":
                source_text = ""
            else:
                source_text = doc["content"] or ""
            zf.writestr(f"Sources/{doc_guid}.txt", source_text)

    buf.seek(0)
    filename = f"{project['name'].replace(' ', '_')}.qdpx"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers=_download_headers(filename),
    )


@router.get("/{project_id}/qdc")
async def export_codebook(project_id: int):
    """Export codebook as REFI-QDA .qdc file."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    guid_map = {}
    code_tree = _build_code_tree(codes)
    if not code_tree:
        raise HTTPException(400, "This project has no codes to export as a codebook")
    root = Element("CodeBook", {
        "xmlns": "urn:QDA-XML:codebook:1.0",
        "origin": "AQDA",
    })
    codes_el = SubElement(root, "Codes")
    _add_codes_xml(codes_el, code_tree, guid_map)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")

    filename = f"{project['name'].replace(' ', '_')}_codebook.qdc"
    return StreamingResponse(
        io.BytesIO(xml_bytes),
        media_type="application/xml",
        headers=_download_headers(filename),
    )


@router.get("/{project_id}/csv")
async def export_csv(project_id: int):
    """Export coded segments as CSV."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["document", "code", "coder", "start_pos", "end_pos", "text", "created_at"])
    for cg in codings:
        doc_name = next((d["name"] for d in documents if d["id"] == cg["document_id"]), "")
        writer.writerow([
            doc_name,
            cg["code_name"],
            cg["coder"] or "",
            cg["start_pos"],
            cg["end_pos"],
            cg["selected_text"],
            cg["created_at"],
        ])

    content = buf.getvalue().encode("utf-8-sig")
    filename = f"{project['name'].replace(' ', '_')}_codings.csv"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="text/csv",
        headers=_download_headers(filename),
    )


@router.get("/{project_id}/json")
async def export_json(project_id: int):
    """Export analysis-oriented project data as JSON (not an archive format)."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)
    variables = await _load_document_variables(project_id)

    docs_meta = []
    for document in documents:
        metadata = {key: value for key, value in document.items() if key != "content"}
        metadata["variables"] = variables.get(document["id"], {})
        docs_meta.append(metadata)

    data = {
        "format": "AQDA analysis export (documents exclude source content)",
        "project": project,
        "documents": docs_meta,
        "codes": codes,
        "codings": codings,
        "memos": memos,
    }

    content = json.dumps(data, indent=2, default=str).encode("utf-8")
    filename = f"{project['name'].replace(' ', '_')}.json"
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/json",
        headers=_download_headers(filename),
    )


async def build_aqda_snapshot(project_id: int) -> tuple[bytes, dict, str]:
    """Build a coherent immutable project snapshot and return bytes, project, ID."""
    # Capture project data and its new snapshot record under one write lock. This
    # ensures the package contents and revision metadata describe the same state.
    db = await get_db()
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        current = await cursor.fetchone()
        if not current:
            raise HTTPException(404, "Project not found")

        existing_head = None
        if current["head_snapshot_id"]:
            existing_head = await (
                await db.execute(
                    "SELECT snapshot_id, revision FROM project_snapshot "
                    "WHERE snapshot_id=? AND project_id=?",
                    (current["head_snapshot_id"], project_id),
                )
            ).fetchone()
        if existing_head and existing_head["revision"] == current["revision"]:
            # Exporting or force-syncing an unchanged project should not invent
            # another ancestry node every time the user clicks the button.
            snapshot_id = existing_head["snapshot_id"]
        else:
            cursor = await db.execute("SELECT value FROM setting WHERE key='coder_name'")
            coder_row = await cursor.fetchone()
            created_by = (
                ((coder_row["value"] if coder_row else "") or "").strip() or "AQDA User"
            )
            snapshot_id = _uuid()
            created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            await db.execute(
                "INSERT INTO project_snapshot "
                "(snapshot_id, project_id, parent_snapshot_id, revision, created_at, created_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    project_id,
                    current["head_snapshot_id"],
                    current["revision"],
                    created_at,
                    created_by,
                ),
            )
            await db.execute(
                "UPDATE project SET head_snapshot_id=? WHERE id=?", (snapshot_id, project_id)
            )

        # Full snapshot: keep trashed codes/codings so round-tripping is exact.
        project, documents, codes, codings, memos = await _load_project_data(
            project_id, include_deleted=True, db=db
        )
        doc_ids = [d["id"] for d in documents]
        doc_vars = []
        if doc_ids:
            placeholders = ",".join("?" * len(doc_ids))
            cursor = await db.execute(
                f"SELECT * FROM document_variable WHERE document_id IN ({placeholders})",
                doc_ids,
            )
            doc_vars = [dict(r) for r in await cursor.fetchall()]
        cursor = await db.execute(
            "SELECT snapshot_id, parent_snapshot_id, revision, created_at, created_by "
            "FROM project_snapshot WHERE project_id=? ORDER BY created_at",
            (project_id,),
        )
        snapshot_history = [dict(row) for row in await cursor.fetchall()]
        cursor = await db.execute(
            "SELECT id, root_code_id, deleted_at FROM code_deletion_batch "
            "WHERE project_id=? ORDER BY deleted_at",
            (project_id,),
        )
        deletion_batches = [dict(row) for row in await cursor.fetchall()]
        await db.commit()
    except Exception:
        if db.in_transaction:
            await db.rollback()
        raise
    finally:
        await db.close()

    # Building the standalone file is synchronous SQLite work that can take
    # seconds for projects with audio; keep it off the event loop so the UI
    # stays responsive while the background sync publishes a snapshot.
    data = await asyncio.to_thread(
        _write_standalone_snapshot,
        project, documents, codes, codings, memos,
        doc_vars, snapshot_history, deletion_batches,
    )
    return data, project, snapshot_id


def _write_standalone_snapshot(
    project: dict,
    documents: list[dict],
    codes: list[dict],
    codings: list[dict],
    memos: list[dict],
    doc_vars: list[dict],
    snapshot_history: list[dict],
    deletion_batches: list[dict],
) -> bytes:
    """Write one project into a fresh single-project database and return its bytes."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    con = None
    try:
        con = sqlite3.connect(tmp.name)
        con.executescript(SCHEMA)

        # Insert project with id=1
        con.execute(
            "INSERT INTO project (id, name, description, created_at, modified_at, lineage_id, "
            "revision, head_snapshot_id) VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
            (
                project["name"],
                project["description"],
                project["created_at"],
                project["modified_at"],
                project["lineage_id"],
                project["revision"],
                project["head_snapshot_id"],
            ),
        )

        for snapshot in snapshot_history:
            con.execute(
                "INSERT INTO project_snapshot "
                "(snapshot_id, project_id, parent_snapshot_id, revision, created_at, created_by) "
                "VALUES (?, 1, ?, ?, ?, ?)",
                (
                    snapshot["snapshot_id"],
                    snapshot["parent_snapshot_id"],
                    snapshot["revision"],
                    snapshot["created_at"],
                    snapshot["created_by"],
                ),
            )

        # Documents — remap IDs sequentially
        doc_map: dict[int, int] = {}
        for i, doc in enumerate(documents, 1):
            doc_map[doc["id"]] = i
            con.execute(
                "INSERT INTO document (id, project_id, name, content, source_type, transcript, label, exclude_from_ai, created_at, modified_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (i, doc["name"], doc["content"], doc["source_type"], doc.get("transcript"),
                 doc.get("label", ""), doc.get("exclude_from_ai", 0), doc["created_at"], doc["modified_at"]),
            )

        # Document variables
        for dv in doc_vars:
            new_doc_id = doc_map.get(dv["document_id"])
            if new_doc_id:
                con.execute(
                    "INSERT INTO document_variable (document_id, key, value) VALUES (?, ?, ?)",
                    (new_doc_id, dv["key"], dv["value"]),
                )

        # Codes — remap IDs; build the full map first so parent references
        # resolve regardless of insertion order (re-parented codes can have
        # parents with higher IDs)
        code_map: dict[int, int] = {}
        for i, code in enumerate(codes, 1):
            code_map[code["id"]] = i
        for batch in deletion_batches:
            root_code_id = code_map.get(batch["root_code_id"])
            if root_code_id is not None:
                con.execute(
                    "INSERT INTO code_deletion_batch "
                    "(id, project_id, root_code_id, deleted_at) VALUES (?, 1, ?, ?)",
                    (batch["id"], root_code_id, batch["deleted_at"]),
                )
        for i, code in enumerate(codes, 1):
            new_parent = code_map.get(code["parent_id"]) if code["parent_id"] else None
            con.execute(
                "INSERT INTO code (id, project_id, parent_id, name, description, color, "
                "sort_order, created_at, deleted_at, deletion_batch_id) "
                "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
                (i, new_parent, code["name"], code["description"], code["color"],
                 code["sort_order"], code["created_at"], code.get("deleted_at"),
                 code.get("deletion_batch_id")),
            )

        # Codings
        coding_map: dict[int, int] = {}
        for cg in codings:
            new_doc = doc_map.get(cg["document_id"])
            new_code = code_map.get(cg["code_id"])
            if new_doc and new_code:
                cursor = con.execute(
                    "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
                    "coder, created_at, deleted_at, deletion_batch_id, offset_unit, repair_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_doc, new_code, cg["start_pos"], cg["end_pos"], cg["selected_text"],
                     cg.get("coder", ""), cg["created_at"], cg.get("deleted_at"),
                     cg.get("deletion_batch_id"), cg.get("offset_unit", "codepoint"),
                     cg.get("repair_status")),
                )
                coding_map[cg["id"]] = cursor.lastrowid

        # Memos
        for memo in memos:
            con.execute(
                "INSERT INTO memo (project_id, document_id, code_id, coding_id, start_pos, "
                "end_pos, title, content, created_at, modified_at, offset_unit, repair_status) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc_map.get(memo["document_id"]), code_map.get(memo["code_id"]),
                 coding_map.get(memo.get("coding_id")),
                 memo.get("start_pos"), memo.get("end_pos"),
                 memo["title"], memo["content"], memo["created_at"], memo["modified_at"],
                 memo.get("offset_unit", "codepoint"), memo.get("repair_status")),
            )

        con.commit()
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError(f"Snapshot integrity check failed: {integrity}")
        con.close()
        con = None

        # Read the file into memory so we can clean up the temp file
        with open(tmp.name, "rb") as f:
            data = f.read()
    finally:
        if con is not None:
            con.close()
        os.unlink(tmp.name)

    return data


@router.get("/{project_id}/aqda")
async def export_aqda(project_id: int):
    """Download an immutable, lineage-aware project snapshot."""
    data, project, snapshot_id = await build_aqda_snapshot(project_id)
    slug = project["name"].replace(" ", "_")
    filename = f"{slug}_r{project['revision']}_{snapshot_id[:8]}.aqda"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers=_download_headers(filename),
    )
