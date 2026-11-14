import io
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import aqda.db as db_module
from aqda.routers import codes, codings, projects
from aqda.routers import coding_fields as fields_router
from aqda.routers.export import build_aqda_snapshot
from aqda.services import shared_projects as shared_service
from aqda.services.shared_projects import (
    open_shared_project,
    set_shared_root,
    share_project,
    sync_project,
)

TEXT = "Alice says hello. Bob says no."
NEW_TABLES = ["coding_run", "coding_run_document", "coding_field", "coding_field_value"]


def upload(data: bytes) -> UploadFile:
    return UploadFile(filename="project.aqda", file=io.BytesIO(data))


async def rows(sql: str, params=()) -> list[dict]:
    db = await db_module.get_db()
    try:
        return [dict(row) for row in await (await db.execute(sql, params)).fetchall()]
    finally:
        await db.close()


async def execute(sql: str, params=()):
    db = await db_module.get_db()
    try:
        cursor = await db.execute(sql, params)
        await db.commit()
        return cursor.lastrowid
    finally:
        await db.close()


def edit_package(data: bytes, *statements: str) -> bytes:
    """Return a copy of a snapshot changed by raw SQL (to imitate other AQDA versions)."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "edited.aqda"
        path.write_bytes(data)
        connection = sqlite3.connect(path)
        try:
            for statement in statements:
                connection.execute(statement)
            connection.commit()
        finally:
            connection.close()
        return path.read_bytes()


# What an AQDA from before coding runs and fields writes: no format marker, no new tables.
OLDER_FORMAT = (
    "DELETE FROM setting WHERE key='snapshot_format'",
    *(f"DROP TABLE {table}" for table in reversed(NEW_TABLES)),
)
NEWER_FORMAT = ("UPDATE setting SET value='3' WHERE key='snapshot_format'",)


async def build_run_project(name="Run study") -> dict:
    """A project with a run, a read document, model and hand codings, fields, and answers."""
    project = await projects.create_project(projects.ProjectCreate(name=name))
    project_id = project["id"]
    stance = await codes.create_code(codes.CodeCreate(project_id=project_id, name="Stance"))
    proposals = await codes.create_code(
        codes.CodeCreate(project_id=project_id, name="Proposed by Run 1")
    )
    doc_id = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', ?)",
        (project_id, TEXT),
    )
    other_doc = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'unread', 'nothing')",
        (project_id,),
    )
    actor = await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=project_id, name="Actor", kind="text")
    )
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=project_id, name="Agrees", kind="yes_no")
    )
    run_uuid = str(uuid.uuid4())
    run_id = await execute(
        "INSERT INTO coding_run (project_id, uuid, label, model, codebook, "
        "proposed_parent_code_id, status, allow_new_codes) "
        "VALUES (?, ?, 'Run 1', 'llama', '[{\"id\": 1}]', ?, 'finished', 1)",
        (project_id, run_uuid, proposals["id"]),
    )
    await execute(
        "INSERT INTO coding_run_document (run_id, document_id, text_hash, submitted, "
        "accepted, refused) VALUES (?, ?, 'hash', 3, 2, '[{\"quote\": \"x\"}]')",
        (run_id, doc_id),
    )
    model_coding = await execute(
        "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, coder, "
        "run_id) VALUES (?, ?, 18, 21, 'Bob', 'Run 1', ?)",
        (doc_id, stance["id"], run_id),
    )
    await execute(
        "INSERT INTO coding_field_value (coding_id, field_id, value) VALUES (?, ?, 'Bob')",
        (model_coding, actor["id"]),
    )
    await execute(
        "UPDATE project SET revision=revision+1 WHERE id=?", (project_id,)
    )
    hand = await codings.create_coding(
        codings.CodingCreate(
            document_id=doc_id, code_id=stance["id"], start_pos=0, end_pos=5,
            selected_text="Alice", fields={"Actor": "Alice", "Agrees": "yes"},
        )
    )
    return {
        "id": project_id, "run_uuid": run_uuid, "doc_id": doc_id, "other_doc": other_doc,
        "hand_coding": hand["id"],
    }


async def run_content(project_id: int) -> dict:
    """Describe a project's runs, fields, and answers by content, not by ID."""
    runs = await rows(
        "SELECT r.*, c.name AS parent_name FROM coding_run r "
        "LEFT JOIN code c ON c.id=r.proposed_parent_code_id WHERE r.project_id=?",
        (project_id,),
    )
    for run in runs:
        assert run["parent_name"] is None or run["proposed_parent_code_id"] in {
            row["id"] for row in await rows(
                "SELECT id FROM code WHERE project_id=?", (project_id,)
            )
        }
    run_docs = await rows(
        "SELECT r.uuid, d.name AS document, d.project_id, rd.text_hash, rd.submitted, "
        "rd.accepted, rd.refused FROM coding_run_document rd "
        "JOIN coding_run r ON r.id=rd.run_id JOIN document d ON d.id=rd.document_id "
        "WHERE r.project_id=?",
        (project_id,),
    )
    assert all(row["project_id"] == project_id for row in run_docs)
    codings_with_runs = await rows(
        "SELECT cg.selected_text, cg.coder, r.uuid, r.project_id AS run_project "
        "FROM coding cg JOIN document d ON d.id=cg.document_id "
        "LEFT JOIN coding_run r ON r.id=cg.run_id WHERE d.project_id=? "
        "ORDER BY cg.start_pos",
        (project_id,),
    )
    assert all(row["run_project"] in (None, project_id) for row in codings_with_runs)
    answers = await rows(
        "SELECT cg.selected_text, f.name, v.value, f.project_id AS field_project "
        "FROM coding_field_value v JOIN coding cg ON cg.id=v.coding_id "
        "JOIN document d ON d.id=cg.document_id JOIN coding_field f ON f.id=v.field_id "
        "WHERE d.project_id=? ORDER BY cg.start_pos, f.name",
        (project_id,),
    )
    assert all(row["field_project"] == project_id for row in answers)
    fields = await rows(
        "SELECT name, kind, sort_order FROM coding_field WHERE project_id=? ORDER BY id",
        (project_id,),
    )
    return {
        "runs": [
            (run["uuid"], run["label"], run["model"], run["codebook"], run["status"],
             run["allow_new_codes"], run["parent_name"])
            for run in runs
        ],
        "run_documents": [
            (row["uuid"], row["document"], row["text_hash"], row["submitted"],
             row["accepted"], row["refused"])
            for row in run_docs
        ],
        "codings": [(row["selected_text"], row["coder"], row["uuid"]) for row in codings_with_runs],
        "answers": [(row["selected_text"], row["name"], row["value"]) for row in answers],
        "fields": [(row["name"], row["kind"], row["sort_order"]) for row in fields],
    }


def describe_schema(path: Path) -> dict:
    """Columns, foreign keys, and indexes of the tables the run and field batch touches."""
    connection = sqlite3.connect(path)
    try:
        described = {}
        for table in ["coding", *NEW_TABLES]:
            indexes = []
            for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
                columns = [
                    row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")
                ]
                indexes.append((index[1], index[2], index[3], index[4], columns))
            described[table] = {
                "columns": connection.execute(f"PRAGMA table_info({table})").fetchall(),
                "foreign_keys": sorted(
                    row[2:] for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                ),
                "indexes": sorted(indexes),
            }
        return described
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_migration_from_version_11_matches_a_fresh_database(tmp_path, use_data_dir):
    fresh = use_data_dir(tmp_path / "fresh")
    await db_module.init_db()

    # Rebuild the version 11 schema: no run or field tables, no run columns on coding.
    schema_v11 = db_module.SCHEMA
    for statement in db_module._V12_STATEMENTS:
        schema_v11 = schema_v11.replace(f"\n{statement};\n", "")
    schema_v11 = schema_v11.replace(
        ",\n    run_id INTEGER DEFAULT NULL REFERENCES coding_run(id)"
        ",\n    accepted_from_run_id INTEGER DEFAULT NULL REFERENCES coding_run(id)\n",
        "\n",
    ).replace(
        f"('schema_version', '{db_module.LATEST_SCHEMA_VERSION}')", "('schema_version', '11')"
    )
    assert "coding_run" not in schema_v11 and "run_id" not in schema_v11
    old = use_data_dir(tmp_path / "old")
    legacy = sqlite3.connect(old / "aqda.db")
    try:
        legacy.executescript(schema_v11)
        legacy.executescript(
            """
            INSERT INTO project (id, name, lineage_id) VALUES (1, 'Old', 'lineage-old');
            INSERT INTO document (id, project_id, name, content) VALUES (1, 1, 'doc', 'hello');
            INSERT INTO code (id, project_id, name) VALUES (1, 1, 'Theme');
            INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text)
            VALUES (1, 1, 0, 5, 'hello');
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    await db_module.init_db()
    migrated = describe_schema(old / "aqda.db")
    assert migrated == describe_schema(fresh / "aqda.db")
    assert ("idx_coding_run_id", 0, "c", 0, ["run_id"]) in migrated["coding"]["indexes"]
    assert ("project", "id", "CASCADE") in [
        (key[0], key[2], key[4]) for key in migrated["coding_run"]["foreign_keys"]
    ]
    version = await rows("SELECT value FROM setting WHERE key='schema_version'")
    assert int(version[0]["value"]) == 13 == db_module.LATEST_SCHEMA_VERSION
    listed = await codings.list_codings(project_id=1)
    assert listed[0]["run_id"] is None
    assert listed[0]["run_label"] is None
    assert listed[0]["accepted_from_run_id"] is None
    assert listed[0]["fields"] == {}


@pytest.mark.asyncio
async def test_runs_and_fields_survive_export_and_import_as_a_new_project(
    tmp_path, use_data_dir
):
    use_data_dir(tmp_path / "source")
    await db_module.init_db()
    source = await build_run_project()
    expected = await run_content(source["id"])
    assert expected["runs"][0][6] == "Proposed by Run 1"
    assert expected["codings"] == [("Alice", "", None), ("Bob", "Run 1", source["run_uuid"])]
    assert expected["answers"] == [
        ("Alice", "Actor", "Alice"), ("Alice", "Agrees", "1"), ("Bob", "Actor", "Bob"),
    ]
    package, _, _ = await build_aqda_snapshot(source["id"])
    exported = sqlite3.connect(":memory:")
    try:
        exported.deserialize(package)
        marker = exported.execute(
            "SELECT value FROM setting WHERE key='snapshot_format'"
        ).fetchone()
        assert marker == (str(db_module.SNAPSHOT_FORMAT),)
    finally:
        exported.close()

    use_data_dir(tmp_path / "destination")
    await db_module.init_db()
    # Occupy the low IDs so that remapping, not coincidence, keeps the references right.
    await build_run_project(name="Unrelated")
    imported = await projects.import_from_db(upload(package), mode="auto")
    new_id = imported["imported"][0]["id"]
    assert await run_content(new_id) == expected
    listed = {row["selected_text"]: row for row in await codings.list_codings(project_id=new_id)}
    assert listed["Bob"]["run_label"] == "Run 1"
    assert listed["Bob"]["run_model"] == "llama"
    assert listed["Bob"]["fields"] == {"Actor": "Bob"}
    assert listed["Alice"]["fields"] == {"Actor": "Alice", "Agrees": "1"}

    # A second copy in the same database keeps the run's uuid within its own project.
    copied = await projects.import_from_db(
        upload(package), mode="copy", target_lineage_id=imported["imported"][0]["lineage_id"]
    )
    assert await run_content(copied["imported"][0]["id"]) == expected


# An accepted coding still names the run it came from after export and import, and after a
# copy within the same database (both snapshot paths remap the run's ID).
@pytest.mark.asyncio
async def test_accepted_codings_keep_their_run_through_snapshots(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "source")
    await db_module.init_db()
    source = await build_run_project()
    model_coding = (await rows(
        "SELECT cg.id FROM coding cg JOIN document d ON d.id=cg.document_id "
        "WHERE d.project_id=? AND cg.run_id IS NOT NULL", (source["id"],)
    ))[0]["id"]
    await codings.accept_coding(model_coding)
    package, _, _ = await build_aqda_snapshot(source["id"])

    use_data_dir(tmp_path / "destination")
    await db_module.init_db()
    # Occupy the low IDs so that remapping, not coincidence, keeps the reference right.
    await build_run_project(name="Unrelated")
    first = await projects.import_from_db(upload(package), mode="auto")
    second = await projects.import_from_db(
        upload(package), mode="copy", target_lineage_id=first["imported"][0]["lineage_id"]
    )
    for imported in (first, second):
        new_id = imported["imported"][0]["id"]
        listed = {
            row["selected_text"]: row for row in await codings.list_codings(project_id=new_id)
        }
        assert listed["Bob"]["run_id"] is None
        assert listed["Bob"]["accepted_from_run_label"] == "Run 1"
        own_run = await rows(
            "SELECT id FROM coding_run WHERE project_id=? AND label='Run 1'", (new_id,)
        )
        assert listed["Bob"]["accepted_from_run_id"] == own_run[0]["id"]


@pytest.mark.asyncio
async def test_repeated_collaborator_updates_do_not_accumulate_runs_or_fields(
    tmp_path, use_data_dir
):
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    source = await build_run_project()
    alice_id = source["id"]
    first, _, _ = await build_aqda_snapshot(alice_id)

    bob = use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    bob_id = (await projects.import_package_bytes(first))["imported"][0]["id"]
    bob_hand = [
        row for row in await codings.list_codings(project_id=bob_id)
        if row["run_id"] is None
    ][0]
    await codings.replace_coding_fields(
        bob_hand["id"], codings.CodingFieldsUpdate(fields={"Actor": "Alice M."})
    )
    second, _, _ = await build_aqda_snapshot(bob_id)

    use_data_dir(alice)
    result = await projects.import_package_bytes(second)
    assert result["imported"][0]["action"] == "update"
    after_first = await run_content(alice_id)
    assert len(after_first["runs"]) == 1 and len(after_first["fields"]) == 2
    assert ("Alice", "Actor", "Alice M.") in after_first["answers"]
    assert ("Alice", "Agrees", "1") not in after_first["answers"]

    use_data_dir(bob)
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=bob_id, name="Count", kind="number")
    )
    third, _, _ = await build_aqda_snapshot(bob_id)

    use_data_dir(alice)
    result = await projects.import_package_bytes(third)
    assert result["imported"][0]["action"] == "update"
    after_second = await run_content(alice_id)
    assert after_second["runs"] == after_first["runs"]
    assert after_second["run_documents"] == after_first["run_documents"]
    assert after_second["codings"] == after_first["codings"]
    assert [field[0] for field in after_second["fields"]] == ["Actor", "Agrees", "Count"]
    # Nothing is left behind from the replaced versions anywhere in the database.
    counts = (await rows(
        "SELECT (SELECT COUNT(*) FROM coding_run) AS runs, "
        "(SELECT COUNT(*) FROM coding_run_document) AS run_documents, "
        "(SELECT COUNT(*) FROM coding_field) AS fields, "
        "(SELECT COUNT(*) FROM coding_field_value) AS answers"
    ))[0]
    assert counts == {"runs": 1, "run_documents": 1, "fields": 3, "answers": 2}
    model = [
        row for row in await codings.list_codings(project_id=alice_id)
        if row["run_id"] is not None
    ][0]
    run = (await rows("SELECT * FROM coding_run WHERE id=?", (model["run_id"],)))[0]
    assert run["project_id"] == alice_id and run["uuid"] == source["run_uuid"]
    assert model["fields"] == {"Actor": "Bob"}


@pytest.mark.asyncio
async def test_refreshing_a_conflict_copy_replaces_its_runs_and_fields(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "mirror")
    await db_module.init_db()
    source = await build_run_project()
    package, project, _ = await build_aqda_snapshot(source["id"])
    copy = await projects.import_package_bytes(
        package, mode="copy", target_lineage_id=project["lineage_id"]
    )
    copy_id = copy["imported"][0]["id"]
    expected = await run_content(copy_id)
    for _ in range(2):
        await projects.refresh_conflict_copy(package, copy_id, project["lineage_id"])
        assert await run_content(copy_id) == expected
    assert (await rows("SELECT COUNT(*) AS n FROM coding_run"))[0]["n"] == 2


@pytest.mark.asyncio
async def test_a_newer_format_is_refused_on_import(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "newer")
    await db_module.init_db()
    source = await build_run_project()
    package, project, _ = await build_aqda_snapshot(source["id"])
    newer = edit_package(package, *NEWER_FORMAT)

    use_data_dir(tmp_path / "older-reader")
    await db_module.init_db()
    with pytest.raises(HTTPException) as refused:
        await projects.import_from_db(upload(newer), mode="auto")
    assert refused.value.status_code == 409
    assert "newer version of AQDA" in refused.value.detail
    assert await rows("SELECT id FROM project") == []
    # The unchanged file still imports.
    assert (await projects.import_from_db(upload(package), mode="auto"))["count"] == 1


@pytest.mark.asyncio
async def test_an_older_format_does_not_replace_runs_or_fields(tmp_path, use_data_dir):
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    source = await build_run_project()
    plain = await projects.create_project(projects.ProjectCreate(name="Plain"))
    with_runs, _, _ = await build_aqda_snapshot(source["id"])
    without_runs, _, _ = await build_aqda_snapshot(plain["id"])

    use_data_dir(tmp_path / "old-aqda")
    await db_module.init_db()
    bob_runs = (await projects.import_package_bytes(with_runs))["imported"][0]["id"]
    bob_plain = (await projects.import_package_bytes(without_runs))["imported"][0]["id"]
    for project_id in (bob_runs, bob_plain):
        await projects.update_project(
            project_id, projects.ProjectUpdate(description="Saved by an older AQDA")
        )
    older_runs = edit_package((await build_aqda_snapshot(bob_runs))[0], *OLDER_FORMAT)
    older_plain = edit_package((await build_aqda_snapshot(bob_plain))[0], *OLDER_FORMAT)

    use_data_dir(alice)
    before = await run_content(source["id"])
    revision = (await projects.get_project(source["id"]))["revision"]
    for mode in ("auto", "replace"):
        with pytest.raises(HTTPException) as refused:
            await projects.import_package_bytes(
                older_runs, mode=mode,
                target_lineage_id=(await projects.get_project(source["id"]))["lineage_id"],
            )
        assert refused.value.status_code == 409
        assert "update AQDA" in refused.value.detail
    assert await run_content(source["id"]) == before
    assert (await projects.get_project(source["id"]))["revision"] == revision
    assert (await projects.get_project(source["id"]))["description"] == ""
    assert not list((alice / "backups").glob("aqda-before-shared-update-*.db"))

    # A project without runs or fields still accepts the older format.
    updated = await projects.import_package_bytes(older_plain)
    assert updated["imported"][0]["action"] == "update"
    assert (await projects.get_project(plain["id"]))["description"] == "Saved by an older AQDA"


async def writer_file(folder: Path) -> Path:
    device = (await rows("SELECT value FROM setting WHERE key='device_id'"))[0]["value"]
    return shared_service._writer_snapshot_path(folder, device)


async def share_between_alice_and_bob(tmp_path, use_data_dir):
    """Alice shares a run project; Bob opens it, edits it, and publishes his version."""
    shared_root = tmp_path / "Drive"
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    source = await build_run_project()
    folder = Path((await share_project(source["id"]))["folder"])

    bob = use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    bob_id = (await open_shared_project(str(folder)))["project_id"]
    await projects.update_project(bob_id, projects.ProjectUpdate(description="Bob's edit"))
    await sync_project(bob_id)
    return alice, bob, folder, source, await writer_file(folder)


@pytest.mark.asyncio
async def test_shared_sync_reports_formats_it_cannot_apply_and_keeps_going(
    tmp_path, use_data_dir
):
    alice, _, folder, source, bob_file = await share_between_alice_and_bob(
        tmp_path, use_data_dir
    )
    bob_package = bob_file.read_bytes()

    # A collaborator with an older AQDA: the update would drop the run and the fields.
    bob_file.write_bytes(edit_package(bob_package, *OLDER_FORMAT))
    use_data_dir(alice)
    before = await run_content(source["id"])
    for _ in range(2):
        await sync_project(source["id"])
        project = await projects.get_project(source["id"])
        assert "older AQDA" in project["shared_sync_error"]
        assert project["description"] == ""
    assert await run_content(source["id"]) == before

    # A collaborator with a newer AQDA: refused, while local work is still published.
    bob_file.write_bytes(edit_package(bob_package, *NEWER_FORMAT))
    await projects.update_project(source["id"], projects.ProjectUpdate(description="Alice"))
    for _ in range(2):
        await sync_project(source["id"])
        project = await projects.get_project(source["id"])
        assert project["shared_sync_error"] == projects.NEWER_FORMAT_MESSAGE
    assert project["shared_last_published_revision"] == project["revision"]
    assert project["description"] == "Alice"
    assert await run_content(source["id"]) == before


@pytest.mark.asyncio
async def test_shared_sync_warns_when_an_older_aqda_wrote_to_the_folder(
    tmp_path, use_data_dir
):
    shared_root = tmp_path / "Drive"
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    plain = await projects.create_project(projects.ProjectCreate(name="Plain"))
    folder = Path((await share_project(plain["id"]))["folder"])

    use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    bob_id = (await open_shared_project(str(folder)))["project_id"]
    await projects.update_project(bob_id, projects.ProjectUpdate(description="Bob"))
    await sync_project(bob_id)
    bob_file = await writer_file(folder)
    bob_file.write_bytes(edit_package(bob_file.read_bytes(), *OLDER_FORMAT))

    # Without runs or fields, the older version applies and nothing needs saying.
    use_data_dir(alice)
    await sync_project(plain["id"])
    project = await projects.get_project(plain["id"])
    assert project["description"] == "Bob"
    assert project["shared_sync_error"] is None

    # Once the project has a coding field, the older writer is worth a warning.
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=plain["id"], name="Actor")
    )
    await sync_project(plain["id"])
    project = await projects.get_project(plain["id"])
    assert "older AQDA" in project["shared_sync_error"]
    assert project["shared_last_published_revision"] == project["revision"]
