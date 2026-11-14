import io
import uuid

import httpx
import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import aqda.db as db_module
from aqda.app import app
from aqda.routers import codes, codings, memos, projects
from aqda.routers import coding_fields as fields_router
from aqda.routers.export import build_aqda_snapshot

TEXT = "Alice says hello. Bob says no."


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


async def revision(project_id: int) -> int:
    return (await rows("SELECT revision FROM project WHERE id=?", (project_id,)))[0]["revision"]


async def new_code(project_id: int, name: str, parent_id=None, description="") -> int:
    code = await codes.create_code(
        codes.CodeCreate(
            project_id=project_id, name=name, parent_id=parent_id, description=description
        )
    )
    return code["id"]


async def add_coding(
    doc_id, code_id, start, end, coder="Timo", run_id=None, answers=None
) -> int:
    """Insert a coding directly (other coders and runs cannot be made through the API here)."""
    coding_id = await execute(
        "INSERT INTO coding (document_id, code_id, start_pos, end_pos, selected_text, coder, "
        "run_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (doc_id, code_id, start, end, TEXT[start:end], coder, run_id),
    )
    for field_id, value in (answers or {}).items():
        await execute(
            "INSERT INTO coding_field_value (coding_id, field_id, value) VALUES (?, ?, ?)",
            (coding_id, field_id, value),
        )
    return coding_id


async def add_run(project_id: int, label: str, parent_code_id=None) -> int:
    return await execute(
        "INSERT INTO coding_run (project_id, uuid, label, model, proposed_parent_code_id, "
        "status) VALUES (?, ?, ?, 'llama', ?, 'finished')",
        (project_id, str(uuid.uuid4()), label, parent_code_id),
    )


async def setup_project(name="Merge study") -> dict:
    project = await projects.create_project(projects.ProjectCreate(name=name))
    project_id = project["id"]
    doc_id = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'doc', ?)",
        (project_id, TEXT),
    )
    actor = await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=project_id, name="Actor", kind="text")
    )
    return {"id": project_id, "doc_id": doc_id, "actor": actor["id"]}


def backups(data_dir) -> list[str]:
    folder = data_dir / "backups"
    return sorted(path.name for path in folder.glob("aqda-before-merge-*.db"))


@pytest.mark.asyncio
async def test_merge_moves_codings_memos_and_children_including_trashed(
    tmp_path, use_data_dir
):
    data_dir = use_data_dir(tmp_path / "merge")
    await db_module.init_db()
    p = await setup_project()
    pid, doc = p["id"], p["doc_id"]
    target = await new_code(pid, "Target")
    existing_child = await new_code(pid, "Existing child", parent_id=target)
    source = await new_code(pid, "Source", description="Talk about greetings.")
    child_b = await new_code(pid, "Child B", parent_id=source)
    child_a = await new_code(pid, "Child A", parent_id=source)
    trashed_child = await new_code(pid, "Trashed child", parent_id=source)
    await codes.update_code(child_b, codes.CodeUpdate(sort_order=2))
    await codes.update_code(child_a, codes.CodeUpdate(sort_order=1))

    active = await add_coding(doc, source, 0, 5, answers={p["actor"]: "Alice"})
    trashed = await add_coding(doc, source, 18, 21)
    await codings.delete_coding(trashed)
    trashed_before = (await rows("SELECT * FROM coding WHERE id=?", (trashed,)))[0]
    under_child = await add_coding(doc, trashed_child, 6, 10)
    await codes.delete_code(trashed_child)
    code_memo = await memos.create_memo(
        memos.MemoCreate(project_id=pid, code_id=source, title="Code memo")
    )
    passage_memo = await memos.create_memo(
        memos.MemoCreate(
            project_id=pid, document_id=doc, coding_id=active, start_pos=0, end_pos=5,
            title="Passage memo",
        )
    )
    run_id = await add_run(pid, "Run 1", parent_code_id=source)
    before = await revision(pid)

    impact = await codes.code_merge_impact(source, target_id=target)
    result = await codes.merge_code(source, codes.CodeMerge(target_id=target))

    counts = {
        "codings_moved": 1, "trashed_codings_moved": 1, "duplicates_dropped": 0,
        "memos_moved": 1, "passage_memos_relinked": 0,
        "children_moved": 2, "trashed_children_moved": 1,
    }
    for key, value in counts.items():
        assert impact[key] == value == result[key], key
    assert impact["source"] == {
        "id": source, "name": "Source", "description": "Talk about greetings."
    }
    assert impact["target"]["name"] == "Target"
    assert impact["description"] == result["description"] == "carried_over"

    # The target took over the definition and the active coding.
    assert result["code"]["id"] == target
    assert result["code"]["description"] == "Talk about greetings."
    assert result["code"]["coding_count"] == 1
    moved = (await rows("SELECT * FROM coding WHERE id=?", (active,)))[0]
    assert moved["code_id"] == target and moved["coder"] == "Timo"
    listed = await codings.list_codings(code_id=target)
    assert [row["fields"] for row in listed] == [{"Actor": "Alice"}]
    # The trashed coding moved unchanged and stays in the trash.
    after_trashed = (await rows("SELECT * FROM coding WHERE id=?", (trashed,)))[0]
    assert after_trashed == {**trashed_before, "code_id": target}

    memo_rows = {row["id"]: row for row in await rows("SELECT * FROM memo")}
    assert memo_rows[code_memo["id"]]["code_id"] == target
    assert memo_rows[passage_memo["id"]]["coding_id"] == active

    # Children follow the target's existing child, in their previous order.
    children = await rows(
        "SELECT id, sort_order, deleted_at FROM code WHERE parent_id=? ORDER BY sort_order",
        (target,),
    )
    # (Source order was: trashed child 0, child A 1, child B 2.)
    assert [row["id"] for row in children] == [existing_child, trashed_child, child_a, child_b]
    assert [row["sort_order"] for row in children] == [0, 1, 2, 3]
    assert children[1]["deleted_at"] is not None

    # The source is gone for good, not in the trash, and nothing points at it.
    assert await rows("SELECT id FROM code WHERE id=?", (source,)) == []
    assert source not in [row["id"] for row in await codes.list_deleted_codes(pid)]
    assert trashed_child in [row["id"] for row in await codes.list_deleted_codes(pid)]
    assert await rows("SELECT id FROM coding WHERE code_id=?", (source,)) == []
    assert await rows("SELECT id FROM memo WHERE code_id=?", (source,)) == []
    run = (await rows("SELECT * FROM coding_run WHERE id=?", (run_id,)))[0]
    assert run["proposed_parent_code_id"] == target

    # Restoring the trashed child brings it back under the target, with its coding.
    restored = await codes.restore_code(trashed_child)
    assert restored["parent_id"] == target
    child_coding = (await rows("SELECT * FROM coding WHERE id=?", (under_child,)))[0]
    assert child_coding["deleted_at"] is None

    # A verified backup was made in this (temporary) data folder and named in the result.
    assert backups(data_dir) == [result["backup"]]
    assert result["backup"].startswith("aqda-before-merge-")
    assert await revision(pid) == before + 1 + 1  # merge, then the restore above


@pytest.mark.asyncio
async def test_merge_drops_only_true_duplicates(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "duplicates")
    await db_module.init_db()
    p = await setup_project()
    pid, doc, actor = p["id"], p["doc_id"], p["actor"]
    agrees = (
        await fields_router.create_coding_field(
            fields_router.CodingFieldCreate(project_id=pid, name="Agrees", kind="yes_no")
        )
    )["id"]
    target = await new_code(pid, "Target")
    source = await new_code(pid, "Source")
    run_1 = await add_run(pid, "Run 1")
    run_2 = await add_run(pid, "Run 2")

    # On the target: a hand coding by Timo and a model coding of run 1.
    kept_hand = await add_coding(doc, target, 0, 5, answers={actor: "Alice"})
    kept_model = await add_coding(
        doc, target, 18, 21, coder="Run 1", run_id=run_1, answers={actor: "Bob", agrees: "0"}
    )

    # Exact repetitions: these two are dropped.
    dup_hand = await add_coding(doc, source, 0, 5, answers={actor: "Alice"})
    dup_model = await add_coding(
        doc, source, 18, 21, coder="Run 1", run_id=run_1, answers={agrees: "0", actor: "Bob"}
    )
    # Anything that differs survives.
    survivors = [
        await add_coding(doc, source, 0, 5, coder="Ana", answers={actor: "Alice"}),
        await add_coding(doc, source, 0, 5, coder="Run 1", run_id=run_1,
                         answers={actor: "Alice"}),
        await add_coding(doc, source, 0, 5, answers={actor: "Alicia"}),
        await add_coding(doc, source, 0, 5),
        await add_coding(doc, source, 0, 6, answers={actor: "Alice"}),
        await add_coding(doc, source, 18, 21, coder="Run 2", run_id=run_2,
                         answers={actor: "Bob", agrees: "0"}),
        await add_coding(doc, source, 18, 21, coder="Run 1", run_id=run_1,
                         answers={actor: "Bob", agrees: "1"}),
    ]
    # A removed exact repetition of an active coding is not dropped; it moves with its
    # trash state.
    trashed_dup = await add_coding(doc, source, 0, 5, answers={actor: "Alice"})
    await codings.delete_coding(trashed_dup)

    memo_on_dup = await memos.create_memo(
        memos.MemoCreate(
            project_id=pid, document_id=doc, coding_id=dup_hand, start_pos=0, end_pos=5,
            title="Why this matters",
        )
    )
    memo_on_model_dup = await memos.create_memo(
        memos.MemoCreate(project_id=pid, coding_id=dup_model, title="Model note")
    )

    impact = await codes.code_merge_impact(source, target_id=target)
    result = await codes.merge_code(source, codes.CodeMerge(target_id=target))
    for key in ("codings_moved", "trashed_codings_moved", "duplicates_dropped",
                "passage_memos_relinked"):
        assert impact[key] == result[key], key
    assert result["duplicates_dropped"] == 2
    assert result["codings_moved"] == len(survivors)
    assert result["trashed_codings_moved"] == 1
    assert result["passage_memos_relinked"] == 2

    remaining = {row["id"] for row in await rows(
        "SELECT id FROM coding WHERE code_id=?", (target,)
    )}
    assert remaining == {kept_hand, kept_model, trashed_dup, *survivors}
    assert await rows(
        "SELECT * FROM coding_field_value WHERE coding_id IN (?, ?)", (dup_hand, dup_model)
    ) == []
    memo_rows = {row["id"]: row for row in await rows("SELECT * FROM memo")}
    assert memo_rows[memo_on_dup["id"]]["coding_id"] == kept_hand
    assert memo_rows[memo_on_model_dup["id"]]["coding_id"] == kept_model
    assert result["code"]["coding_count"] == 2 + len(survivors)


@pytest.mark.asyncio
async def test_merge_refusals_change_nothing_and_make_no_backup(tmp_path, use_data_dir):
    data_dir = use_data_dir(tmp_path / "refusals")
    await db_module.init_db()
    p = await setup_project()
    other = await setup_project(name="Other study")
    pid = p["id"]
    parent = await new_code(pid, "Parent")
    child = await new_code(pid, "Child", parent_id=parent)
    grandchild = await new_code(pid, "Grandchild", parent_id=child)
    trashed = await new_code(pid, "Trashed")
    await codes.delete_code(trashed)
    foreign = await new_code(other["id"], "Foreign")
    plain = await new_code(pid, "Plain")
    await add_coding(p["doc_id"], parent, 0, 5)
    before = await revision(pid)

    cases = [
        (parent, parent, 400, "itself"),
        (parent, grandchild, 400, "child codes"),
        (parent, child, 400, "child codes"),
        (trashed, plain, 404, "trash"),
        (plain, trashed, 404, "trash"),
        (plain, 999999, 404, "not found"),
        (999999, plain, 404, "not found"),
        (plain, foreign, 400, "same project"),
    ]
    for source, target, status, words in cases:
        for call in (
            lambda: codes.code_merge_impact(source, target_id=target),
            lambda: codes.merge_code(source, codes.CodeMerge(target_id=target)),
        ):
            with pytest.raises(HTTPException) as refused:
                await call()
            assert refused.value.status_code == status
            assert words in refused.value.detail

    assert backups(data_dir) == []
    assert await revision(pid) == before
    assert {row["name"] for row in await codes.list_codes(pid)} == {
        "Parent", "Child", "Grandchild", "Plain"
    }

    # Merging a child into its parent (or any ancestor) is allowed.
    result = await codes.merge_code(grandchild, codes.CodeMerge(target_id=parent))
    assert result["code"]["id"] == parent


@pytest.mark.asyncio
async def test_merge_keeps_both_definitions(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "definitions")
    await db_module.init_db()
    p = await setup_project()
    pid = p["id"]
    target = await new_code(pid, "Target", description="The target's own definition.")
    differing = await new_code(pid, "Differing", description="Another definition.")
    same = await new_code(pid, "Same", description="The target's own definition.")
    empty = await new_code(pid, "Empty")

    impact = await codes.code_merge_impact(differing, target_id=target)
    assert impact["description"] == "saved_as_memo"
    result = await codes.merge_code(differing, codes.CodeMerge(target_id=target))
    assert result["description"] == "saved_as_memo"
    assert result["code"]["description"] == "The target's own definition."
    assert result["source"]["description"] == "Another definition."
    memo = (await rows("SELECT * FROM memo WHERE id=?", (result["description_memo_id"],)))[0]
    assert memo["code_id"] == target and memo["project_id"] == pid
    assert memo["content"] == "Another definition."
    assert "Differing" in memo["title"]

    for source in (same, empty):
        result = await codes.merge_code(source, codes.CodeMerge(target_id=target))
        assert result["description"] in {"same", "none"}
        assert result["description_memo_id"] is None
        assert result["code"]["description"] == "The target's own definition."
    assert len(await rows("SELECT id FROM memo WHERE code_id=?", (target,))) == 1


@pytest.mark.asyncio
async def test_merge_routes_over_http(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "http")
    await db_module.init_db()
    p = await setup_project()
    target = await new_code(p["id"], "Target")
    source = await new_code(p["id"], "Source")
    await add_coding(p["doc_id"], source, 0, 5)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8765") as client:
        impact = await client.get(f"/api/codes/{source}/merge-impact?target_id={target}")
        refused = await client.post(f"/api/codes/{source}/merge", json={"target_id": source})
        merged = await client.post(f"/api/codes/{source}/merge", json={"target_id": target})
    assert impact.status_code == 200 and impact.json()["codings_moved"] == 1
    assert refused.status_code == 400
    assert refused.json()["detail"] == "A code cannot be merged into itself."
    assert merged.status_code == 200
    assert merged.json()["code"]["coding_count"] == 1


async def project_content(project_id: int) -> dict:
    """Describe a project's codes, codings, answers, and memos by content, not by ID."""
    code_rows = await rows(
        "SELECT c.name, parent.name AS parent, c.description, c.sort_order, "
        "c.deleted_at IS NOT NULL AS trashed FROM code c "
        "LEFT JOIN code parent ON parent.id=c.parent_id WHERE c.project_id=? ORDER BY c.name",
        (project_id,),
    )
    coding_rows = await rows(
        "SELECT c.name AS code, cg.start_pos, cg.end_pos, cg.coder, r.label AS run, "
        "cg.deleted_at IS NOT NULL AS trashed, "
        "(SELECT group_concat(f.name || '=' || v.value) FROM coding_field_value v "
        " JOIN coding_field f ON f.id=v.field_id WHERE v.coding_id=cg.id) AS answers "
        "FROM coding cg JOIN code c ON c.id=cg.code_id "
        "JOIN document d ON d.id=cg.document_id LEFT JOIN coding_run r ON r.id=cg.run_id "
        "WHERE d.project_id=? ORDER BY cg.start_pos, cg.coder, c.name",
        (project_id,),
    )
    memo_rows = await rows(
        "SELECT m.title, c.name AS code, cg.start_pos AS coding_start, cg.coder AS coding_coder "
        "FROM memo m LEFT JOIN code c ON c.id=m.code_id LEFT JOIN coding cg ON cg.id=m.coding_id "
        "WHERE m.project_id=? ORDER BY m.title",
        (project_id,),
    )
    runs = await rows(
        "SELECT r.label, c.name AS parent FROM coding_run r "
        "LEFT JOIN code c ON c.id=r.proposed_parent_code_id WHERE r.project_id=?",
        (project_id,),
    )
    return {"codes": code_rows, "codings": coding_rows, "memos": memo_rows, "runs": runs}


@pytest.mark.asyncio
async def test_merged_project_survives_export_and_import(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "source")
    await db_module.init_db()
    p = await setup_project()
    pid, doc, actor = p["id"], p["doc_id"], p["actor"]
    target = await new_code(pid, "Target", description="Kept definition.")
    source = await new_code(pid, "Source", description="Merged definition.")
    child = await new_code(pid, "Child", parent_id=source)
    trashed_child = await new_code(pid, "Trashed child", parent_id=source)
    run_id = await add_run(pid, "Run 1", parent_code_id=source)
    kept = await add_coding(doc, target, 0, 5, answers={actor: "Alice"})
    dup = await add_coding(doc, source, 0, 5, answers={actor: "Alice"})
    await add_coding(doc, source, 18, 21, coder="Run 1", run_id=run_id, answers={actor: "Bob"})
    trashed = await add_coding(doc, source, 6, 10)
    await codings.delete_coding(trashed)
    await add_coding(doc, child, 11, 16)
    await add_coding(doc, trashed_child, 22, 26)
    await codes.delete_code(trashed_child)
    await memos.create_memo(
        memos.MemoCreate(project_id=pid, coding_id=dup, title="On the duplicate")
    )
    await memos.create_memo(memos.MemoCreate(project_id=pid, code_id=source, title="Code memo"))
    await codes.merge_code(source, codes.CodeMerge(target_id=target))
    expected = await project_content(pid)
    assert [m["coding_start"] for m in expected["memos"] if m["title"] == "On the duplicate"] == [0]
    assert expected["runs"] == [{"label": "Run 1", "parent": "Target"}]
    assert kept

    package, _, _ = await build_aqda_snapshot(pid)
    use_data_dir(tmp_path / "destination")
    await db_module.init_db()
    await setup_project(name="Unrelated")  # occupy low IDs so remapping is exercised
    imported = await projects.import_from_db(
        UploadFile(filename="project.aqda", file=io.BytesIO(package)), mode="auto"
    )
    new_id = imported["imported"][0]["id"]
    assert await project_content(new_id) == expected

    # The trashed child is still restorable under the target in the imported copy.
    trashed_rows = await codes.list_deleted_codes(new_id)
    assert [row["name"] for row in trashed_rows] == ["Trashed child"]
    assert trashed_rows[0]["parent_name"] == "Target"
    assert trashed_rows[0]["coding_count"] == 1
    await codes.restore_code(trashed_rows[0]["id"])
    assert {row["name"] for row in await codes.list_codes(new_id)} == {
        "Target", "Child", "Trashed child"
    }


@pytest.mark.asyncio
async def test_merge_then_restoring_a_removed_run_brings_back_no_duplicates(
    tmp_path, use_data_dir
):
    from aqda.routers import runs

    use_data_dir(tmp_path / "removed-run")
    await db_module.init_db()
    p = await setup_project()
    pid, doc, actor = p["id"], p["doc_id"], p["actor"]
    target = await new_code(pid, "Target")
    source = await new_code(pid, "Source")
    run_id = await add_run(pid, "Run 1")
    run_uuid = (await rows("SELECT uuid FROM coding_run WHERE id=?", (run_id,)))[0]["uuid"]

    # The run coded the same passage with both codes, with the same answers.
    kept = await add_coding(doc, target, 0, 5, coder="Run 1", run_id=run_id,
                            answers={actor: "Alice"})
    repeated = await add_coding(doc, source, 0, 5, coder="Run 1", run_id=run_id,
                                answers={actor: "Alice"})
    # Out of use too, but in another way than the target's coding: it moves.
    different = await add_coding(doc, source, 18, 21, coder="Run 1", run_id=run_id)
    memo = await memos.create_memo(
        memos.MemoCreate(project_id=pid, coding_id=repeated, title="On the repeat")
    )
    await runs.remove_run(run_uuid)
    await codings.delete_coding(different)

    impact = await codes.code_merge_impact(source, target_id=target)
    result = await codes.merge_code(source, codes.CodeMerge(target_id=target))
    for key in ("codings_moved", "trashed_codings_moved", "duplicates_dropped",
                "passage_memos_relinked"):
        assert impact[key] == result[key], key
    assert result["codings_moved"] == 0
    assert result["duplicates_dropped"] == 1
    assert result["trashed_codings_moved"] == 1
    assert result["passage_memos_relinked"] == 1
    assert await rows("SELECT id FROM coding WHERE id=?", (repeated,)) == []
    memo_row = (await rows("SELECT coding_id FROM memo WHERE id=?", (memo["id"],)))[0]
    assert memo_row["coding_id"] == kept

    await runs.restore_run(run_uuid)
    active = await rows(
        "SELECT id FROM coding WHERE code_id=? AND deleted_at IS NULL", (target,)
    )
    assert [row["id"] for row in active] == [kept]


@pytest.mark.asyncio
async def test_merge_keeps_only_the_ten_newest_merge_backups(tmp_path, use_data_dir):
    data_dir = use_data_dir(tmp_path / "merge-backups")
    await db_module.init_db()
    p = await setup_project()
    folder = data_dir / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    old = [folder / f"aqda-before-merge-2020010{day}T000000.000000Z.db" for day in range(1, 10)]
    old += [folder / f"aqda-before-merge-2020011{day}T000000.000000Z.db" for day in range(0, 3)]
    for path in old:
        path.write_bytes(b"")
    other = folder / "aqda-daily-20200101T000000.000000Z.db"
    other.write_bytes(b"")

    target = await new_code(p["id"], "Target")
    source = await new_code(p["id"], "Source")
    result = await codes.merge_code(source, codes.CodeMerge(target_id=target))

    kept = backups(data_dir)
    assert len(kept) == 10
    assert result["backup"] in kept
    # The oldest merge backups went; other backups are left alone.
    assert kept[:-1] == sorted(path.name for path in old)[-9:]
    assert other.exists()
