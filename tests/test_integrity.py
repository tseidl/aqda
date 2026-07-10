import json

import httpx
import pytest
from fastapi import HTTPException

import aqda.db as db_module
from aqda.app import app
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
