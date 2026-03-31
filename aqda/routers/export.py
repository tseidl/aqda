"""Export routes — REFI-QDA (.qdpx), codebook (.qdc), CSV, JSON, standalone .aqda."""

import csv
import io
import json
import os
import sqlite3
import tempfile
import uuid
import zipfile
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.sax.saxutils import escape as xml_escape

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from aqda.db import get_db, SCHEMA

router = APIRouter()


def _uuid() -> str:
    return str(uuid.uuid4())


def _safe_xml_text(text: str) -> str:
    """Escape text for safe inclusion in XML, handling CDATA-breaking sequences."""
    return xml_escape(text)


async def _load_project_data(project_id: int):
    """Load all project data for export."""
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

        cursor = await db.execute(
            "SELECT * FROM code WHERE project_id=? ORDER BY parent_id NULLS FIRST, sort_order",
            (project_id,),
        )
        codes = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT cg.*, c.name as code_name FROM coding cg "
            "JOIN code c ON c.id=cg.code_id "
            "JOIN document d ON d.id=cg.document_id "
            "WHERE d.project_id=? ORDER BY cg.document_id, cg.start_pos",
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
        await db.close()


# --- REFI-QDA Export ---

def _build_code_tree(codes: list[dict], parent_id=None) -> list[dict]:
    """Build hierarchical code tree."""
    children = [c for c in codes if c["parent_id"] == parent_id]
    for child in children:
        child["children"] = _build_code_tree(codes, child["id"])
    return children


def _add_codes_xml(parent_el, codes_tree: list[dict], guid_map: dict):
    """Recursively add codes to XML."""
    for code in codes_tree:
        guid = _uuid()
        guid_map[("code", code["id"])] = guid
        code_el = SubElement(parent_el, "Code", {
            "guid": guid,
            "name": code["name"],
            "isCodable": "true",
            "color": code["color"],
        })
        if code.get("description"):
            desc = SubElement(code_el, "Description")
            desc.text = _safe_xml_text(code["description"])
        if code.get("children"):
            _add_codes_xml(code_el, code["children"], guid_map)


@router.get("/{project_id}/qdpx")
async def export_qdpx(project_id: int):
    """Export project as REFI-QDA .qdpx file."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    guid_map = {}
    user_guid = _uuid()

    # Build XML
    root = Element("Project", {
        "xmlns": "urn:QDA-XML:project:1.0",
        "name": project["name"],
        "origin": "AQDA",
        "creatingUserGUID": user_guid,
        "creationDateTime": project["created_at"] + "Z" if project["created_at"] else "",
    })

    if project.get("description"):
        desc = SubElement(root, "Description")
        desc.text = _safe_xml_text(project["description"])

    # Users
    users_el = SubElement(root, "Users")
    SubElement(users_el, "User", {"guid": user_guid, "name": "AQDA User"})

    # CodeBook
    codebook_el = SubElement(root, "CodeBook")
    codes_el = SubElement(codebook_el, "Codes")
    code_tree = _build_code_tree(codes)
    _add_codes_xml(codes_el, code_tree, guid_map)

    # Sources (documents)
    sources_el = SubElement(root, "Sources")
    codings_by_doc = {}
    for cg in codings:
        codings_by_doc.setdefault(cg["document_id"], []).append(cg)

    for doc in documents:
        doc_guid = _uuid()
        guid_map[("doc", doc["id"])] = doc_guid
        source_el = SubElement(sources_el, "TextSource", {
            "guid": doc_guid,
            "name": doc["name"],
            "plainTextPath": f"sources/{doc['name']}.txt",
            "creatingUser": user_guid,
            "creationDateTime": doc["created_at"] + "Z" if doc["created_at"] else "",
        })

        # Add coded selections
        for cg in codings_by_doc.get(doc["id"], []):
            sel_guid = _uuid()
            sel_el = SubElement(source_el, "PlainTextSelection", {
                "guid": sel_guid,
                "startPosition": str(cg["start_pos"]),
                "endPosition": str(cg["end_pos"]),
                "creatingUser": user_guid,
                "creationDateTime": cg["created_at"] + "Z" if cg["created_at"] else "",
            })
            coding_el = SubElement(sel_el, "Coding", {
                "guid": _uuid(),
                "creatingUser": user_guid,
                "creationDateTime": cg["created_at"] + "Z" if cg["created_at"] else "",
            })
            code_guid = guid_map.get(("code", cg["code_id"]), _uuid())
            SubElement(coding_el, "CodeRef", {"targetGUID": code_guid})

    # Notes (memos)
    if memos:
        notes_el = SubElement(root, "Notes")
        for memo in memos:
            note_guid = _uuid()
            note_el = SubElement(notes_el, "Note", {
                "guid": note_guid,
                "name": memo["title"] or "Memo",
                "creatingUser": user_guid,
                "creationDateTime": memo["created_at"] + "Z" if memo["created_at"] else "",
            })
            content_el = SubElement(note_el, "PlainTextContent")
            content_el.text = _safe_xml_text(memo["content"])

    # Build ZIP
    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.qde", xml_bytes)
        for doc in documents:
            zf.writestr(f"sources/{doc['name']}.txt", doc["content"])

    buf.seek(0)
    filename = f"{project['name'].replace(' ', '_')}.qdpx"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/qdc")
async def export_codebook(project_id: int):
    """Export codebook as REFI-QDA .qdc file."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    guid_map = {}
    root = Element("CodeBook", {
        "xmlns": "urn:QDA-XML:codebook:1.0",
        "origin": "AQDA",
    })
    codes_el = SubElement(root, "Codes")
    code_tree = _build_code_tree(codes)
    _add_codes_xml(codes_el, code_tree, guid_map)

    xml_bytes = b'<?xml version="1.0" encoding="UTF-8"?>\n' + tostring(root, encoding="unicode").encode("utf-8")

    filename = f"{project['name'].replace(' ', '_')}_codebook.qdc"
    return StreamingResponse(
        io.BytesIO(xml_bytes),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/csv")
async def export_csv(project_id: int):
    """Export coded segments as CSV."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["document", "code", "start_pos", "end_pos", "text", "created_at"])
    for cg in codings:
        doc_name = next((d["name"] for d in documents if d["id"] == cg["document_id"]), "")
        writer.writerow([
            doc_name,
            cg["code_name"],
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/json")
async def export_json(project_id: int):
    """Export full project as JSON."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    # Strip content from documents in the top-level list (include separately)
    docs_meta = [{k: v for k, v in d.items() if k != "content"} for d in documents]

    data = {
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
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{project_id}/aqda")
async def export_aqda(project_id: int):
    """Export a single project as a standalone .aqda (SQLite) file for sharing."""
    project, documents, codes, codings, memos = await _load_project_data(project_id)

    # Also load document variables
    db = await get_db()
    try:
        doc_ids = [d["id"] for d in documents]
        doc_vars = []
        if doc_ids:
            placeholders = ",".join("?" * len(doc_ids))
            cursor = await db.execute(
                f"SELECT * FROM document_variable WHERE document_id IN ({placeholders})",
                doc_ids,
            )
            doc_vars = [dict(r) for r in await cursor.fetchall()]
    finally:
        await db.close()

    # Build a standalone SQLite database with just this project
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        con = sqlite3.connect(tmp.name)
        con.executescript(SCHEMA)

        # Insert project with id=1
        con.execute(
            "INSERT INTO project (id, name, description, created_at, modified_at) VALUES (1, ?, ?, ?, ?)",
            (project["name"], project["description"], project["created_at"], project["modified_at"]),
        )

        # Documents — remap IDs sequentially
        doc_map: dict[int, int] = {}
        for i, doc in enumerate(documents, 1):
            doc_map[doc["id"]] = i
            con.execute(
                "INSERT INTO document (id, project_id, name, content, source_type, transcript, created_at, modified_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                (i, doc["name"], doc["content"], doc["source_type"], doc.get("transcript"), doc["created_at"], doc["modified_at"]),
            )

        # Document variables
        for dv in doc_vars:
            new_doc_id = doc_map.get(dv["document_id"])
            if new_doc_id:
                con.execute(
                    "INSERT INTO document_variable (document_id, key, value) VALUES (?, ?, ?)",
                    (new_doc_id, dv["key"], dv["value"]),
                )

        # Codes — remap IDs, respecting hierarchy order
        code_map: dict[int, int] = {}
        for i, code in enumerate(codes, 1):
            code_map[code["id"]] = i
            new_parent = code_map.get(code["parent_id"]) if code["parent_id"] else None
            con.execute(
                "INSERT INTO code (id, project_id, parent_id, name, description, color, sort_order, created_at, deleted_at) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
                (i, new_parent, code["name"], code["description"], code["color"], code["sort_order"], code["created_at"], code.get("deleted_at")),
            )

        # Codings
        for cg in codings:
            new_doc = doc_map.get(cg["document_id"])
            new_code = code_map.get(cg["code_id"])
            if new_doc and new_code:
                con.execute(
                    "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, created_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (new_doc, new_code, cg["start_pos"], cg["end_pos"], cg["selected_text"], cg["created_at"], cg.get("deleted_at")),
                )

        # Memos
        for memo in memos:
            con.execute(
                "INSERT INTO memo (project_id, document_id, code_id, title, content, created_at, modified_at) VALUES (1, ?, ?, ?, ?, ?, ?)",
                (doc_map.get(memo["document_id"]), code_map.get(memo["code_id"]),
                 memo["title"], memo["content"], memo["created_at"], memo["modified_at"]),
            )

        con.commit()
        con.close()

        # Read the file into memory so we can clean up the temp file
        with open(tmp.name, "rb") as f:
            data = f.read()
    finally:
        os.unlink(tmp.name)

    from datetime import date
    slug = project['name'].replace(' ', '_')
    filename = f"{slug}_{date.today().isoformat()}.aqda"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
