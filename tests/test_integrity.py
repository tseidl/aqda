import json

import httpx
import pytest
from fastapi import HTTPException

import aqda.db as db_module
from aqda.app import _split_host_header, app
from aqda.routers import codes, codings, export, projects
from aqda.services.offsets import repair_legacy_offsets


async def response_bytes(response) -> bytes:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


@pytest.mark.asyncio
async def test_offsets_repair_utf16_and_shifted_unique_text(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "offsets")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Offsets"))
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', ?)",
            (project["id"], "A😀hello and unique target"),
        )
        code = await db.execute(
            "INSERT INTO code (project_id, name) VALUES (?, 'Theme')", (project["id"],)
        )
        await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "offset_unit) VALUES (?, ?, 3, 8, 'hello', 'legacy_utf16')",
            (doc.lastrowid, code.lastrowid),
        )
        await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "offset_unit) VALUES (?, ?, 23, 29, 'target', 'legacy_utf16')",
            (doc.lastrowid, code.lastrowid),
        )
        await db.commit()
        result = await repair_legacy_offsets(db, [project["id"]])
        await db.commit()
        assert result == {"converted": 1, "reanchored": 1, "flagged": 0}
        rows = await (
            await db.execute(
                "SELECT start_pos, end_pos, repair_status FROM coding ORDER BY id"
            )
        ).fetchall()
        assert tuple(rows[0]) == (2, 7, "converted_utf16")
        assert tuple(rows[1]) == (19, 25, "reanchored_unique")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_coding_validation_rejects_mismatch_and_cross_project(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "validation")
    await db_module.init_db()
    first = await projects.create_project(projects.ProjectCreate(name="One"))
    second = await projects.create_project(projects.ProjectCreate(name="Two"))
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', 'hello')",
            (first["id"],),
        )
        wrong_code = await db.execute(
            "INSERT INTO code (project_id, name) VALUES (?, 'Wrong')", (second["id"],)
        )
        right_code = await db.execute(
            "INSERT INTO code (project_id, name) VALUES (?, 'Right')", (first["id"],)
        )
        await db.commit()
    finally:
        await db.close()

    with pytest.raises(HTTPException, match="same project"):
        await codings.create_coding(
            codings.CodingCreate(
                document_id=doc.lastrowid,
                code_id=wrong_code.lastrowid,
                start_pos=0,
                end_pos=5,
                selected_text="hello",
            )
        )
    with pytest.raises(HTTPException, match="does not match"):
        await codings.create_coding(
            codings.CodingCreate(
                document_id=doc.lastrowid,
                code_id=right_code.lastrowid,
                start_pos=0,
                end_pos=5,
                selected_text="wrong",
            )
        )


@pytest.mark.asyncio
async def test_subtree_restore_does_not_resurrect_previously_deleted_coding(
    tmp_path, use_data_dir
):
    use_data_dir(tmp_path / "trash")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Trash"))
    parent = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Parent"))
    child = await codes.create_code(
        codes.CodeCreate(project_id=project["id"], parent_id=parent["id"], name="Child")
    )
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', 'abcdefghij')",
            (project["id"],),
        )
        active = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text) "
            "VALUES (?, ?, 0, 2, 'ab')",
            (doc.lastrowid, child["id"]),
        )
        old = await db.execute(
            "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, "
            "deleted_at) VALUES (?, ?, 2, 4, 'cd', '2020-01-01 00:00:00')",
            (doc.lastrowid, child["id"]),
        )
        await db.commit()
    finally:
        await db.close()

    impact = await codes.code_delete_impact(parent["id"])
    assert impact == {
        "name": "Parent",
        "code_count": 2,
        "child_count": 1,
        "coding_count": 1,
    }
    await codes.delete_code(parent["id"])
    await codes.restore_code(parent["id"])
    db = await db_module.get_db()
    try:
        active_row = await (
            await db.execute("SELECT deleted_at FROM coding WHERE id=?", (active.lastrowid,))
        ).fetchone()
        old_row = await (
            await db.execute("SELECT deleted_at FROM coding WHERE id=?", (old.lastrowid,))
        ).fetchone()
        assert active_row["deleted_at"] is None
        assert old_row["deleted_at"] == "2020-01-01 00:00:00"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_export_fidelity_and_api_404(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "exports")
    await db_module.init_db()
    project = await projects.create_project(
        projects.ProjectCreate(name="日本語 Projekt", description="A & B")
    )
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', 'text')",
            (project["id"],),
        )
        await db.execute(
            "INSERT INTO document_variable (document_id, key, value) VALUES (?, 'group', 'A')",
            (doc.lastrowid,),
        )
        await db.commit()
    finally:
        await db.close()

    qdpx = await export.export_qdpx(project["id"])
    assert "filename*=UTF-8''" in qdpx.headers["content-disposition"]
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(await response_bytes(qdpx))) as archive:
        xml = archive.read("project.qde").decode()
        assert "A &amp; B" in xml
        assert "A &amp;amp; B" not in xml

    exported_json = json.loads(await response_bytes(await export.export_json(project["id"])))
    assert exported_json["documents"][0]["variables"] == {"group": "A"}
    assert "exclude source content" in exported_json["format"]

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        response = await client.get("/api/definitely-not-a-route")
        dns_rebinding = await client.get(
            "/api/projects", headers={"Host": "attacker.example"}
        )
        cross_site = await client.post(
            "/api/shared/sync", headers={"Origin": "https://attacker.example"}
        )
        local_site = await client.post(
            "/api/definitely-not-a-route",
            headers={"Origin": "http://127.0.0.1:8765"},
        )
    assert response.status_code == 404
    assert response.json()["detail"].startswith("API route not found")
    assert dns_rebinding.status_code == 400
    assert dns_rebinding.json()["detail"] == (
        "AQDA only accepts requests addressed to localhost"
    )
    assert cross_site.status_code == 403
    assert cross_site.json()["detail"] == "Cross-site requests to AQDA are not allowed"
    assert local_site.status_code == 404


@pytest.mark.asyncio
async def test_same_origin_writes_follow_the_chosen_port():
    # Browsers attach Origin to every same-origin POST, so `aqda --port 9000`
    # must accept its own origin while still rejecting other sites and ports.
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:9000"
    ) as client:
        same_origin = await client.post(
            "/api/definitely-not-a-route", headers={"Origin": "http://127.0.0.1:9000"}
        )
        other_port = await client.post(
            "/api/definitely-not-a-route", headers={"Origin": "http://127.0.0.1:8765"}
        )
        dev_server = await client.post(
            "/api/definitely-not-a-route", headers={"Origin": "http://localhost:5173"}
        )
        https_origin = await client.post(
            "/api/definitely-not-a-route", headers={"Origin": "https://localhost:9000"}
        )
    assert same_origin.status_code == 404
    assert other_port.status_code == 403
    assert dev_server.status_code == 404
    assert https_origin.status_code == 403


@pytest.mark.asyncio
async def test_export_keeps_restored_code_whose_parent_is_trashed(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "orphan")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Orphans"))
    parent = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Parent"))
    child = await codes.create_code(
        codes.CodeCreate(project_id=project["id"], parent_id=parent["id"], name="Child")
    )
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', 'hello world')",
            (project["id"],),
        )
        await db.commit()
    finally:
        await db.close()
    await codings.create_coding(
        codings.CodingCreate(
            document_id=doc.lastrowid,
            code_id=child["id"],
            start_pos=0,
            end_pos=5,
            selected_text="hello",
        )
    )
    # Two separate deletions, then only the child is restored: it now hangs
    # under a parent that is still in the trash.
    await codes.delete_code(child["id"])
    await codes.delete_code(parent["id"])
    await codes.restore_code(child["id"])

    codebook = (await response_bytes(await export.export_codebook(project["id"]))).decode()
    assert 'name="Child"' in codebook
    assert 'name="Parent"' not in codebook

    import io
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(io.BytesIO(await response_bytes(await export.export_qdpx(project["id"])))) as archive:
        root = ElementTree.fromstring(archive.read("project.qde").decode())
    ns = "{urn:QDA-XML:project:1.0}"
    code_guids = {el.get("guid") for el in root.iter(f"{ns}Code")}
    refs = [el.get("targetGUID") for el in root.iter(f"{ns}CodeRef")]
    assert refs and all(ref in code_guids for ref in refs)


@pytest.mark.asyncio
async def test_cross_site_fetch_metadata_and_malformed_ports_are_rejected():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8765"
    ) as client:
        # An <img> or <iframe> on a hostile page carries no Origin but does carry
        # Sec-Fetch-Site, which must block even read-only routes such as exports.
        image_load = await client.get(
            "/api/projects", headers={"Sec-Fetch-Site": "cross-site"}
        )
        same_origin = await client.get(
            "/api/definitely-not-a-route", headers={"Sec-Fetch-Site": "same-origin"}
        )
        out_of_range = await client.get(
            "/api/definitely-not-a-route", headers={"Host": "localhost:70000"}
        )
    assert image_load.status_code == 403
    assert same_origin.status_code == 404
    assert out_of_range.status_code == 400
    # str.isdigit accepts superscript digits that int() rejects; the parser must not 500.
    assert _split_host_header("localhost:²") is None
    assert _split_host_header("[::1]:8765") == ("::1", 8765)
    assert _split_host_header("[localhost]:8765") is None


@pytest.mark.asyncio
async def test_export_promotes_codes_trapped_in_a_parent_cycle(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "cycle")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Cycle"))
    first = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="First"))
    second = await codes.create_code(
        codes.CodeCreate(project_id=project["id"], parent_id=first["id"], name="Second")
    )
    db = await db_module.get_db()
    try:
        # The API refuses cycles, but imported databases are not checked.
        await db.execute("UPDATE code SET parent_id=? WHERE id=?", (second["id"], first["id"]))
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', 'hello world')",
            (project["id"],),
        )
        await db.commit()
    finally:
        await db.close()
    await codings.create_coding(
        codings.CodingCreate(
            document_id=doc.lastrowid, code_id=second["id"],
            start_pos=0, end_pos=5, selected_text="hello",
        )
    )

    codebook = (await response_bytes(await export.export_codebook(project["id"]))).decode()
    assert 'name="First"' in codebook and 'name="Second"' in codebook

    import io
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(io.BytesIO(await response_bytes(await export.export_qdpx(project["id"])))) as archive:
        root = ElementTree.fromstring(archive.read("project.qde").decode())
    ns = "{urn:QDA-XML:project:1.0}"
    code_guids = {el.get("guid") for el in root.iter(f"{ns}Code")}
    assert len(code_guids) == 2
    refs = [el.get("targetGUID") for el in root.iter(f"{ns}CodeRef")]
    assert refs and all(ref in code_guids for ref in refs)


@pytest.mark.asyncio
async def test_same_code_on_the_same_span_is_refused_once(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "duplicates")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Dup"))
    theme = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Theme"))
    other = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Other"))
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', 'hello world')",
            (project["id"],),
        )
        await db.commit()
    finally:
        await db.close()
    span = dict(document_id=doc.lastrowid, start_pos=0, end_pos=5, selected_text="hello")
    await codings.create_coding(codings.CodingCreate(code_id=theme["id"], **span))
    with pytest.raises(HTTPException) as refused:
        await codings.create_coding(codings.CodingCreate(code_id=theme["id"], **span))
    assert refused.value.status_code == 409
    # A different code on the same passage is ordinary qualitative coding.
    second = await codings.create_coding(codings.CodingCreate(code_id=other["id"], **span))
    assert second["code_id"] == other["id"]


@pytest.mark.asyncio
async def test_shared_update_backups_are_pruned_and_schema_version_is_derived(
    tmp_path, use_data_dir
):
    data_dir = use_data_dir(tmp_path / "prune")
    await db_module.init_db()
    for _ in range(4):
        db_module.create_shared_update_backup(keep=2)
    kept = list((data_dir / "backups").glob("aqda-before-shared-update-*.db"))
    assert len(kept) == 2

    assert f"('schema_version', '{db_module.LATEST_SCHEMA_VERSION}')" in db_module.SCHEMA
    db = await db_module.get_db()
    try:
        row = await (
            await db.execute("SELECT value FROM setting WHERE key='schema_version'")
        ).fetchone()
        assert int(row["value"]) == db_module.LATEST_SCHEMA_VERSION
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_qdpx_exports_variables_and_links_memos(tmp_path, use_data_dir):
    from aqda.routers import documents, memos

    use_data_dir(tmp_path / "refi")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="REFI", description="d"))
    db = await db_module.get_db()
    try:
        doc = await db.execute(
            "INSERT INTO document (project_id, name, content) VALUES (?, 'int_01', 'hello world')",
            (project["id"],),
        )
        await db.commit()
    finally:
        await db.close()
    await documents.set_variables(
        doc.lastrowid,
        [documents.VariableUpdate(key="gender", value="F"),
         documents.VariableUpdate(key="age", value="35")],
    )
    parent = await codes.create_code(codes.CodeCreate(project_id=project["id"], name="Parent"))
    coding = await codings.create_coding(
        codings.CodingCreate(
            document_id=doc.lastrowid, code_id=parent["id"],
            start_pos=0, end_pos=5, selected_text="hello",
        )
    )
    pid = project["id"]
    await memos.create_memo(memos.MemoCreate(project_id=pid, code_id=parent["id"], title="Code memo"))
    await memos.create_memo(memos.MemoCreate(
        project_id=pid, document_id=doc.lastrowid, coding_id=coding["id"], title="Coding memo"
    ))
    await memos.create_memo(memos.MemoCreate(
        project_id=pid, document_id=doc.lastrowid, start_pos=6, end_pos=11, title="Passage memo"
    ))
    await memos.create_memo(memos.MemoCreate(
        project_id=pid, document_id=doc.lastrowid, title="Document memo"
    ))
    await memos.create_memo(memos.MemoCreate(project_id=pid, title="Project memo"))

    import io
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(io.BytesIO(await response_bytes(await export.export_qdpx(pid)))) as archive:
        root = ElementTree.fromstring(archive.read("project.qde").decode())
    ns = "{urn:QDA-XML:project:1.0}"

    # ProjectType order: Variables before Sources before Notes; Description before NoteRef.
    order = [child.tag.replace(ns, "") for child in root]
    assert [tag for tag in order if tag in {"Variables", "Sources", "Notes", "Description", "NoteRef"}] == [
        "Variables", "Sources", "Notes", "Description", "NoteRef",
    ]

    variable_names = {
        el.get("guid"): el.get("name") for el in root.iter(f"{ns}Variable")
    }
    source = root.find(f"{ns}Sources/{ns}TextSource")
    values = {
        variable_names[vv.find(f"{ns}VariableRef").get("targetGUID")]: vv.find(f"{ns}TextValue").text
        for vv in source.iter(f"{ns}VariableValue")
    }
    assert values == {"gender": "F", "age": "35"}

    note_guids = {el.get("guid") for el in root.iter(f"{ns}Note")}
    assert len(note_guids) == 5
    refs = [el.get("targetGUID") for el in root.iter(f"{ns}NoteRef")]
    assert refs and all(ref in note_guids for ref in refs)
    assert root.find(f"{ns}CodeBook/{ns}Codes/{ns}Code/{ns}NoteRef") is not None
    coded = source.find(f"{ns}PlainTextSelection[@startPosition='0']")
    assert coded.find(f"{ns}Coding") is not None and coded.find(f"{ns}NoteRef") is not None
    # The passage memo keeps its own span: a selection without a Coding.
    passage = source.find(f"{ns}PlainTextSelection[@startPosition='6']")
    assert passage.get("endPosition") == "11"
    assert passage.find(f"{ns}Coding") is None and passage.find(f"{ns}NoteRef") is not None
    assert source.find(f"{ns}NoteRef") is not None
    assert root.find(f"{ns}NoteRef") is not None


@pytest.mark.asyncio
async def test_empty_project_exports_omit_invalid_empty_containers(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "empty")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Empty"))

    import io
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(io.BytesIO(await response_bytes(await export.export_qdpx(project["id"])))) as archive:
        root = ElementTree.fromstring(archive.read("project.qde").decode())
    ns = "{urn:QDA-XML:project:1.0}"
    assert root.find(f"{ns}CodeBook") is None and root.find(f"{ns}Sources") is None
    assert root.find(f"{ns}Users/{ns}User") is not None

    with pytest.raises(HTTPException) as refused:
        await export.export_codebook(project["id"])
    assert refused.value.status_code == 400
