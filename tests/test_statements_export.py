import csv
import io
import uuid

import pytest
from fastapi import HTTPException

import aqda.db as db_module
from aqda.routers import codes, codings, export, projects
from aqda.routers import coding_fields as fields_router

TEXT_A = "Grüße aus München – „Wir sagen ja“. 😀 Zweiter Satz."
TEXT_B = "Bob says no. Carol agrees."
FIXED = [
    "document", "code", "code_path", "coder", "run", "model", "accepted_from_run",
    "start_pos", "end_pos", "text", "created_at",
]


async def execute(sql: str, params=()):
    db = await db_module.get_db()
    try:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


async def response_bytes(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


async def statements(project_id: int) -> tuple[bytes, list[list[str]]]:
    response = await export.export_statements(project_id)
    data = await response_bytes(response)
    return data, list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))


async def add_coding(doc_id, code_id, text, start, end, coder="Timo", run_id=None, answers=None):
    coding_id = await execute(
        "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, coder, "
        "run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, '2026-11-13 10:00:00')",
        (doc_id, code_id, start, end, text[start:end], coder, run_id),
    )
    for field_id, value in (answers or {}).items():
        await execute(
            "INSERT INTO coding_field_value (coding_id, field_id, value) VALUES (?, ?, ?)",
            (coding_id, field_id, value),
        )
    return coding_id


@pytest.mark.asyncio
async def test_statements_export_rows_columns_and_values(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "statements")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Policy talk"))
    pid = project["id"]
    # Inserted in reverse name order so that sorting, not insertion, decides row order.
    doc_b = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'b-interview', ?)",
        (pid, TEXT_B),
    )
    doc_a = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'a-zeitung', ?)",
        (pid, TEXT_A),
    )
    await execute(
        "INSERT INTO document_variable (document_id, key, value) VALUES "
        "(?, 'year', '2024'), (?, 'outlet', 'SZ'), (?, 'year', '2025')",
        (doc_a, doc_a, doc_b),
    )
    concepts = await codes.create_code(codes.CodeCreate(project_id=pid, name="Concepts"))
    subsidies = await codes.create_code(
        codes.CodeCreate(project_id=pid, name="Subsidies", parent_id=concepts["id"])
    )
    other = await codes.create_code(codes.CodeCreate(project_id=pid, name="Other"))
    doomed = await codes.create_code(codes.CodeCreate(project_id=pid, name="Doomed"))
    field = {}
    for name, kind in (("Actor", "text"), ("Agrees", "yes_no"), ("Weight", "number")):
        created = await fields_router.create_coding_field(
            fields_router.CodingFieldCreate(project_id=pid, name=name, kind=kind)
        )
        field[name] = created["id"]
    run_id = await execute(
        "INSERT INTO coding_run (project_id, uuid, label, model, status) "
        "VALUES (?, ?, 'Run 1', 'qwen3.5:9b', 'finished')",
        (pid, str(uuid.uuid4())),
    )
    removed_run = await execute(
        "INSERT INTO coding_run (project_id, uuid, label, model, status, deleted_at) "
        "VALUES (?, ?, 'Old run', 'llama', 'finished', '2026-11-12 09:00:00')",
        (pid, str(uuid.uuid4())),
    )

    quote = TEXT_A.index("„")
    hand = await add_coding(
        doc_a, subsidies["id"], TEXT_A, quote, quote + 15,
        answers={field["Actor"]: "Ministerium", field["Agrees"]: "1", field["Weight"]: "3"},
    )
    model = await add_coding(
        doc_a, subsidies["id"], TEXT_A, quote, quote + 15, coder="Run 1", run_id=run_id,
        answers={field["Actor"]: "Ministerium", field["Agrees"]: "0"},
    )
    first = await add_coding(doc_a, other["id"], TEXT_A, 0, 5)
    in_b = await add_coding(doc_b, other["id"], TEXT_B, 0, 3, coder="")
    # Not active, so not exported: a removed coding, a coding under a trashed code, and a
    # coding of a removed run.
    removed = await add_coding(doc_a, other["id"], TEXT_A, 6, 9)
    await codings.delete_coding(removed)
    await add_coding(doc_b, doomed["id"], TEXT_B, 4, 8)
    await codes.delete_code(doomed["id"])
    # (Left without its own removal mark, so the run's removal alone must exclude it.)
    await execute(
        "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, coder, "
        "run_id) VALUES (?, ?, 0, 3, 'Bob', 'Old run', ?)",
        (doc_b, other["id"], removed_run),
    )

    data, table = await statements(pid)
    assert data.startswith(b"\xef\xbb\xbf")
    header, body = table[0], table[1:]
    assert header == FIXED + [
        "var_outlet", "var_year", "field_Actor", "field_Agrees", "field_Weight"
    ]
    assert len(body) == 4
    by_column = [dict(zip(header, row)) for row in body]
    # Rows ordered by document name, then position, then coding ID.
    assert [(row["document"], row["start_pos"]) for row in by_column] == [
        ("a-zeitung", "0"), ("a-zeitung", str(quote)), ("a-zeitung", str(quote)),
        ("b-interview", "0"),
    ]
    first_row, hand_row, model_row, b_row = by_column
    assert first_row["text"] == "Grüße" and first_row["code_path"] == "Other"
    assert hand_row == {
        "document": "a-zeitung", "code": "Subsidies", "code_path": "Concepts > Subsidies",
        "coder": "Timo", "run": "", "model": "", "accepted_from_run": "",
        "start_pos": str(quote), "end_pos": str(quote + 15),
        "text": TEXT_A[quote:quote + 15], "created_at": "2026-11-13 10:00:00",
        "var_outlet": "SZ", "var_year": "2024",
        "field_Actor": "Ministerium", "field_Agrees": "1", "field_Weight": "3",
    }
    assert model_row["coder"] == "Run 1" and model_row["run"] == "Run 1"
    assert model_row["model"] == "qwen3.5:9b"
    assert model_row["field_Agrees"] == "0" and model_row["field_Weight"] == ""
    assert b_row["var_outlet"] == "" and b_row["var_year"] == "2025"
    assert b_row["coder"] == "" and b_row["field_Actor"] == ""
    assert "„Wir sagen ja“" in hand_row["text"]
    assert hand < model and first and in_b

    response = await export.export_statements(pid)
    assert response.media_type == "text/csv"
    assert "Policy_talk_statements.csv" in response.headers["content-disposition"]

    # The codings CSV keeps its columns.
    codings_csv = (await response_bytes(await export.export_csv(pid))).decode("utf-8-sig")
    assert codings_csv.splitlines()[0] == (
        "document,code,coder,start_pos,end_pos,text,created_at"
    )

    with pytest.raises(HTTPException) as missing:
        await export.export_statements(999999)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_statements_headers_are_unique_and_deterministic(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "headers")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Headers"))
    pid = project["id"]
    doc_1 = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'one', 'abc')", (pid,)
    )
    doc_2 = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'two', 'abc')", (pid,)
    )
    # Keys that differ only in case or spacing, and one that looks like a numbered repeat.
    await execute(
        "INSERT INTO document_variable (document_id, key, value) VALUES "
        "(?, 'Party', 'x'), (?, 'party', 'y'), (?, 'a  b', '1'), (?, 'a b', '2'), "
        "(?, 'Party_2', 'z')",
        (doc_1, doc_2, doc_1, doc_2, doc_1),
    )
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=pid, name="Stance", kind="text")
    )
    code = await codes.create_code(codes.CodeCreate(project_id=pid, name="Theme"))
    await add_coding(doc_1, code["id"], "abc", 0, 2)

    _, first = await statements(pid)
    _, second = await statements(pid)
    header = first[0]
    assert header == second[0]
    assert len({name.casefold() for name in header}) == len(header)
    assert header[len(FIXED):] == [
        "var_Party", "var_Party_2", "var_a b", "var_a b_2", "var_party_3", "field_Stance",
    ]
    row = dict(zip(header, first[1]))
    assert row["var_Party"] == "x" and row["var_Party_2"] == "z"
    assert row["var_a b"] == "1" and row["var_a b_2"] == "" and row["var_party_3"] == ""
