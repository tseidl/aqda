import io
import zipfile

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import aqda.db as db_module
from aqda.routers import documents, projects


def upload(data: bytes, name: str) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


TRANSITIONAL = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
STRICT = "http://purl.oclc.org/ooxml/wordprocessingml/main"


def make_docx(paragraphs: list[str], namespace: str = TRANSITIONAL, body: str | None = None) -> bytes:
    body = body if body is not None else "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            f'<w:document xmlns:w="{namespace}" '
            'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
            f"<w:body>{body}</w:body></w:document>",
        )
    return buf.getvalue()


def test_docx_parser_handles_strict_namespace_text_boxes_and_size_limits(monkeypatch):
    assert documents._extract_docx_text_sync(make_docx(["Strict"], namespace=STRICT)) == "Strict"

    # A text box: the outer paragraph holds a nested paragraph inside an
    # AlternateContent whose Fallback repeats the Choice content.
    text_box = (
        "<w:p><w:r><w:t>Outside</w:t></w:r>"
        "<mc:AlternateContent><mc:Choice><w:txbxContent>"
        "<w:p><w:r><w:t>Inside</w:t></w:r></w:p></w:txbxContent></mc:Choice>"
        "<mc:Fallback><w:txbxContent><w:p><w:r><w:t>Inside</w:t></w:r></w:p></w:txbxContent>"
        "</mc:Fallback></mc:AlternateContent></w:p>"
        "<w:p><w:r><w:t>After</w:t></w:r><w:tab/><w:r><w:t>tab</w:t></w:r></w:p>"
    )
    assert documents._extract_docx_text_sync(make_docx([], body=text_box)) == "Outside\nInside\nAfter\ttab"

    with pytest.raises(ValueError):
        documents._extract_docx_text_sync(
            make_docx(["x"], namespace="http://example.com/not-word")
        )
    monkeypatch.setattr(documents, "MAX_DOCX_XML_BYTES", 64)
    with pytest.raises(ValueError, match="too large"):
        documents._extract_docx_text_sync(make_docx(["y" * 200]))


# Alternative representations of the same Word content must be imported only once.
@pytest.mark.parametrize("nested", [False, True])
def test_docx_alternative_choices_do_not_duplicate_text(nested):
    text = "<w:r><w:t>Inside</w:t></w:r>"
    if nested:
        text = f"<w:txbxContent><w:p>{text}</w:p></w:txbxContent>"
    body = (
        "<w:p><mc:AlternateContent>"
        f'<mc:Choice Requires="w">{text}</mc:Choice>'
        f'<mc:Choice Requires="w">{text}</mc:Choice>'
        f"<mc:Fallback>{text}</mc:Fallback>"
        "</mc:AlternateContent></w:p>"
    )
    assert documents._extract_docx_text_sync(make_docx([], body=body)).strip() == "Inside"


# Keep fallback text when the preferred representation has no extractable Word text.
def test_docx_uses_text_fallback_for_nontext_choice():
    body = (
        "<w:p><mc:AlternateContent>"
        '<mc:Choice Requires="w"><w:drawing/></mc:Choice>'
        "<mc:Fallback><w:r><w:t>Fallback text</w:t></w:r></mc:Fallback>"
        "</mc:AlternateContent></w:p>"
    )
    assert documents._extract_docx_text_sync(make_docx([], body=body)) == "Fallback text"


@pytest.mark.asyncio
async def test_docx_imports_as_text_and_other_office_formats_are_refused(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "docx")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Word"))

    doc = await documents.upload_document(
        project_id=project["id"],
        file=upload(make_docx(["First paragraph.", "Second one."]), "interview.docx"),
    )
    assert doc["source_type"] == "text"
    stored = await documents.get_document(doc["id"])
    assert stored["content"] == "First paragraph.\nSecond one."

    with pytest.raises(HTTPException) as refused:
        await documents.upload_document(
            project_id=project["id"], file=upload(b"\xd0\xcf\x11\xe0 legacy", "notes.doc")
        )
    assert refused.value.status_code == 400
    assert "docx" in refused.value.detail

    result = await documents.upload_documents_bulk(
        project_id=project["id"],
        files=[
            upload(make_docx(["Bulk paragraph."]), "bulk.docx"),
            upload(b"not really a word file", "broken.docx"),
            upload(b"{\\rtf1 hello}", "old.rtf"),
        ],
    )
    assert [d["name"] for d in result["documents"]] == ["bulk.docx"]
    assert {s["name"]: s["reason"] for s in result["skipped"]} == {
        "broken.docx": "could not be read",
        "old.rtf": documents.UNSUPPORTED_MESSAGE,
    }
