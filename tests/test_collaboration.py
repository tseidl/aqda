import io
import sqlite3

import pytest
from starlette.datastructures import UploadFile

import aqda.db as db_module
from aqda.routers import codes, codings, documents, export, memos, projects


async def response_bytes(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


def upload(data: bytes, name: str = "shared.aqda") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


async def project_row(project_id: int):
    db = await db_module.get_db()
    try:
        return await (await db.execute("SELECT * FROM project WHERE id=?", (project_id,))).fetchone()
    finally:
        await db.close()


async def project_count() -> int:
    db = await db_module.get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) AS n FROM project")).fetchone())["n"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_shared_snapshot_fast_forwards_clean_project_and_creates_backup(
    tmp_path, use_data_dir
):
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    created = await projects.create_project(projects.ProjectCreate(name="Study"))
    alice_id = created["id"]

    first_snapshot = await response_bytes(await export.export_aqda(alice_id))
    alice_after_export = await project_row(alice_id)
    first_snapshot_id = alice_after_export["head_snapshot_id"]
    lineage_id = alice_after_export["lineage_id"]

    use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    first_import = await projects.import_from_db(upload(first_snapshot), mode="auto")
    assert first_import["imported"][0]["action"] == "create"
    bob_id = first_import["imported"][0]["id"]
    bob_project = await project_row(bob_id)
    assert bob_project["lineage_id"] == lineage_id
    assert bob_project["head_snapshot_id"] == first_snapshot_id

    await projects.update_project(
        bob_id,
        projects.ProjectUpdate(description="Bob added analysis"),
    )
    second_snapshot = await response_bytes(await export.export_aqda(bob_id))

    use_data_dir(alice)
    update_result = await projects.import_from_db(upload(second_snapshot), mode="auto")
    assert update_result["conflicts"] == []
    assert update_result["imported"] == [
        {
            "id": alice_id,
            "name": "Study",
            "action": "update",
            "lineage_id": lineage_id,
        }
    ]
    assert update_result["backup_path"]
    assert list((alice / "backups").glob("aqda-before-shared-update-*.db"))
    backup = sqlite3.connect(update_result["backup_path"])
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT description FROM project WHERE id=?", (alice_id,)).fetchone()[0] == ""
    finally:
        backup.close()

    updated = await project_row(alice_id)
    assert updated["description"] == "Bob added analysis"
    assert await project_count() == 1

    duplicate = await projects.import_from_db(upload(second_snapshot), mode="auto")
    assert duplicate["count"] == 0
    assert duplicate["unchanged"][0]["reason"] == "unchanged"


@pytest.mark.asyncio
async def test_divergent_snapshot_never_overwrites_and_can_be_kept_as_copy(
    tmp_path, use_data_dir
):
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    created = await projects.create_project(projects.ProjectCreate(name="Interviews"))
    alice_id = created["id"]
    shared_base = await response_bytes(await export.export_aqda(alice_id))
    lineage_id = (await project_row(alice_id))["lineage_id"]

    use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    imported = await projects.import_from_db(upload(shared_base), mode="auto")
    bob_id = imported["imported"][0]["id"]
    await projects.update_project(bob_id, projects.ProjectUpdate(description="Bob branch"))
    bob_snapshot = await response_bytes(await export.export_aqda(bob_id))

    use_data_dir(alice)
    await projects.update_project(alice_id, projects.ProjectUpdate(description="Alice branch"))
    conflict = await projects.import_from_db(upload(bob_snapshot), mode="auto")
    assert conflict["count"] == 0
    assert conflict["conflicts"][0]["lineage_id"] == lineage_id
    assert (await project_row(alice_id))["description"] == "Alice branch"
    assert await project_count() == 1

    kept = await projects.import_from_db(
        upload(bob_snapshot),
        mode="copy",
        target_lineage_id=lineage_id,
    )
    assert kept["imported"][0]["action"] == "copy"
    assert "conflicting copy" in kept["imported"][0]["name"]
    assert kept["imported"][0]["lineage_id"] != lineage_id
    assert await project_count() == 2

    replaced = await projects.import_from_db(
        upload(bob_snapshot),
        mode="replace",
        target_lineage_id=lineage_id,
    )
    assert replaced["imported"][0]["action"] == "update"
    assert replaced["backup_path"]
    assert (await project_row(alice_id))["description"] == "Bob branch"


@pytest.mark.asyncio
async def test_exporting_without_edits_does_not_create_a_false_conflict(tmp_path, use_data_dir):
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    created = await projects.create_project(projects.ProjectCreate(name="Field notes"))
    alice_id = created["id"]
    shared_base = await response_bytes(await export.export_aqda(alice_id))

    use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    imported = await projects.import_from_db(upload(shared_base), mode="auto")
    bob_id = imported["imported"][0]["id"]

    # Alice downloading another identical snapshot is metadata activity, not an edit.
    use_data_dir(alice)
    await response_bytes(await export.export_aqda(alice_id))

    use_data_dir(tmp_path / "bob")
    await projects.update_project(bob_id, projects.ProjectUpdate(description="Bob's notes"))
    bob_snapshot = await response_bytes(await export.export_aqda(bob_id))

    use_data_dir(alice)
    result = await projects.import_from_db(upload(bob_snapshot), mode="auto")
    assert result["conflicts"] == []
    assert result["imported"][0]["action"] == "update"
    assert (await project_row(alice_id))["description"] == "Bob's notes"


@pytest.mark.asyncio
async def test_snapshot_round_trip_preserves_relationships_and_variables(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "source")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Linked memo"))
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc.txt', 'hello')",
            (project["id"],),
        )
        await db.execute(
            "INSERT INTO document_variable (document_id, key, value) "
            "VALUES (?, 'participant', 'P01')",
            (doc.lastrowid,),
        )
        parent = await db.execute(
            "INSERT INTO code (project_id, name) VALUES (?, 'Language')", (project["id"],)
        )
        code = await db.execute(
            "INSERT INTO code (project_id, parent_id, name) VALUES (?, ?, 'Greeting')",
            (project["id"], parent.lastrowid),
        )
        coding = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text) "
            "VALUES (?, ?, 0, 5, 'hello')",
            (doc.lastrowid, code.lastrowid),
        )
        await db.execute(
            "INSERT INTO memo (project_id, document_id, code_id, coding_id, title) "
            "VALUES (?, ?, ?, ?, 'Linked')",
            (project["id"], doc.lastrowid, code.lastrowid, coding.lastrowid),
        )
        await db.commit()
    finally:
        await db.close()

    package = await response_bytes(await export.export_aqda(project["id"]))
    exported = sqlite3.connect(":memory:")
    try:
        exported.deserialize(package)
        coding_id = exported.execute("SELECT id FROM coding").fetchone()[0]
        memo_coding_id = exported.execute("SELECT coding_id FROM memo").fetchone()[0]
        assert memo_coding_id == coding_id
    finally:
        exported.close()

    use_data_dir(tmp_path / "destination")
    await db_module.init_db()
    imported = await projects.import_from_db(upload(package), mode="auto")
    imported_id = imported["imported"][0]["id"]
    db = await db_module.get_db()
    try:
        row = await (
            await db.execute(
                "SELECT m.coding_id AS memo_coding_id, cg.id AS coding_id, "
                "child.parent_id, parent.name AS parent_name, dv.value AS participant "
                "FROM memo m JOIN coding cg ON cg.id=m.coding_id "
                "JOIN code child ON child.id=cg.code_id "
                "JOIN code parent ON parent.id=child.parent_id "
                "JOIN document d ON d.id=cg.document_id "
                "JOIN document_variable dv ON dv.document_id=d.id AND dv.key='participant' "
                "WHERE m.project_id=?",
                (imported_id,),
            )
        ).fetchone()
        assert row["memo_coding_id"] == row["coding_id"]
        assert row["parent_id"] is not None
        assert row["parent_name"] == "Language"
        assert row["participant"] == "P01"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_migration_assigns_lineage_to_legacy_projects(tmp_path, use_data_dir):
    data_dir = use_data_dir(tmp_path / "legacy")
    legacy = sqlite3.connect(data_dir / "aqda.db")
    try:
        legacy.executescript(
            """
            CREATE TABLE project (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now')),
                modified_at TEXT DEFAULT (datetime('now')),
                deleted_at TEXT DEFAULT NULL
            );
            CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO project (id, name) VALUES (1, 'Legacy');
            INSERT INTO setting (key, value) VALUES ('schema_version', '7');
            """
        )
        legacy.commit()
    finally:
        legacy.close()

    await db_module.init_db()
    migrated = await project_row(1)
    assert migrated["lineage_id"]
    assert migrated["revision"] == 0
    assert list((data_dir / "backups").glob("aqda-before-migration-v9-*.db"))


@pytest.mark.asyncio
async def test_substantive_resource_mutations_advance_project_revision(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "revisions")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Revision tracking"))
    project_id = project["id"]

    doc = await documents.upload_document(
        project_id=project_id,
        file=upload(b"hello world", "doc.txt"),
    )
    code = await codes.create_code(codes.CodeCreate(project_id=project_id, name="Greeting"))
    coding = await codings.create_coding(
        codings.CodingCreate(
            document_id=doc["id"],
            code_id=code["id"],
            start_pos=0,
            end_pos=5,
            selected_text="hello",
        )
    )
    memo = await memos.create_memo(
        memos.MemoCreate(project_id=project_id, document_id=doc["id"], title="Note")
    )
    await documents.set_variables(
        doc["id"], [documents.VariableUpdate(key="source", value="interview")]
    )
    await memos.update_memo(memo["id"], memos.MemoUpdate(content="Analysis"))
    await codings.delete_coding(coding["id"])

    assert (await project_row(project_id))["revision"] == 7
