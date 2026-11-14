import hashlib
import itertools
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException

import aqda.db as db_module
from aqda.routers import ai, codes, codings, documents, projects, runs
from aqda.routers import coding_fields as fields_router
from aqda.routers.export import build_aqda_snapshot

TEXT = (
    "Alice: We want jobs. Bob: We want jobs. Carol: “Subsidies don’t work.” "
    "Dan: The state must act – now. Eve: Taxes are too high."
)


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


# A project with a code tree (Economy > Jobs, Policy) and one text document.
async def make_project(name="Survey", text=TEXT) -> dict:
    project = await projects.create_project(projects.ProjectCreate(name=name))
    pid = project["id"]
    economy = await codes.create_code(
        codes.CodeCreate(project_id=pid, name="Economy", description="About the economy")
    )
    jobs = await codes.create_code(
        codes.CodeCreate(project_id=pid, name="Jobs", parent_id=economy["id"])
    )
    policy = await codes.create_code(codes.CodeCreate(project_id=pid, name="Policy"))
    doc_id = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'Interview', ?)",
        (pid, text),
    )
    return {
        "id": pid, "doc": doc_id,
        "economy": economy["id"], "jobs": jobs["id"], "policy": policy["id"],
    }


async def start_run(project_id: int, label="GPT pass", **extra) -> dict:
    return await runs.create_run(
        runs.RunCreate(project_id=project_id, label=label, model="test-model", **extra)
    )


async def submit(run: dict, doc_id: int, passages, **extra) -> dict:
    return await runs.submit_document(
        run["uuid"], doc_id, runs.DocumentSubmission(passages=passages, **extra)
    )


@pytest_asyncio.fixture
async def world(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "data")
    await db_module.init_db()
    return await make_project()


# --- Document text ---------------------------------------------------------


@pytest.mark.asyncio
async def test_document_text_is_the_codeable_text_with_its_fingerprint(world):
    result = await documents.get_document_text(world["doc"])
    assert result["text"] == TEXT
    assert result["length"] == len(TEXT)
    assert result["text_hash"] == hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    assert (result["document_id"], result["name"]) == (world["doc"], "Interview")

    audio = await execute(
        "INSERT INTO document (project_id, name, content, source_type, transcript) "
        "VALUES (?, 'Recording', 'BASE64AUDIO', 'audio', 'Spoken 😀 words')",
        (world["id"],),
    )
    spoken = await documents.get_document_text(audio)
    assert spoken["text"] == "Spoken 😀 words"
    assert spoken["length"] == 14  # code points, not UTF-16 units

    with pytest.raises(HTTPException) as missing:
        await documents.get_document_text(999)
    assert missing.value.status_code == 404


# --- Starting and listing runs -----------------------------------------------


@pytest.mark.asyncio
async def test_run_records_the_codebook_and_needs_a_label(world):
    before = await revision(world["id"])
    run = await start_run(
        world["id"], instructions="Code stances.", settings={"temperature": 0}, note="pilot"
    )
    assert await revision(world["id"]) == before + 1
    assert uuid.UUID(run["uuid"])
    assert run["status"] == "running" and not run["removed"]
    assert run["settings"] == {"temperature": 0}
    assert run["allow_new_codes"] is False
    assert run["codebook"] == [
        {"path": ["Economy"], "name": "Economy", "description": "About the economy",
         "id_at_start": world["economy"]},
        {"path": ["Economy", "Jobs"], "name": "Jobs", "description": "",
         "id_at_start": world["jobs"]},
        {"path": ["Policy"], "name": "Policy", "description": "",
         "id_at_start": world["policy"]},
    ]
    for blank in ("", "   "):
        with pytest.raises(HTTPException) as refused:
            await start_run(world["id"], label=blank)
        assert refused.value.status_code == 422
    with pytest.raises(HTTPException) as missing:
        await start_run(999)
    assert missing.value.status_code == 404


@pytest.mark.asyncio
async def test_list_and_detail_carry_counts(world):
    run = await start_run(world["id"])
    other_doc = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'Empty', 'Nothing.')",
        (world["id"],),
    )
    await submit(run, world["doc"], [
        {"quote": "Taxes are too high.", "code_id": world["policy"]},
        {"quote": "not in the text", "code_id": world["policy"]},
    ], raw_response="{...}")
    await submit(run, other_doc, [])

    listed = await runs.list_runs(world["id"])
    assert len(listed) == 1
    assert (listed[0]["documents_read"], listed[0]["codings"], listed[0]["refused"]) == (2, 1, 1)
    assert listed[0]["passages_submitted"] == 2

    detail = await runs.get_run(run["uuid"])
    by_doc = {row["document_id"]: row for row in detail["documents"]}
    assert by_doc[world["doc"]]["document_name"] == "Interview"
    assert (by_doc[world["doc"]]["submitted"], by_doc[world["doc"]]["accepted"]) == (2, 1)
    assert by_doc[world["doc"]]["refused"][0]["quote"] == "not in the text"
    assert by_doc[world["doc"]]["raw_response"] == "{...}"
    assert by_doc[world["doc"]]["codings"] == 1
    assert by_doc[other_doc]["submitted"] == 0 and by_doc[other_doc]["refused"] == []

    await runs.remove_run(run["uuid"])
    assert await runs.list_runs(world["id"]) == []
    removed = await runs.list_runs(world["id"], include_removed=True)
    assert removed[0]["removed"] and removed[0]["status"] == "stopped"
    assert (removed[0]["codings"], removed[0]["codings_removed_with_run"]) == (0, 1)


# --- Submitting a document ---------------------------------------------------


@pytest.mark.asyncio
async def test_passages_are_placed_and_refused_one_by_one(world):
    run = await start_run(world["id"])
    before = await revision(world["id"])
    result = await submit(run, world["doc"], [
        # 0: tolerant quote, code by ID
        {"quote": '"Subsidies don\'t work."', "code_id": world["policy"]},
        # 1: repeated words, resolved by context; code by path
        {"quote": "We want jobs.", "before": "Bob:", "code_path": ["Economy", "Jobs"]},
        # 2: repeated words without context
        {"quote": "We want jobs.", "code_id": world["jobs"]},
        # 3: not in the text
        {"quote": "We want houses.", "code_id": world["jobs"]},
        # 4: exact position, quote checked tolerantly
        {"start_pos": TEXT.index("The state"), "end_pos": TEXT.index(" Eve"),
         "quote": "The state must act - now.", "code_path": ["policy"]},
        # 5: position and a quote that does not match it
        {"start_pos": 0, "end_pos": 6, "quote": "Bob:", "code_id": world["policy"]},
        # 6: unknown code path
        {"quote": "Taxes are too high.", "code_path": ["Economy", "Taxes"]},
        # 7: no code
        {"quote": "Taxes are too high."},
        # 8: new code in a run that may not propose any
        {"quote": "Taxes are too high.", "new_code": {"name": "Taxes"}},
        # 9: position outside the text
        {"start_pos": 5, "end_pos": len(TEXT) + 1, "code_id": world["policy"]},
        # 10: not a passage at all
        "Taxes",
        # 11: elided quote
        {"quote": "Dan: The state ... now.", "code_id": world["policy"]},
    ])
    assert await revision(world["id"]) == before + 1
    accepted = {item["index"]: item for item in result["accepted"]}
    assert sorted(accepted) == [0, 1, 4]
    assert accepted[0]["method"] == "tolerant"
    assert accepted[1]["method"] == "exact"
    assert accepted[4]["method"] == "position"

    coding = accepted[0]["coding"]
    assert coding["selected_text"] == "“Subsidies don’t work.”"
    assert TEXT[coding["start_pos"]:coding["end_pos"]] == coding["selected_text"]
    assert coding["run_id"] == run["id"]
    assert (coding["coder"], coding["run_label"], coding["run_model"]) == (
        "GPT pass", "GPT pass", "test-model"
    )
    assert coding["offset_unit"] == "codepoint" and coding["fields"] == {}
    assert coding["code_name"] == "Policy" and coding["document_name"] == "Interview"
    assert accepted[1]["coding"]["start_pos"] == TEXT.index("Bob:") + 5
    assert accepted[1]["coding"]["code_id"] == world["jobs"]
    # A case difference in a path is tolerated when it still names one code.
    assert accepted[4]["coding"]["code_id"] == world["policy"]

    reasons = {item["index"]: item["reason"] for item in result["refused"]}
    assert sorted(reasons) == [2, 3, 5, 6, 7, 8, 9, 10, 11]
    assert "2 times" in reasons[2]
    assert "not found" in reasons[3]
    assert "does not match" in reasons[5]
    assert "Economy > Taxes" in reasons[6]
    assert "names no code" in reasons[7]
    assert "new codes" in reasons[8]
    assert "outside" in reasons[9]
    assert "expected form" in reasons[10]
    assert "leaves words out" in reasons[11]
    assert result["refused"][0]["quote"] == "We want jobs."
    assert result["counts"] == {"submitted": 12, "accepted": 3, "refused": 9}

    listed = await codings.list_codings(document_id=world["doc"])
    assert len(listed) == 3
    stored = await rows(
        "SELECT * FROM coding_run_document WHERE run_id=?", (run["id"],)
    )
    assert stored[0]["text_hash"] == (await documents.get_document_text(world["doc"]))["text_hash"]
    assert (stored[0]["submitted"], stored[0]["accepted"]) == (12, 3)


@pytest.mark.asyncio
async def test_resubmitting_replaces_the_earlier_submission(world):
    run = await start_run(world["id"])
    first = await submit(run, world["doc"], [
        {"quote": "Taxes are too high.", "code_id": world["policy"]},
        {"quote": "missing words", "code_id": world["policy"]},
    ], raw_response="first")
    old_id = first["accepted"][0]["coding"]["id"]
    second = await submit(run, world["doc"], [
        {"quote": "The state must act", "code_id": world["policy"]},
    ], raw_response="second")
    active = await codings.list_codings(document_id=world["doc"])
    assert [c["id"] for c in active] == [second["accepted"][0]["coding"]["id"]]
    stored = await rows("SELECT * FROM coding_run_document WHERE run_id=?", (run["id"],))
    assert len(stored) == 1
    assert (stored[0]["submitted"], stored[0]["accepted"], stored[0]["refused"]) == (1, 1, "[]")
    assert stored[0]["raw_response"] == "second"

    # Removing and restoring the run never brings the replaced coding back.
    await runs.remove_run(run["uuid"])
    await execute("UPDATE coding_run SET status='running' WHERE id=?", (run["id"],))
    await runs.restore_run(run["uuid"])
    active_ids = {c["id"] for c in await codings.list_codings(document_id=world["doc"])}
    assert old_id not in active_ids
    assert len(active_ids) == 1

    # An empty resubmission still records the document as read, with nothing coded.
    await submit(run, world["doc"], [])
    assert await codings.list_codings(document_id=world["doc"]) == []
    stored = await rows("SELECT * FROM coding_run_document WHERE run_id=?", (run["id"],))
    assert (stored[0]["submitted"], stored[0]["accepted"]) == (0, 0)


@pytest.mark.asyncio
async def test_resubmission_also_retires_codings_waiting_in_the_code_trash(world):
    run = await start_run(world["id"])
    await submit(run, world["doc"], [{"quote": "We want jobs.", "before": "Alice:",
                                      "code_id": world["jobs"]}])
    await codes.delete_code(world["economy"])
    await submit(run, world["doc"], [{"quote": "Taxes", "code_id": world["policy"]}])
    await codes.restore_code(world["economy"])
    active = await codings.list_codings(document_id=world["doc"])
    assert [c["code_id"] for c in active] == [world["policy"]]


@pytest.mark.asyncio
async def test_changed_text_refuses_the_whole_submission(world):
    run = await start_run(world["id"])
    before = await revision(world["id"])
    with pytest.raises(HTTPException) as changed:
        await submit(run, world["doc"], [{"quote": "Taxes", "code_id": world["policy"]}],
                     text_hash="0" * 64)
    assert changed.value.status_code == 409
    assert await revision(world["id"]) == before
    assert await rows("SELECT * FROM coding_run_document") == []
    assert await codings.list_codings(document_id=world["doc"]) == []

    current = (await documents.get_document_text(world["doc"]))["text_hash"]
    result = await submit(run, world["doc"], [{"quote": "Taxes", "code_id": world["policy"]}],
                          text_hash=current)
    assert result["counts"]["accepted"] == 1


@pytest.mark.asyncio
async def test_submission_is_refused_for_closed_runs_and_foreign_documents(world):
    run = await start_run(world["id"])
    other = await make_project("Other")
    with pytest.raises(HTTPException) as foreign:
        await submit(run, other["doc"], [])
    assert foreign.value.status_code == 400
    with pytest.raises(HTTPException) as missing:
        await submit(run, 999, [])
    assert missing.value.status_code == 404

    await execute("UPDATE document SET exclude_from_ai=1 WHERE id=?", (world["doc"],))
    with pytest.raises(HTTPException) as excluded:
        await submit(run, world["doc"], [])
    assert excluded.value.status_code == 409
    await execute("UPDATE document SET exclude_from_ai=0 WHERE id=?", (world["doc"],))

    finished = await runs.finish_run(run["uuid"])
    assert finished["status"] == "finished" and finished["finished_at"]
    with pytest.raises(HTTPException) as closed:
        await submit(run, world["doc"], [])
    assert closed.value.status_code == 409

    removed_run = await start_run(world["id"], label="Second")
    await runs.remove_run(removed_run["uuid"])
    with pytest.raises(HTTPException) as removed:
        await submit(removed_run, world["doc"], [])
    assert removed.value.status_code == 409
    with pytest.raises(HTTPException) as unknown:
        await runs.get_run(str(uuid.uuid4()))
    assert unknown.value.status_code == 404


@pytest.mark.asyncio
async def test_finish_is_idempotent_and_remove_stops_a_running_run(world):
    run = await start_run(world["id"])
    stopped = await runs.finish_run(run["uuid"], runs.RunFinish(status="stopped"))
    assert stopped["status"] == "stopped"
    revision_after = await revision(world["id"])
    again = await runs.finish_run(run["uuid"], runs.RunFinish(status="finished"))
    assert again["status"] == "stopped"
    assert again["finished_at"] == stopped["finished_at"]
    assert await revision(world["id"]) == revision_after
    with pytest.raises(HTTPException) as invalid:
        await runs.finish_run(run["uuid"], runs.RunFinish(status="running"))
    assert invalid.value.status_code == 422

    running = await start_run(world["id"], label="Second")
    removed = await runs.remove_run(running["uuid"])
    assert removed["status"] == "stopped" and removed["removed"] and removed["finished_at"]
    assert (await runs.remove_run(running["uuid"]))["deleted_at"] == removed["deleted_at"]
    restored = await runs.restore_run(running["uuid"])
    assert not restored["removed"] and restored["status"] == "stopped"


@pytest.mark.asyncio
async def test_new_codes_go_under_a_parent_named_after_the_run(world):
    run = await start_run(world["id"], label="Pass 1", allow_new_codes=True)
    result = await submit(run, world["doc"], [
        {"quote": "Taxes are too high.", "new_code": {"name": "Tax burden",
                                                      "description": "Taxes as a burden"}},
        {"quote": "The state must act", "new_code": {"name": "tax  BURDEN"}},
        {"quote": "Subsidies", "new_code": {"name": "Subsidy doubts", "definition": "Doubt"}},
        {"quote": "Subsidies", "new_code": {"name": "  "}},
    ])
    assert len(result["accepted"]) == 3
    assert "needs a name" in result["refused"][0]["reason"]
    parent = (await rows("SELECT * FROM code WHERE name='Proposed by Pass 1'"))[0]
    assert parent["parent_id"] is None
    children = await rows(
        "SELECT name, description FROM code WHERE parent_id=? ORDER BY id", (parent["id"],)
    )
    assert children == [
        {"name": "Tax burden", "description": "Taxes as a burden"},
        {"name": "Subsidy doubts", "description": "Doubt"},
    ]
    # Proposed codes get different colours, like codes created by hand.
    colors = await rows("SELECT color FROM code WHERE parent_id=?", (parent["id"],))
    assert len({row["color"] for row in colors}) == 2
    assert result["accepted"][0]["coding"]["code_id"] == result["accepted"][1]["coding"]["code_id"]
    assert (await runs.get_run(run["uuid"]))["proposed_parent_code_id"] == parent["id"]

    # A later run with the same label gets its own parent.
    second = await start_run(world["id"], label="Pass 1", allow_new_codes=True)
    await submit(second, world["doc"], [{"quote": "Taxes", "new_code": {"name": "Tax burden"}}])
    assert await rows("SELECT name FROM code WHERE name LIKE 'Proposed by Pass 1%' ORDER BY id") \
        == [{"name": "Proposed by Pass 1"}, {"name": "Proposed by Pass 1 (2)"}]

    # A trashed parent is not silently replaced.
    await codes.delete_code(parent["id"])
    refused = await submit(run, world["doc"], [{"quote": "Taxes", "new_code": {"name": "X"}}])
    assert "in the trash" in refused["refused"][0]["reason"]


@pytest.mark.asyncio
async def test_field_answers_are_checked_and_duplicates_refused(world):
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=world["id"], name="Actor", kind="text")
    )
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=world["id"], name="Agrees", kind="yes_no")
    )
    run = await start_run(world["id"])
    result = await submit(run, world["doc"], [
        {"quote": "Taxes are too high.", "code_id": world["policy"],
         "fields": {"Actor": "Eve", "agrees": "no"}},
        {"quote": "Taxes are too high.", "code_id": world["policy"],
         "fields": {"Actor": "Eve", "Agrees": "no"}},
        {"quote": "Taxes are too high.", "code_id": world["policy"],
         "fields": {"Actor": "Somebody else"}},
        {"quote": "Taxes are too high.", "code_id": world["policy"], "fields": {"Agrees": 7}},
        {"quote": "Taxes are too high.", "code_id": world["policy"], "fields": {"Mood": "x"}},
        {"quote": "Taxes are too high.", "code_id": world["policy"], "fields": ["Eve"]},
    ])
    assert [item["index"] for item in result["accepted"]] == [0, 2]
    assert result["accepted"][0]["coding"]["fields"] == {"Actor": "Eve", "Agrees": "0"}
    reasons = {item["index"]: item["reason"] for item in result["refused"]}
    assert "already submitted" in reasons[1]
    assert "yes or no" in reasons[3]
    assert "Mood" in reasons[4]
    assert "by field name" in reasons[5]

    # A hand coding of the same passage, code, and answers is not a duplicate.
    hand = await codings.create_coding(codings.CodingCreate(
        document_id=world["doc"], code_id=world["policy"],
        start_pos=TEXT.index("Taxes"), end_pos=len(TEXT), selected_text="Taxes are too high.",
        fields={"Actor": "Eve", "Agrees": "no"},
    ))
    assert hand["run_id"] is None


# Accepting a model coding makes it the researcher's own: one coding, counted once.
@pytest.mark.asyncio
async def test_accepting_a_model_coding_makes_it_a_hand_coding(world):
    await fields_router.create_coding_field(
        fields_router.CodingFieldCreate(project_id=world["id"], name="Actor", kind="text")
    )
    await execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('coder_name', 'Timo')")
    run = await start_run(world["id"], label="First pass")
    result = await submit(run, world["doc"], [
        {"quote": "Taxes are too high.", "code_id": world["policy"], "fields": {"Actor": "Eve"}},
        {"quote": "The state must act", "code_id": world["policy"]},
    ])
    taxes, state = (item["coding"]["id"] for item in result["accepted"])
    before = await revision(world["id"])

    accepted = await codings.accept_coding(taxes)
    assert accepted["run_id"] is None and accepted["coder"] == "Timo"
    assert accepted["accepted_from_run_label"] == "First pass"
    assert accepted["accepted_from_run_model"] == "test-model"
    assert accepted["fields"] == {"Actor": "Eve"}
    assert await revision(world["id"]) > before
    # Still one coding on the passage, and the run counts it as accepted, not as its own.
    listed = await codings.list_codings(document_id=world["doc"])
    assert sorted(row["id"] for row in listed) == sorted([taxes, state])
    summary = (await runs.list_runs(world["id"]))[0]
    assert (summary["codings"], summary["codings_accepted"]) == (1, 1)

    # It is a hand coding from now on: answers can be changed, a second accept is refused.
    changed = await codings.replace_coding_fields(
        taxes, codings.CodingFieldsUpdate(fields={"Actor": "Eve Example"})
    )
    assert changed["fields"] == {"Actor": "Eve Example"}
    with pytest.raises(HTTPException) as error:
        await codings.accept_coding(taxes)
    assert error.value.status_code == 409
    with pytest.raises(HTTPException) as error:
        await codings.accept_coding(999999)
    assert error.value.status_code == 404

    # Removing the run takes the unaccepted coding away and leaves the accepted one;
    # restoring brings only the run's own coding back.
    await runs.remove_run(run["uuid"])
    assert [row["id"] for row in await codings.list_codings(document_id=world["doc"])] == [taxes]
    with pytest.raises(HTTPException) as error:
        await codings.accept_coding(state)
    assert error.value.status_code == 404
    await runs.restore_run(run["uuid"])
    listed = {row["id"]: row for row in await codings.list_codings(document_id=world["doc"])}
    assert listed[taxes]["run_id"] is None and listed[state]["run_id"] == run["id"]


# Accepting is refused when the researcher already coded the passage the same way.
@pytest.mark.asyncio
async def test_accepting_is_refused_next_to_an_identical_own_coding(world):
    await execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('coder_name', 'Timo')")
    run = await start_run(world["id"])
    result = await submit(run, world["doc"], [
        {"quote": "Taxes are too high.", "code_id": world["policy"]},
    ])
    model_coding = result["accepted"][0]["coding"]["id"]
    await codings.create_coding(codings.CodingCreate(
        document_id=world["doc"], code_id=world["policy"],
        start_pos=TEXT.index("Taxes"), end_pos=len(TEXT), selected_text="Taxes are too high.",
    ))
    with pytest.raises(HTTPException) as error:
        await codings.accept_coding(model_coding)
    assert error.value.status_code == 409 and "already coded" in error.value.detail
    still = {row["id"]: row for row in await codings.list_codings(document_id=world["doc"])}
    assert still[model_coding]["run_id"] == run["id"]

    # Another coder on the same passage is no obstacle.
    await execute("UPDATE setting SET value='Second coder' WHERE key='coder_name'")
    accepted = await codings.accept_coding(model_coding)
    assert accepted["coder"] == "Second coder" and accepted["run_id"] is None


@pytest.mark.asyncio
async def test_ambiguous_run_uuid_needs_a_project(world):
    run = await start_run(world["id"])
    copy = await make_project("Copy")
    await execute(
        "INSERT INTO coding_run (project_id, uuid, label, status) VALUES (?, ?, 'GPT pass', "
        "'running')",
        (copy["id"], run["uuid"]),
    )
    with pytest.raises(HTTPException) as ambiguous:
        await runs.get_run(run["uuid"])
    assert ambiguous.value.status_code == 409
    assert "project_id" in ambiguous.value.detail
    assert (await runs.get_run(run["uuid"], project_id=world["id"]))["id"] == run["id"]
    result = await runs.submit_document(
        run["uuid"], copy["doc"],
        runs.DocumentSubmission(passages=[{"quote": "Taxes", "code_id": copy["policy"]}]),
        project_id=copy["id"],
    )
    assert result["counts"]["accepted"] == 1

    # A trashed project does not count.
    await projects.delete_project(copy["id"])
    assert (await runs.get_run(run["uuid"]))["id"] == run["id"]


# --- The "Active" rule -------------------------------------------------------
#
# A coding is active exactly when it was not removed on its own (or replaced by a
# resubmission), its code is not in the trash, and its run is not removed.

SENTENCES = [f"Sentence number {index} is here." for index in range(10)]
MATRIX_TEXT = " ".join(SENTENCES)


def span(index: int) -> dict:
    start = MATRIX_TEXT.index(SENTENCES[index])
    return {"start_pos": start, "end_pos": start + len(SENTENCES[index])}


async def build_matrix_world(name: str) -> dict:
    """Codings of every kind on code X (with child X2), Y, by hand and from two runs."""
    project = await projects.create_project(projects.ProjectCreate(name=name))
    pid = project["id"]
    x = (await codes.create_code(codes.CodeCreate(project_id=pid, name="X")))["id"]
    x2 = (await codes.create_code(codes.CodeCreate(project_id=pid, name="X2", parent_id=x)))["id"]
    y = (await codes.create_code(codes.CodeCreate(project_id=pid, name="Y")))["id"]
    doc = await execute(
        "INSERT INTO document (project_id, name, content) VALUES (?, 'd', ?)", (pid, MATRIX_TEXT)
    )

    async def hand(index, code):
        return (await codings.create_coding(codings.CodingCreate(
            document_id=doc, code_id=code, selected_text=SENTENCES[index], **span(index)
        )))["id"]

    world = {"project": pid, "x": x, "doc": doc, "codings": {}}
    world["codings"]["hand_x"] = (await hand(0, x), x, None)
    world["codings"]["hand_x2"] = (await hand(1, x2), x2, None)
    world["codings"]["hand_removed"] = (await hand(2, x), x, None)
    await codings.delete_coding(world["codings"]["hand_removed"][0])

    run = await start_run(pid, label="Run A")
    other = await start_run(pid, label="Run B")
    world["run"], world["run_id"] = run["uuid"], run["id"]
    # Passages given by position only need the fingerprint of the text.
    fingerprint = {"text_hash": hashlib.sha256(MATRIX_TEXT.encode("utf-8")).hexdigest()}
    replaced = await submit(run, doc, [{**span(3), "code_id": x}], **fingerprint)
    result = await submit(run, doc, [
        {**span(4), "code_id": x}, {**span(5), "code_id": x2},
        {**span(6), "code_id": y}, {**span(7), "code_id": x},
    ], **fingerprint)
    ids = [item["coding"]["id"] for item in result["accepted"]]
    world["codings"]["run_x"] = (ids[0], x, run["id"])
    world["codings"]["run_x2"] = (ids[1], x2, run["id"])
    world["codings"]["run_y"] = (ids[2], y, run["id"])
    world["codings"]["run_removed"] = (ids[3], x, run["id"])
    world["codings"]["run_replaced"] = (replaced["accepted"][0]["coding"]["id"], x, run["id"])
    await codings.delete_coding(ids[3])
    other_result = await submit(other, doc, [{**span(8), "code_id": x}], **fingerprint)
    world["codings"]["other_run_x"] = (other_result["accepted"][0]["coding"]["id"], x, other["id"])
    world["removed_alone"] = {"hand_removed", "run_removed", "run_replaced"}
    world["run_removed"] = False
    world["code_trashed"] = False
    return world


async def apply(world: dict, operation: str) -> None:
    if operation == "remove_run":
        await runs.remove_run(world["run"], project_id=world["project"])
        world["run_removed"] = True
    elif operation == "restore_run":
        await runs.restore_run(world["run"], project_id=world["project"])
        world["run_removed"] = False
    elif operation == "delete_code":
        if not world["code_trashed"]:
            await codes.delete_code(world["x"])
            world["code_trashed"] = True
    elif operation == "restore_code":
        await codes.restore_code(world["x"])
        world["code_trashed"] = False
    elif operation == "delete_single":
        await codings.delete_coding(world["codings"]["run_x"][0])
        world["removed_alone"].add("run_x")


async def check_active(world: dict, context) -> None:
    states = {
        row["id"]: row["deleted_at"] is None
        for row in await rows(
            "SELECT cg.id, cg.deleted_at FROM coding cg JOIN document d ON d.id=cg.document_id "
            "WHERE d.project_id=?",
            (world["project"],),
        )
    }
    trashed_codes = {
        row["id"] for row in await rows(
            "SELECT id FROM code WHERE project_id=? AND deleted_at IS NOT NULL",
            (world["project"],),
        )
    }
    assert bool(trashed_codes) == world["code_trashed"], context
    for name, (coding_id, code_id, run_id) in world["codings"].items():
        expected = (
            name not in world["removed_alone"]
            and code_id not in trashed_codes
            and not (run_id == world["run_id"] and world["run_removed"])
        )
        assert states[coding_id] == expected, (context, name)


OPERATIONS = ["remove_run", "restore_run", "delete_code", "restore_code", "delete_single"]


@pytest.mark.asyncio
async def test_active_rule_holds_after_every_order_of_operations(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "matrix")
    await db_module.init_db()
    sequences = [
        sequence
        for length in (1, 2, 3, 4)
        for sequence in itertools.product(OPERATIONS[:4], repeat=length)
    ]
    # The single deletion at every position of every three-step sequence.
    sequences += [
        (*sequence[:position], "delete_single", *sequence[position:])
        for sequence in itertools.product(OPERATIONS[:4], repeat=3)
        for position in range(4)
    ]
    for number, sequence in enumerate(sequences):
        world = await build_matrix_world(f"Matrix {number}")
        await check_active(world, ("start", sequence))
        for step, operation in enumerate(sequence):
            await apply(world, operation)
            await check_active(world, (sequence, step))


@pytest.mark.asyncio
@pytest.mark.parametrize("removals", [("remove_run", "delete_code"), ("delete_code", "remove_run")])
@pytest.mark.parametrize(
    "restorations", [("restore_run", "restore_code"), ("restore_code", "restore_run")]
)
@pytest.mark.parametrize("cut", [0, 1, 2])
async def test_active_rule_survives_a_snapshot_round_trip(
    tmp_path, use_data_dir, removals, restorations, cut
):
    """Export after the removals (and after `cut` restorations), import elsewhere, go on."""
    use_data_dir(tmp_path / "source")
    await db_module.init_db()
    world = await build_matrix_world("Round trip")
    for operation in removals:
        await apply(world, operation)
    for operation in restorations[:cut]:
        await apply(world, operation)
    await check_active(world, "before export")
    package, _, _ = await build_aqda_snapshot(world["project"])

    use_data_dir(tmp_path / "target")
    await db_module.init_db()
    imported = (await projects.import_package_bytes(package))["imported"][0]["id"]
    # Find the same codings by their place in the text, code name, and run label.
    copied = {
        (row["start_pos"], row["code_name"], row["label"]): row["id"]
        for row in await rows(
            "SELECT cg.id, cg.start_pos, c.name AS code_name, r.label FROM coding cg "
            "JOIN code c ON c.id=cg.code_id LEFT JOIN coding_run r ON r.id=cg.run_id "
            "JOIN document d ON d.id=cg.document_id WHERE d.project_id=?",
            (imported,),
        )
    }
    source_rows = {}
    for name, (coding_id, _, _) in world["codings"].items():
        source_rows[name] = coding_id
    names = await rows(
        "SELECT id, name FROM code WHERE project_id=?", (imported,)
    )
    code_ids = {row["name"]: row["id"] for row in names}
    run_row = (await rows(
        "SELECT id FROM coding_run WHERE project_id=? AND label='Run A'", (imported,)
    ))[0]
    other_run = (await rows(
        "SELECT id FROM coding_run WHERE project_id=? AND label='Run B'", (imported,)
    ))[0]
    layout = {
        "hand_x": (0, "X", None), "hand_x2": (1, "X2", None), "hand_removed": (2, "X", None),
        "run_replaced": (3, "X", "Run A"), "run_x": (4, "X", "Run A"),
        "run_x2": (5, "X2", "Run A"), "run_y": (6, "Y", "Run A"),
        "run_removed": (7, "X", "Run A"), "other_run_x": (8, "X", "Run B"),
    }
    run_ids = {None: None, "Run A": run_row["id"], "Run B": other_run["id"]}
    world["codings"] = {
        name: (
            copied[(span(index)["start_pos"], code, label)], code_ids[code], run_ids[label]
        )
        for name, (index, code, label) in layout.items()
    }
    world.update(project=imported, x=code_ids["X"], run_id=run_row["id"])
    await check_active(world, "after import")
    for operation in restorations[cut:]:
        await apply(world, operation)
        await check_active(world, ("after import", operation))
    assert not world["run_removed"] and not world["code_trashed"]


# --- AI panel: hand codings only for examples and definitions ----------------


@pytest.mark.asyncio
async def test_suggestions_and_definitions_learn_from_hand_codings_only(world, monkeypatch):
    run = await start_run(world["id"])
    await submit(run, world["doc"], [{"quote": "Taxes are too high.", "code_id": world["policy"]}])
    prompts, embedded = [], []

    async def generate(prompt, model, ollama_url, system="", think=False, task=None):
        prompts.append(prompt)
        return "A definition."

    async def embed(text, model, ollama_url):
        embedded.extend([text] if isinstance(text, str) else text)
        return [1.0, 0.0] if isinstance(text, str) else [[1.0, 0.0] for _ in text]

    monkeypatch.setattr(ai, "_ollama_generate", generate)
    monkeypatch.setattr(ai, "_ollama_embed", embed)
    ids = {"project_id": world["id"], "code_id": world["policy"]}

    only_model = await ai.generate_definition(ai.GenerateDefinitionRequest(**ids, llm_model="m"))
    assert only_model["segment_count"] == 0 and prompts == []

    await codings.create_coding(codings.CodingCreate(
        document_id=world["doc"], code_id=world["policy"],
        start_pos=TEXT.index("The state"), end_pos=TEXT.index(" Eve"),
        selected_text="The state must act – now.",
    ))
    definition = await ai.generate_definition(ai.GenerateDefinitionRequest(**ids, llm_model="m"))
    assert definition["segment_count"] == 1
    assert "The state must act" in prompts[-1] and "Taxes are too high" not in prompts[-1]

    suggestions = await ai.suggest_codings(ai.AutoCodeRequest(**ids, embedding_model="m"))
    query = next(text for text in embedded if text.startswith("Code: Policy"))
    assert "The state must act" in query and "Taxes are too high" not in query
    # The model's passage still counts as already coded.
    taxes = TEXT.index("Taxes")
    assert all(not (s["start_pos"] <= taxes < s["end_pos"]) for s in suggestions)

    # Summaries still read everything under the code.
    summary = await ai.summarize_code(ai.SummarizeCodeRequest(**ids, llm_model="m"))
    assert summary["total_segments"] == 2


# --- Shared-folder sync waits for a run in progress -------------------------


async def writer_files(folder) -> dict:
    return {
        path.name: path.stat().st_mtime_ns for path in (folder / "snapshots").glob("*.aqda")
    }


@pytest.mark.asyncio
async def test_sync_neither_publishes_nor_applies_while_a_run_is_in_progress(
    tmp_path, use_data_dir
):
    from pathlib import Path

    from aqda.services.coding_runs import RUN_PAUSE_PREFIX
    from aqda.services.shared_projects import (
        open_shared_project,
        set_shared_root,
        share_project,
        sync_project,
    )

    shared_root = tmp_path / "Drive"
    alice_data = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    alice = await make_project("Shared survey")
    folder = Path((await share_project(alice["id"]))["folder"])

    bob_data = use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    bob_id = (await open_shared_project(str(folder)))["project_id"]

    # Alice's program starts a run and codes a document: nothing is published meanwhile.
    use_data_dir(alice_data)
    run = await start_run(alice["id"], label="Night run")
    await submit(run, alice["doc"], [{"quote": "Taxes", "code_id": alice["policy"]}])
    published = await writer_files(folder)
    result = await sync_project(alice["id"])
    assert result["paused_for_run"] == "Night run"
    assert await writer_files(folder) == published
    status = (await rows("SELECT shared_sync_error FROM project WHERE id=?", (alice["id"],)))
    assert status[0]["shared_sync_error"].startswith(RUN_PAUSE_PREFIX)
    assert "Night run" in status[0]["shared_sync_error"]

    # Finishing the run lets the next pass publish and clears the message.
    await runs.finish_run(run["uuid"])
    result = await sync_project(alice["id"])
    assert result["published"] and result["published"]["published"]
    assert await writer_files(folder) != published
    status = (await rows("SELECT shared_sync_error FROM project WHERE id=?", (alice["id"],)))
    assert status[0]["shared_sync_error"] is None

    # Bob takes Alice's version, then edits while Alice has another run going.
    use_data_dir(bob_data)
    await sync_project(bob_id)
    use_data_dir(alice_data)
    second = await start_run(alice["id"], label="Second run")
    await sync_project(alice["id"])  # paused: Bob would see a half-finished run otherwise
    await execute(
        "UPDATE coding_run SET status='finished', last_activity_at=datetime('now') WHERE id=?",
        (second["id"],),
    )
    await sync_project(alice["id"])  # publishes the finished run
    use_data_dir(bob_data)
    await sync_project(bob_id)
    await projects.update_project(bob_id, projects.ProjectUpdate(description="Bob's note"))
    await sync_project(bob_id)

    # A running run on Alice's side (not touching her project) keeps Bob's update out.
    use_data_dir(alice_data)
    await execute(
        "INSERT INTO coding_run (project_id, uuid, label, status, last_activity_at) "
        "VALUES (?, ?, 'Third run', 'running', datetime('now'))",
        (alice["id"], str(uuid.uuid4())),
    )
    result = await sync_project(alice["id"])
    assert result["paused_for_run"] == "Third run"
    project = (await rows("SELECT * FROM project WHERE id=?", (alice["id"],)))[0]
    assert project["description"] != "Bob's note"

    # A run that went quiet for longer than the window no longer blocks sync.
    await execute(
        "UPDATE coding_run SET last_activity_at=datetime('now', '-31 minutes') "
        "WHERE label='Third run'"
    )
    result = await sync_project(alice["id"])
    assert "paused_for_run" not in result
    project = (await rows("SELECT * FROM project WHERE id=?", (alice["id"],)))[0]
    assert project["description"] == "Bob's note"
    assert not (project["shared_sync_error"] or "").startswith(RUN_PAUSE_PREFIX)


@pytest.mark.asyncio
async def test_removed_or_stale_runs_do_not_pause_sync(world):
    from aqda.services.coding_runs import run_in_progress_label

    run = await start_run(world["id"], label="Quick")
    db = await db_module.get_db()
    try:
        assert await run_in_progress_label(db, world["id"]) == "Quick"
        await runs.remove_run(run["uuid"])
        assert await run_in_progress_label(db, world["id"]) is None
        await runs.restore_run(run["uuid"])
        await execute("UPDATE coding_run SET status='running' WHERE id=?", (run["id"],))
        assert await run_in_progress_label(db, world["id"]) == "Quick"
        await execute(
            "UPDATE coding_run SET last_activity_at=datetime('now', '-31 minutes') WHERE id=?",
            (run["id"],),
        )
        assert await run_in_progress_label(db, world["id"]) is None
    finally:
        await db.close()


# --- Code trash counts what a restore brings back ---------------------------


@pytest.mark.asyncio
async def test_code_trash_counts_only_codings_a_restore_brings_back(world):
    run = await start_run(world["id"])
    await submit(run, world["doc"], [{"quote": "Taxes are too high.", "code_id": world["policy"]}])
    await codings.create_coding(codings.CodingCreate(
        document_id=world["doc"], code_id=world["policy"],
        start_pos=TEXT.index("The state"), end_pos=TEXT.index(" Eve"),
        selected_text="The state must act – now.",
    ))
    await codes.delete_code(world["policy"])
    [entry] = await codes.list_deleted_codes(world["id"])
    assert entry["coding_count"] == 2

    # With the run removed, restoring the code brings back the hand coding only.
    await runs.remove_run(run["uuid"])
    [entry] = await codes.list_deleted_codes(world["id"])
    assert entry["coding_count"] == 1
    await codes.restore_code(world["policy"])
    assert len(await codings.list_codings(code_id=world["policy"])) == 1


# --- Passages given by position need the text's fingerprint ------------------


@pytest.mark.asyncio
async def test_position_without_quote_needs_the_text_hash(world):
    run = await start_run(world["id"])
    passage = {"start_pos": TEXT.index("Taxes"), "end_pos": len(TEXT), "code_id": world["policy"]}
    with_quote = {**passage, "quote": "Taxes are too high."}
    result = await submit(run, world["doc"], [passage, with_quote])
    assert [item["index"] for item in result["accepted"]] == [1]
    [refused] = result["refused"]
    assert refused["index"] == 0 and refused["quote"] is None
    assert "text_hash" in refused["reason"] and "quote" in refused["reason"]

    current = (await documents.get_document_text(world["doc"]))["text_hash"]
    result = await submit(run, world["doc"], [passage], text_hash=current)
    assert [item["method"] for item in result["accepted"]] == ["position"]


# --- Sharing, resolving, and replacing imports wait for a run too -------------


async def running_run(project_id: int, label: str) -> int:
    return await execute(
        "INSERT INTO coding_run (project_id, uuid, label, status, last_activity_at) "
        "VALUES (?, ?, ?, 'running', datetime('now'))",
        (project_id, str(uuid.uuid4()), label),
    )


async def end_run(run_id: int) -> None:
    await execute("UPDATE coding_run SET status='stopped' WHERE id=?", (run_id,))


def refused_for_run(error, label: str) -> bool:
    detail = error.value.detail
    return (
        error.value.status_code == 409
        and f"model run “{label}”" in detail
        and "finish or be stopped first" in detail
    )


@pytest.mark.asyncio
async def test_sharing_waits_for_a_run_in_progress(tmp_path, use_data_dir):
    from aqda.services.shared_projects import set_shared_root, share_project

    use_data_dir(tmp_path / "share")
    await db_module.init_db()
    await set_shared_root(str(tmp_path / "Drive"))
    project = await make_project("To share")
    run_id = await running_run(project["id"], "Night run")
    with pytest.raises(HTTPException) as refused:
        await share_project(project["id"])
    assert refused_for_run(refused, "Night run")
    assert (await rows("SELECT shared_folder FROM project WHERE id=?", (project["id"],)))[0][
        "shared_folder"
    ] is None
    await end_run(run_id)
    assert (await share_project(project["id"]))["published"]


@pytest.mark.asyncio
async def test_imports_that_replace_a_project_wait_for_a_run(tmp_path, use_data_dir):
    alice = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    project = await make_project("Imported")
    lineage = (await projects.get_project(project["id"]))["lineage_id"]
    package, _, _ = await build_aqda_snapshot(project["id"])

    # A collaborator's newer version of the same project.
    use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    bob_id = (await projects.import_package_bytes(package))["imported"][0]["id"]
    await projects.update_project(bob_id, projects.ProjectUpdate(description="Bob's note"))
    newer, _, _ = await build_aqda_snapshot(bob_id)

    use_data_dir(alice)
    run_id = await running_run(project["id"], "Night run")
    before = await revision(project["id"])
    for mode in ("auto", "replace"):
        with pytest.raises(HTTPException) as refused:
            await projects.import_package_bytes(newer, mode=mode, target_lineage_id=lineage)
        assert refused_for_run(refused, "Night run")
    assert await revision(project["id"]) == before
    assert not list((alice / "backups").glob("aqda-before-shared-update-*.db"))

    # A new project from the same file is fine; so is the update once the run has ended.
    copy = await projects.import_package_bytes(newer, mode="copy", target_lineage_id=lineage)
    assert copy["imported"][0]["action"] == "copy"
    await end_run(run_id)
    updated = await projects.import_package_bytes(newer)
    assert updated["imported"][0]["action"] == "update"

    # Refreshing a reference copy that has a run going is refused the same way.
    copy_id = copy["imported"][0]["id"]
    run_id = await running_run(copy_id, "Copy run")
    with pytest.raises(HTTPException) as refused:
        await projects.refresh_conflict_copy(newer, copy_id, lineage)
    assert refused_for_run(refused, "Copy run")
    await end_run(run_id)
    assert (await projects.refresh_conflict_copy(newer, copy_id, lineage))["id"] == copy_id


@pytest.mark.asyncio
async def test_resolving_a_concurrent_version_waits_for_a_run(tmp_path, use_data_dir):
    from aqda.services.shared_projects import resolve_conflict_copy

    use_data_dir(tmp_path / "resolve")
    await db_module.init_db()
    original = await make_project("Original")
    mirror = await make_project("Reference copy")
    await execute(
        "INSERT INTO shared_conflict_branch (project_id, source_lineage_id, "
        "anchor_snapshot_id, latest_snapshot_id, latest_snapshot_path, conflict_project_id, "
        "conflict_base_revision) VALUES (?, 'lineage', 'head', 'head', '/nowhere', ?, 0)",
        (original["id"], mirror["id"]),
    )
    for project_id, label in ((original["id"], "In the original"), (mirror["id"], "In the copy")):
        run_id = await running_run(project_id, label)
        for choice in ("use_reference", "keep_current"):
            with pytest.raises(HTTPException) as refused:
                await resolve_conflict_copy(mirror["id"], choice)
            assert refused_for_run(refused, label)
        await end_run(run_id)
    branch = await rows("SELECT status FROM shared_conflict_branch")
    assert branch == [{"status": "active"}]
    assert (await rows("SELECT deleted_at FROM project WHERE id=?", (mirror["id"],)))[0][
        "deleted_at"
    ] is None


@pytest.mark.asyncio
async def test_background_sync_checks_for_a_run_again_before_publishing(
    tmp_path, use_data_dir, monkeypatch
):
    from pathlib import Path

    from aqda.services import shared_projects as shared_service

    use_data_dir(tmp_path / "late")
    await db_module.init_db()
    await shared_service.set_shared_root(str(tmp_path / "Drive"))
    project = await make_project("Late run")
    folder = Path((await shared_service.share_project(project["id"]))["folder"])
    await projects.update_project(project["id"], projects.ProjectUpdate(description="Edited"))
    published = await writer_files(folder)

    # No run at the start of the pass; a program starts one before the publishing step.
    answers = iter([None, "Late run"])
    monkeypatch.setattr(shared_service, "_run_in_progress", lambda _: _async(next(answers)))
    result = await shared_service.sync_project(project["id"])
    assert result["paused_for_run"] == "Late run" and result["published"] is None
    assert await writer_files(folder) == published
    status = await rows("SELECT shared_sync_error FROM project WHERE id=?", (project["id"],))
    assert "Late run" in status[0]["shared_sync_error"]


async def _async(value):
    return value
