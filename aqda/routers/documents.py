"""Document management routes."""

import base64
import re
from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from aqda.db import get_db, MAX_UPLOAD_BYTES

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tiff', '.tif'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.ogg', '.flac', '.webm', '.wma', '.aac'}

AUDIO_MIME_MAP = {
    '.mp3': 'audio/mpeg',
    '.wav': 'audio/wav',
    '.m4a': 'audio/mp4',
    '.ogg': 'audio/ogg',
    '.flac': 'audio/flac',
    '.webm': 'audio/webm',
    '.wma': 'audio/x-ms-wma',
    '.aac': 'audio/aac',
}

router = APIRouter()


class DocumentUpdate(BaseModel):
    name: str | None = None


class VariableUpdate(BaseModel):
    key: str
    value: str


async def _get_doc_variables(db, document_id: int) -> dict[str, str]:
    cursor = await db.execute(
        "SELECT key, value FROM document_variable WHERE document_id=?",
        (document_id,),
    )
    rows = await cursor.fetchall()
    return {r["key"]: r["value"] for r in rows}


async def _get_all_doc_variables(db, project_id: int) -> dict[int, dict[str, str]]:
    cursor = await db.execute(
        "SELECT dv.document_id, dv.key, dv.value "
        "FROM document_variable dv "
        "JOIN document d ON d.id = dv.document_id "
        "WHERE d.project_id=?",
        (project_id,),
    )
    rows = await cursor.fetchall()
    result: dict[int, dict[str, str]] = {}
    for r in rows:
        result.setdefault(r["document_id"], {})[r["key"]] = r["value"]
    return result


@router.get("")
async def list_documents(project_id: int):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT id, project_id, name, source_type, created_at, modified_at, "
            "LENGTH(content) as content_length "
            "FROM document WHERE project_id=? ORDER BY name",
            (project_id,),
        )
        rows = await cursor.fetchall()
        all_vars = await _get_all_doc_variables(db, project_id)
        docs = []
        for r in rows:
            d = dict(r)
            d["variables"] = all_vars.get(d["id"], {})
            docs.append(d)
        return docs
    finally:
        await db.close()


async def _parse_filename_variables(db, filename: str) -> dict[str, str]:
    """Parse meta-variables from a filename using a configured regex pattern.

    Settings:
      filename_pattern: regex with named groups, e.g. (?P<gender>[MF])(?P<age>\\d+)_(?P<city>\\w+)
      filename_variables: comma-separated variable names (fallback for positional groups)

    Example: pattern = "(?P<id>\\d+)_(?P<gender>[MF])(?P<age>\\d+)_(?P<city>\\w+)"
             filename = "Interview_01_M35_NYC_2024.txt"
    """
    cursor = await db.execute(
        "SELECT key, value FROM setting WHERE key IN ('filename_pattern', 'filename_variables')"
    )
    rows = await cursor.fetchall()
    settings = {r["key"]: r["value"] for r in rows}
    pattern = settings.get("filename_pattern", "").strip()
    if not pattern:
        return {}

    # Strip extension for matching
    name_without_ext = filename.rsplit('.', 1)[0] if '.' in filename else filename
    if len(pattern) > 500:
        return {}
    try:
        match = re.search(pattern, name_without_ext)
    except re.error:
        return {}

    if not match:
        return {}

    result = {}
    # Named groups
    named = match.groupdict()
    if named:
        result.update({k: v for k, v in named.items() if v is not None})
    else:
        # Positional groups with variable names from settings
        var_names = [v.strip() for v in settings.get("filename_variables", "").split(",") if v.strip()]
        groups = match.groups()
        for i, val in enumerate(groups):
            if val is not None and i < len(var_names):
                result[var_names[i]] = val
    return result


async def _save_doc_variables(db, document_id: int, variables: dict[str, str]):
    """Save variables for a document."""
    for key, value in variables.items():
        if value.strip():
            await db.execute(
                "INSERT INTO document_variable (document_id, key, value) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(document_id, key) DO UPDATE SET value=excluded.value",
                (document_id, key, value),
            )


def _detect_source_type(filename: str) -> str:
    ext = '.' + filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext == '.pdf':
        return 'pdf'
    if ext in IMAGE_EXTENSIONS:
        return 'image'
    if ext in AUDIO_EXTENSIONS:
        return 'audio'
    return 'text'


@router.post("", status_code=201)
async def upload_document(
    project_id: int = Form(...),
    file: UploadFile = File(...),
):
    content_bytes = await file.read()
    if len(content_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large. Maximum size is {MAX_UPLOAD_BYTES // (1024*1024)} MB.")
    filename = file.filename or "untitled"
    source_type = _detect_source_type(filename)

    if source_type == "pdf":
        text = await _extract_pdf_text(content_bytes)
    elif source_type == "image":
        ext = filename.rsplit('.', 1)[-1].lower()
        mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        text = f"data:{mime};base64,{base64.b64encode(content_bytes).decode('ascii')}"
    elif source_type == "audio":
        ext = '.' + filename.rsplit('.', 1)[-1].lower()
        mime = AUDIO_MIME_MAP.get(ext, 'audio/mpeg')
        text = f"data:{mime};base64,{base64.b64encode(content_bytes).decode('ascii')}"
    else:
        text = content_bytes.decode("utf-8", errors="replace")

    if not text.strip():
        raise HTTPException(400, "Document is empty or could not be read")

    db = await get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO document (project_id, name, content, source_type) VALUES (?, ?, ?, ?)",
            (project_id, filename, text, source_type),
        )
        await db.commit()
        doc_id = cursor.lastrowid
        # Parse filename variables
        file_vars = await _parse_filename_variables(db, filename)
        if file_vars:
            await _save_doc_variables(db, doc_id, file_vars)
            await db.commit()
        cursor = await db.execute(
            "SELECT id, project_id, name, source_type, created_at, modified_at "
            "FROM document WHERE id=?",
            (doc_id,),
        )
        return dict(await cursor.fetchone())
    finally:
        await db.close()


@router.post("/bulk", status_code=201)
async def upload_documents_bulk(
    project_id: int = Form(...),
    files: list[UploadFile] = File(...),
):
    results = []
    for file in files:
        content_bytes = await file.read()
        if len(content_bytes) > MAX_UPLOAD_BYTES:
            continue  # Skip oversized files in bulk upload
        filename = file.filename or "untitled"
        source_type = _detect_source_type(filename)
        if source_type == "pdf":
            text = await _extract_pdf_text(content_bytes)
        elif source_type == "image":
            ext = filename.rsplit('.', 1)[-1].lower()
            mime = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
            text = f"data:{mime};base64,{base64.b64encode(content_bytes).decode('ascii')}"
        elif source_type == "audio":
            ext = '.' + filename.rsplit('.', 1)[-1].lower()
            mime = AUDIO_MIME_MAP.get(ext, 'audio/mpeg')
            text = f"data:{mime};base64,{base64.b64encode(content_bytes).decode('ascii')}"
        else:
            text = content_bytes.decode("utf-8", errors="replace")
        if not text.strip():
            continue
        db = await get_db()
        try:
            cursor = await db.execute(
                "INSERT INTO document (project_id, name, content, source_type) VALUES (?, ?, ?, ?)",
                (project_id, filename, text, source_type),
            )
            await db.commit()
            doc_id = cursor.lastrowid
            # Parse filename variables
            file_vars = await _parse_filename_variables(db, filename)
            if file_vars:
                await _save_doc_variables(db, doc_id, file_vars)
                await db.commit()
            cursor = await db.execute(
                "SELECT id, project_id, name, source_type, created_at, modified_at "
                "FROM document WHERE id=?",
                (doc_id,),
            )
            results.append(dict(await cursor.fetchone()))
        finally:
            await db.close()
    return results


@router.get("/{document_id}")
async def get_document(document_id: int):
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM document WHERE id=?", (document_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        d = dict(row)
        d["variables"] = await _get_doc_variables(db, document_id)
        return d
    finally:
        await db.close()


@router.patch("/{document_id}")
async def update_document(document_id: int, data: DocumentUpdate):
    db = await get_db()
    try:
        if data.name:
            await db.execute(
                "UPDATE document SET name=?, modified_at=datetime('now') WHERE id=?",
                (data.name, document_id),
            )
            await db.commit()
        cursor = await db.execute("SELECT * FROM document WHERE id=?", (document_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        return dict(row)
    finally:
        await db.close()


@router.delete("/{document_id}", status_code=204)
async def delete_document(document_id: int):
    db = await get_db()
    try:
        await db.execute("DELETE FROM document WHERE id=?", (document_id,))
        await db.commit()
    finally:
        await db.close()


@router.get("/{document_id}/variables")
async def get_variables(document_id: int):
    db = await get_db()
    try:
        return await _get_doc_variables(db, document_id)
    finally:
        await db.close()


@router.put("/{document_id}/variables")
async def set_variables(document_id: int, items: list[VariableUpdate]):
    db = await get_db()
    try:
        for item in items:
            if item.value.strip():
                await db.execute(
                    "INSERT INTO document_variable (document_id, key, value) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(document_id, key) DO UPDATE SET value=excluded.value",
                    (document_id, item.key, item.value),
                )
            else:
                await db.execute(
                    "DELETE FROM document_variable WHERE document_id=? AND key=?",
                    (document_id, item.key),
                )
        await db.commit()
        return await _get_doc_variables(db, document_id)
    finally:
        await db.close()


@router.delete("/{document_id}/variables/{key}")
async def delete_variable(document_id: int, key: str):
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM document_variable WHERE document_id=? AND key=?",
            (document_id, key),
        )
        await db.commit()
        return await _get_doc_variables(db, document_id)
    finally:
        await db.close()


async def _extract_pdf_text(content_bytes: bytes) -> str:
    """Extract text from PDF bytes using pdfplumber."""
    import io
    import pdfplumber

    pages = []
    with pdfplumber.open(io.BytesIO(content_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


@router.post("/{document_id}/transcribe")
async def transcribe_audio(document_id: int):
    """Transcribe an audio document using faster-whisper. Replaces content with text."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise HTTPException(
            400,
            'Audio transcription requires faster-whisper. Install with: pipx inject aqda "aqda[audio]"',
        )

    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM document WHERE id=?", (document_id,))
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(404, "Document not found")
        doc = dict(row)

        if doc["source_type"] != "audio":
            raise HTTPException(400, "Document is not an audio file")

        # Decode base64 content back to bytes
        content = doc["content"]
        # content format: data:<mime>;base64,<data>
        if ";base64," not in content:
            raise HTTPException(400, "Invalid audio content format")
        b64_data = content.split(";base64,", 1)[1]
        audio_bytes = base64.b64decode(b64_data)

        # Determine file extension from mime type
        mime_part = content.split(";")[0].replace("data:", "")
        ext_map = {v: k for k, v in AUDIO_MIME_MAP.items()}
        ext = ext_map.get(mime_part, ".wav")

        # Write to temp file
        import tempfile
        import os

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            # Get whisper model from settings
            cursor = await db.execute(
                "SELECT value FROM setting WHERE key='whisper_model'"
            )
            setting_row = await cursor.fetchone()
            model_name = setting_row["value"] if setting_row else "base"

            # Transcribe
            model = WhisperModel(model_name, device="cpu", compute_type="int8")
            segments, _info = model.transcribe(tmp_path)
            transcript = " ".join(seg.text.strip() for seg in segments)

            if not transcript.strip():
                raise HTTPException(400, "Transcription produced no text")

            # Store transcript alongside audio (keeps original audio in content)
            await db.execute(
                "UPDATE document SET transcript=?, modified_at=datetime('now') "
                "WHERE id=?",
                (transcript, document_id),
            )
            await db.commit()

            cursor = await db.execute("SELECT * FROM document WHERE id=?", (document_id,))
            updated = await cursor.fetchone()
            d = dict(updated)
            d["variables"] = await _get_doc_variables(db, document_id)
            return d
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    finally:
        await db.close()
