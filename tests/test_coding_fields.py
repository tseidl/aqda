import uuid

import pytest
from fastapi import HTTPException

import aqda.db as db_module
from aqda.routers import codes, codings, projects
from aqda.routers import coding_fields as fields_router
from aqda.services.coding_fields import find_duplicate_coding

TEXT = "Alice says hello. Bob says no."


async def revision(project_id: int) -> int:
    db = await db_module.get_db()
    try:
        row = await (
            await db.execute("SELECT revision FROM project WHERE id=?", (project_id,))
        ).fetchone()
        return row["revision"]
    finally:
        await db.close()


async def scalar(sql: str, params=()):
    db = await db_module.get_db()
    try:
        return (await (await db.execute(sql, params)).fetchone())[0]
    finally:
        await db.close()


async def setup_project(tmp_path, use_data_dir, name="Fields"):
    use_data_dir(tmp_path / name)
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name=name))
    code = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Stance"))
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', ?)",
            (project["id"], TEXT),
        )
        await db.commit()
    finally:
        await db.close()
    return project["id"], doc.lastrowid, code["id"]


async def add_field(project_id: int, name: str, kind: str) -> dict:
    return await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=project_id, name=name, kind=kind)
    )


async def add_run(project_id: int, label="Model run", model="test-model") -> int:
    db = await db_module.get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO coding_run (project_id, uuid, label, model) VALUES (?, ?, ?, ?)",
            (project_id, str(uuid.uuid4()), label, model),
        )
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


def hello(document_id: int, code_id: int, fields=None) -> codings.CodingCreate:
    return codings.CodingCreate(
        document_id=document_id,
        code_id=code_id,
        start_pos=0,
        end_pos=5,
        selected_text="Alice",
        fields=fields,
    )


@pytest.mark.asyncio
async def test_coding_fields_are_created_listed_changed_and_deleted(tmp_path, use_data_dir):
    project_id, doc_id, code_id = await setup_project(tmp_path, use_data_dir)

    start = await revision(project_id)
    actor = await add_field(project_id, "  Actor  ", "text")
    agrees = await add_field(project_id, "Agrees", "yes_no")
    count = await add_field(project_id, "Count", "number")
    assert actor["name"] == "Actor"
    assert [actor["sort_order"], agrees["sort_order"], count["sort_order"]] == [0, 1, 2]
    assert await revision(project_id) == start + 3

    with pytest.raises(HTTPException) as same_name:
        await add_field(project_id, "actor", "text")
    assert same_name.value.status_code == 409
    with pytest.raises(HTTPException) as bad_kind:
        await add_field(project_id, "Mood", "colour")
    assert bad_kind.value.status_code == 422
    with pytest.raises(HTTPException) as no_name:
        await add_field(project_id, "   ", "text")
    assert no_name.value.status_code == 422
    with pytest.raises(HTTPException) as no_project:
        await add_field(project_id + 99, "Actor", "text")
    assert no_project.value.status_code == 404

    await codings.create_coding(hello(doc_id, code_id, {"Actor": "Alice", "Count": "12"}))

    # Reorder and rename; a rename onto another field's name is refused.
    moved = await fields_router.update_coding_field(
        count["id"], fields_router.CodingFieldUpdate(sort_order=-1)
    )
    assert moved["sort_order"] == -1
    listed = await fields_router.list_coding_fields(project_id)
    assert [field["name"] for field in listed] == ["Count", "Actor", "Agrees"]
    assert {field["name"]: field["value_count"] for field in listed} == {
        "Count": 1, "Actor": 1, "Agrees": 0,
    }
    renamed = await fields_router.update_coding_field(
        actor["id"], fields_router.CodingFieldUpdate(name="Speaker")
    )
    assert renamed["name"] == "Speaker"
    with pytest.raises(HTTPException) as taken:
        await fields_router.update_coding_field(
            actor["id"], fields_router.CodingFieldUpdate(name="AGREES")
        )
    assert taken.value.status_code == 409

    # A new kind is accepted only when every existing answer fits it.
    with pytest.raises(HTTPException) as unfit:
        await fields_router.update_coding_field(
            actor["id"], fields_router.CodingFieldUpdate(kind="number")
        )
    assert unfit.value.status_code == 409
    as_text = await fields_router.update_coding_field(
        count["id"], fields_router.CodingFieldUpdate(kind="text")
    )
    assert as_text["kind"] == "text"

    before = await revision(project_id)
    await fields_router.delete_coding_field(actor["id"])
    assert await revision(project_id) == before + 1
    assert await scalar(
        "SELECT COUNT(*) FROM coding_field_value WHERE field_id=?", (actor["id"],)
    ) == 0
    listed = await codings.list_codings(document_id=doc_id)
    assert listed[0]["fields"] == {"Count": "12"}
    with pytest.raises(HTTPException) as gone:
        await fields_router.delete_coding_field(actor["id"])
    assert gone.value.status_code == 404


@pytest.mark.asyncio
async def test_coding_answers_are_validated_and_saved_with_the_coding(tmp_path, use_data_dir):
    project_id, doc_id, code_id = await setup_project(tmp_path, use_data_dir)
    await add_field(project_id, "Actor", "text")
    await add_field(project_id, "Agrees", "yes_no")
    await add_field(project_id, "Count", "number")

    created = await codings.create_coding(
        hello(doc_id, code_id, {"actor": "  Alice ", "Agrees": "yes", "Count": 3.0})
    )
    assert created["fields"] == {"Actor": "Alice", "Agrees": "1", "Count": "3"}
    assert created["run_id"] is None and created["run_label"] is None

    start = await revision(project_id)
    codings_before = await scalar("SELECT COUNT(*) FROM coding")
    refusals = [
        ({"Count": "12.5"}, "whole number"),
        ({"Agrees": "maybe"}, "yes or no"),
        ({"Count": True}, "whole number"),
        ({"Actor": 7}, "text"),
        ({"Mood": "calm"}, "“Mood”"),
    ]
    for answers, message in refusals:
        with pytest.raises(HTTPException) as refused:
            await codings.create_coding(hello(doc_id, code_id, answers))
        assert refused.value.status_code == 422
        assert message in refused.value.detail
    # A refused answer saves neither the coding nor anything else.
    assert await scalar("SELECT COUNT(*) FROM coding") == codings_before
    assert await revision(project_id) == start

    # Replacing answers drops the ones left out; an empty answer means unanswered.
    replaced = await codings.replace_coding_fields(
        created["id"], codings.CodingFieldsUpdate(fields={"Agrees": False, "Actor": " "})
    )
    assert replaced["fields"] == {"Agrees": "0"}
    assert await revision(project_id) == start + 1
    with pytest.raises(HTTPException) as invalid:
        await codings.replace_coding_fields(
            created["id"], codings.CodingFieldsUpdate(fields={"Count": "many"})
        )
    assert invalid.value.status_code == 422
    assert (await codings.list_codings(document_id=doc_id))[0]["fields"] == {"Agrees": "0"}

    await codings.delete_coding(created["id"])
    with pytest.raises(HTTPException) as deleted:
        await codings.replace_coding_fields(
            created["id"], codings.CodingFieldsUpdate(fields={})
        )
    assert deleted.value.status_code == 404


@pytest.mark.asyncio
async def test_codings_list_names_their_run_and_model_answers_stay_fixed(tmp_path, use_data_dir):
    project_id, doc_id, code_id = await setup_project(tmp_path, use_data_dir)
    await add_field(project_id, "Actor", "text")
    run_id = await add_run(project_id, label="Run 1", model="llama")
    db = await db_module.get_db()
    try:
        cursor = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "coder, run_id) VALUES (?, ?, 0, 5, 'Alice', 'Run 1', ?)",
            (doc_id, code_id, run_id),
        )
        model_coding = cursor.lastrowid
        await db.execute(
            "INSERT INTO coding_field_value (coding_id, field_id, value) "
            "SELECT ?, id, 'Alice' FROM coding_field WHERE name='Actor'",
            (model_coding,),
        )
        await db.commit()
    finally:
        await db.close()
    hand = await codings.create_coding(hello(doc_id, code_id))

    listed = {row["id"]: row for row in await codings.list_codings(project_id=project_id)}
    assert listed[model_coding]["run_id"] == run_id
    assert listed[model_coding]["run_label"] == "Run 1"
    assert listed[model_coding]["run_model"] == "llama"
    assert listed[model_coding]["fields"] == {"Actor": "Alice"}
    assert listed[hand["id"]]["run_label"] is None
    assert listed[hand["id"]]["fields"] == {}

    with pytest.raises(HTTPException) as fixed:
        await codings.replace_coding_fields(
            model_coding, codings.CodingFieldsUpdate(fields={"Actor": "Bob"})
        )
    assert fixed.value.status_code == 409


@pytest.mark.asyncio
async def test_only_an_exact_repetition_is_a_duplicate(tmp_path, use_data_dir):
    project_id, doc_id, code_id = await setup_project(tmp_path, use_data_dir)
    await add_field(project_id, "Actor", "text")
    await add_field(project_id, "Agrees", "yes_no")

    first = await codings.create_coding(hello(doc_id, code_id, {"Actor": "Alice"}))
    with pytest.raises(HTTPException) as repeated:
        await codings.create_coding(hello(doc_id, code_id, {"actor": "Alice"}))
    assert repeated.value.status_code == 409
    # With coding fields, the refusal says that the answers are the same too.
    assert repeated.value.detail == (
        "This code is already applied to this passage with exactly these answers."
    )
    # Other answers on the same passage are another statement (for example another actor).
    await codings.create_coding(hello(doc_id, code_id, {"Actor": "Bob"}))
    # More answers than the existing coding is also another statement.
    await codings.create_coding(hello(doc_id, code_id, {"Actor": "Alice", "Agrees": "no"}))
    await codings.create_coding(hello(doc_id, code_id))
    with pytest.raises(HTTPException):
        await codings.create_coding(hello(doc_id, code_id))

    # A model coding of the same passage does not block the researcher's own coding.
    run_id = await add_run(project_id)
    db = await db_module.get_db()
    try:
        await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "coder, run_id) VALUES (?, ?, 0, 5, 'Alice', 'Model run', ?)",
            (doc_id, code_id, run_id),
        )
        model_coding = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]
        await db.execute(
            "UPDATE setting SET value='Second coder' WHERE key='coder_name'"
        )
        await db.commit()
        # Within a run, the rule compares the run and the answers, not the coder name.
        assert await find_duplicate_coding(
            db, document_id=doc_id, code_id=code_id, start_pos=0, end_pos=5,
            run_id=run_id, coder="", answers={},
        ) == model_coding
        other_run = await add_run(project_id, label="Other run")
        assert await find_duplicate_coding(
            db, document_id=doc_id, code_id=code_id, start_pos=0, end_pos=5,
            run_id=other_run, coder="", answers={},
        ) is None
    finally:
        await db.close()

    # Another coder name is another coder.
    second = await codings.create_coding(hello(doc_id, code_id, {"Actor": "Alice"}))
    assert second["coder"] == "Second coder"

    # Changing answers onto an exact copy of another coding is refused as well.
    with pytest.raises(HTTPException) as onto_copy:
        bob = [
            row for row in await codings.list_codings(document_id=doc_id)
            if row["fields"] == {"Actor": "Bob"}
        ][0]
        await codings.replace_coding_fields(
            first["id"], codings.CodingFieldsUpdate(fields={"Actor": bob["fields"]["Actor"]})
        )
    assert onto_copy.value.status_code == 409


@pytest.mark.asyncio
async def test_suggestions_list_earlier_text_answers(tmp_path, use_data_dir):
    project_id, doc_id, code_id = await setup_project(tmp_path, use_data_dir)
    actor = await add_field(project_id, "Actor", "text")
    agrees = await add_field(project_id, "Agrees", "yes_no")
    other = await codes.create_code(codes.CodeCreate(project_id=project_id, name="Other"))
    for code, name in [(code_id, "Ministry"), (other["id"], "Ministry"), (code_id, "Union")]:
        await codings.create_coding(hello(doc_id, code, {"Actor": name, "Agrees": "yes"}))
    removed = await codings.create_coding(hello(doc_id, other["id"], {"Actor": "Typo"}))
    await codings.delete_coding(removed["id"])

    assert await fields_router.coding_field_suggestions(actor["id"]) == ["Ministry", "Union"]
    assert await fields_router.coding_field_suggestions(actor["id"], q="uni") == ["Union"]
    assert await fields_router.coding_field_suggestions(agrees["id"]) == []
    with pytest.raises(HTTPException):
        await fields_router.coding_field_suggestions(actor["id"] + 99)


@pytest.mark.asyncio
async def test_no_coding_field_is_added_to_a_project_in_the_trash(tmp_path, use_data_dir):
    project_id, _, _ = await setup_project(tmp_path, use_data_dir)
    await projects.delete_project(project_id)
    with pytest.raises(HTTPException) as trashed:
        await add_field(project_id, "Actor", "text")
    assert trashed.value.status_code == 404
    await projects.restore_project(project_id)
    assert (await add_field(project_id, "Actor", "text"))["name"] == "Actor"
