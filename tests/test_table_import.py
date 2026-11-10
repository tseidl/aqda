import io
from datetime import datetime

import pytest
from fastapi import HTTPException
from openpyxl import Workbook
from starlette.datastructures import UploadFile

import aqda.db as db_module
from aqda.routers import documents, projects


def upload(data: bytes, name: str) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


# A German-Excel style export: UTF-8 with BOM, semicolons, a quoted multi-line answer,
# a repeated and an empty header, a blank line, and a row without an answer.
SURVEY_CSV = (
    "﻿id;answer;group;group;\n"
    "r1;\"I moved here for work.\nThen I stayed.\";A;x;\n"
    "\n"
    "r2;;B;y;\n"
    ";Short one;A;;\n"
).encode("utf-8")


def make_xlsx() -> bytes:
    workbook = Workbook()
    first = workbook.active
    first.title = "Notes"
    first.append(["only"])
    first.append(["ignored"])
    second = workbook.create_sheet("Responses")
    second.append(["respondent", "age", "date", "response"])
    second.append(["P1", 34.0, datetime(2026, 5, 1), "A long answer about the town."])
    second.append(["P2", 51, datetime(2026, 5, 2, 14, 30), "Another answer."])
    buf = io.BytesIO()
    workbook.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_csv_preview_detects_delimiter_headers_and_text_column():
    preview = await documents.preview_table(file=upload(SURVEY_CSV, "survey.csv"), sheet=None, skip_rows=None)
    assert preview["columns"] == ["id", "answer", "group", "group (2)"]
    assert preview["row_count"] == 3
    assert preview["rows"][0][1] == "I moved here for work.\nThen I stayed."
    assert preview["suggested_text_column"] == "answer"
    assert preview["sheets"] == []


@pytest.mark.asyncio
async def test_csv_import_creates_one_document_per_row_with_variables(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "csv")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Survey"))

    result = await documents.import_table(
        project_id=project["id"],
        file=upload(SURVEY_CSV, "survey.csv"),
        text_column="answer",
        name_column="id",
        variable_columns='["group", "group (2)", "answer"]',
        sheet=None,
        skip_rows=0,
    )
    assert result["imported"] == 2 and result["skipped_empty"] == 1

    docs = await documents.list_documents(project["id"])
    by_text = {
        (await documents.get_document(d["id"]))["content"]: d for d in docs
    }
    first = by_text["I moved here for work.\nThen I stayed."]
    assert first["name"] == "r1"
    assert first["variables"] == {"group": "A", "group (2)": "x"}
    # An empty name cell falls back to the file name and row number; empty values are not stored.
    second = by_text["Short one"]
    assert second["name"] == "survey – row 3"
    assert second["variables"] == {"group": "A"}

    with pytest.raises(HTTPException) as missing:
        await documents.import_table(
            project_id=project["id"], file=upload(SURVEY_CSV, "survey.csv"),
            text_column="nope", name_column=None, variable_columns="[]", sheet=None, skip_rows=0,
        )
    assert missing.value.status_code == 400

    with pytest.raises(HTTPException) as empty:
        await documents.import_table(
            project_id=project["id"], file=upload(b"id,answer\n1,\n2,\n", "blank.csv"),
            text_column="answer", name_column=None, variable_columns="[]", sheet=None, skip_rows=0,
        )
    assert empty.value.status_code == 400
    # A refused import adds nothing.
    assert len(await documents.list_documents(project["id"])) == 2

    await projects.delete_project(project["id"])
    with pytest.raises(HTTPException) as gone:
        await documents.import_table(
            project_id=project["id"], file=upload(SURVEY_CSV, "survey.csv"),
            text_column="answer", name_column=None, variable_columns="[]", sheet=None, skip_rows=0,
        )
    assert gone.value.status_code == 404


@pytest.mark.asyncio
async def test_xlsx_import_reads_chosen_sheet_and_formats_cells(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "xlsx")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Excel"))
    data = make_xlsx()

    preview = await documents.preview_table(file=upload(data, "study.xlsx"), sheet=None, skip_rows=None)
    assert preview["sheets"] == ["Notes", "Responses"] and preview["sheet"] == "Notes"
    preview = await documents.preview_table(file=upload(data, "study.xlsx"), sheet="Responses", skip_rows=None)
    assert preview["rows"][0] == ["P1", "34", "2026-05-01", "A long answer about the town."]
    assert preview["rows"][1][2] == "2026-05-02 14:30:00"
    assert preview["suggested_text_column"] == "response"

    result = await documents.import_table(
        project_id=project["id"], file=upload(data, "study.xlsx"),
        text_column="response", name_column=None,
        variable_columns='["respondent", "age"]', sheet="Responses", skip_rows=0,
    )
    assert result["imported"] == 2
    docs = sorted(await documents.list_documents(project["id"]), key=lambda d: d["name"])
    assert [d["name"] for d in docs] == ["study – row 1", "study – row 2"]
    assert docs[0]["variables"] == {"respondent": "P1", "age": "34"}


# A Qualtrics export: question wording and import IDs below the column names.
QUALTRICS_CSV = (
    'ResponseId,Q1,Q2\n'
    '"Response ID","Why did you move here?","Age"\n'
    '"{""ImportId"":""_recordId""}","{""ImportId"":""QID1_TEXT""}","{""ImportId"":""QID2""}"\n'
    'R_1,For work.,34\n'
    'R_2,For family.,51\n'
).encode("utf-8")


@pytest.mark.asyncio
async def test_survey_header_rows_are_skipped(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "skip")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Qualtrics"))

    preview = await documents.preview_table(
        file=upload(QUALTRICS_CSV, "qualtrics.csv"), sheet=None, skip_rows=None
    )
    assert preview["skip_rows"] == 2 and preview["skip_rows_suggested"]
    assert preview["row_count"] == 2 and preview["rows"][0][0] == "R_1"
    # The researcher's own number overrides the suggestion.
    preview = await documents.preview_table(
        file=upload(QUALTRICS_CSV, "qualtrics.csv"), sheet=None, skip_rows=0
    )
    assert preview["row_count"] == 4 and not preview["skip_rows_suggested"]
    # Ordinary tables skip nothing unless asked.
    plain = await documents.preview_table(file=upload(SURVEY_CSV, "survey.csv"), sheet=None, skip_rows=None)
    assert plain["skip_rows"] == 0

    result = await documents.import_table(
        project_id=project["id"], file=upload(QUALTRICS_CSV, "qualtrics.csv"),
        text_column="Q1", name_column="ResponseId", variable_columns='["Q2"]',
        sheet=None, skip_rows=2,
    )
    assert result["imported"] == 2
    docs = {d["name"]: d for d in await documents.list_documents(project["id"])}
    assert sorted(docs) == ["R_1", "R_2"] and docs["R_1"]["variables"] == {"Q2": "34"}

    with pytest.raises(HTTPException) as too_many:
        await documents.preview_table(file=upload(QUALTRICS_CSV, "qualtrics.csv"), sheet=None, skip_rows=9)
    assert too_many.value.status_code == 400 and "skipping 9" in too_many.value.detail


@pytest.mark.asyncio
async def test_spreadsheets_in_a_file_import_point_to_the_table_import(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "bulk")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Bulk"))
    result = await documents.upload_documents_bulk(
        project_id=project["id"],
        files=[
            upload(b"plain text", "note.txt"),
            upload(SURVEY_CSV, "survey.csv"),
            upload(b"\xd0\xcf\x11\xe0", "old.xls"),
        ],
    )
    assert [d["name"] for d in result["documents"]] == ["note.txt"]
    assert {s["name"]: s["reason"] for s in result["skipped"]} == {
        "survey.csv": documents.TABLE_MESSAGE,
        "old.xls": documents.OLD_EXCEL_MESSAGE,
    }


# A whole interview or report in one cell exceeds the CSV reader's default cell limit.
@pytest.mark.asyncio
async def test_table_import_accepts_very_long_cells(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "long-cell")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Reports"))
    report = "A long report. " * 12_000  # 180,000 characters
    data = f'id,text\nr1,"{report}"\nr2,Short\n'.encode()
    result = await documents.import_table(
        project_id=project["id"], file=upload(data, "reports.csv"), text_column="text",
        name_column="id", variable_columns="[]", sheet=None, skip_rows=0,
    )
    assert result["imported"] == 2
    first = await documents.get_document(result["first_document_id"])
    assert first["content"] == report.strip()
