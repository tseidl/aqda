import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

import aqda.db as db_module
from aqda.routers import projects, shared as shared_router
from aqda.routers.export import build_aqda_snapshot
from aqda.services import shared_projects as shared_service
from aqda.services.shared_projects import (
    discover_shared_projects,
    open_shared_project,
    resolve_conflict_copy,
    set_shared_root,
    share_project,
    sync_project,
    unlink_shared_project,
)


async def project_row(project_id: int):
    db = await db_module.get_db()
    try:
        return await (
            await db.execute("SELECT * FROM project WHERE id=?", (project_id,))
        ).fetchone()
    finally:
        await db.close()


async def project_count() -> int:
    db = await db_module.get_db()
    try:
        return (await (await db.execute("SELECT COUNT(*) FROM project")).fetchone())[0]
    finally:
        await db.close()


async def project_rows():
    db = await db_module.get_db()
    try:
        return await (await db.execute("SELECT * FROM project ORDER BY id")).fetchall()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_shared_folder_opens_once_then_syncs_both_directions(tmp_path, use_data_dir):
    shared_root = tmp_path / "Google Drive" / "AQDA"

    alice_data = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    alice = await projects.create_project(projects.ProjectCreate(name="Shared interviews"))
    shared = await share_project(alice["id"])
    folder = Path(shared["folder"])
    assert len(list((folder / "snapshots").glob("*.aqda"))) == 1

    use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    discovered = await discover_shared_projects()
    assert discovered[0]["name"] == "Shared interviews"
    opened = await open_shared_project(discovered[0]["folder"])
    bob_id = opened["project_id"]
    await projects.update_project(
        bob_id, projects.ProjectUpdate(description="Bob coded the second interview")
    )
    await sync_project(bob_id)
    assert len(list((folder / "snapshots").glob("*.aqda"))) == 2

    use_data_dir(alice_data)
    await sync_project(alice["id"])
    assert (await project_row(alice["id"]))["description"] == "Bob coded the second interview"

    use_data_dir(tmp_path / "bob")
    await unlink_shared_project(bob_id)
    assert (await project_row(bob_id))["shared_folder"] is None
    assert len(list((folder / "snapshots").glob("*.aqda"))) == 1

    resumed = await share_project(bob_id)
    assert resumed["folder"] == str(folder)
    assert len(list(shared_root.glob("*.aqda-project"))) == 1
    assert len(list((folder / "snapshots").glob("*.aqda"))) == 2


@pytest.mark.asyncio
async def test_existing_local_changes_require_choice_before_connecting(
    tmp_path, use_data_dir
):
    shared_root = tmp_path / "Drive" / "coding"

    alice_data = use_data_dir(tmp_path / "alice-existing")
    await db_module.init_db()
    alice = await projects.create_project(projects.ProjectCreate(name="Existing study"))
    baseline, _, _ = await build_aqda_snapshot(alice["id"])

    bob_data = use_data_dir(tmp_path / "bob-existing")
    await db_module.init_db()
    imported = await projects.import_package_bytes(baseline)
    bob_id = imported["imported"][0]["id"]
    await projects.update_project(
        bob_id, projects.ProjectUpdate(description="Minor local test coding")
    )

    use_data_dir(alice_data)
    await set_shared_root(str(shared_root))
    (shared_root / "Existing study.aqda").write_bytes(baseline)
    folder = Path((await share_project(alice["id"]))["folder"])

    use_data_dir(bob_data)
    await set_shared_root(str(shared_root))
    assert (await shared_router.shared_status())["standalone_aqda_count"] == 1
    discovered = await discover_shared_projects()
    assert discovered[0]["local_project_id"] == bob_id
    assert discovered[0]["linked_project_id"] is None

    needs_choice = await open_shared_project(str(folder))
    assert needs_choice["needs_local_newer_choice"] is True
    assert needs_choice["local_relation"] == "newer"
    assert (await project_row(bob_id))["description"] == "Minor local test coding"
    assert (await project_row(bob_id))["shared_folder"] is None

    connected = await open_shared_project(str(folder), "use_shared")
    assert connected["needs_local_newer_choice"] is False
    assert connected["backup_path"]
    assert (await project_row(bob_id))["description"] == ""
    assert (await project_row(bob_id))["shared_folder"] == str(folder)
    assert await project_count() == 1
    assert len(list((folder / "snapshots").glob("*.aqda"))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", ["use_shared", "use_local"])
async def test_divergent_local_copy_requires_choice_before_connecting(
    tmp_path, use_data_dir, choice
):
    shared_root = tmp_path / "Drive" / "divergent-study"

    use_data_dir(tmp_path / "alice-divergent-open")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    alice = await projects.create_project(projects.ProjectCreate(name="Divergent study"))
    folder = Path((await share_project(alice["id"]))["folder"])
    baseline = next((folder / "snapshots").glob("*.aqda")).read_bytes()
    await projects.update_project(
        alice["id"], projects.ProjectUpdate(description="Alice changed the shared copy")
    )
    await sync_project(alice["id"])

    use_data_dir(tmp_path / "bob-divergent-open")
    await db_module.init_db()
    imported = await projects.import_package_bytes(baseline)
    bob_id = imported["imported"][0]["id"]
    await projects.update_project(
        bob_id, projects.ProjectUpdate(description="Bob independently changed his copy")
    )
    await set_shared_root(str(shared_root))

    writer_count = len(list((folder / "snapshots").glob("*.aqda")))
    needs_choice = await open_shared_project(str(folder))
    assert needs_choice["needs_local_newer_choice"] is True
    assert needs_choice["local_relation"] == "divergent"
    assert (await project_row(bob_id))["description"] == "Bob independently changed his copy"
    assert (await project_row(bob_id))["shared_folder"] is None
    assert len(list((folder / "snapshots").glob("*.aqda"))) == writer_count

    connected = await open_shared_project(str(folder), choice)
    assert (await project_row(bob_id))["shared_folder"] == str(folder)
    if choice == "use_shared":
        assert connected["backup_path"]
        assert (await project_row(bob_id))["description"] == "Alice changed the shared copy"
        assert await project_count() == 1
    else:
        assert connected["conflicts"]
        assert (await project_row(bob_id))["description"] == (
            "Bob independently changed his copy"
        )
        assert await project_count() == 2
        mirror = next(row for row in await project_rows() if row["id"] != bob_id)
        assert mirror["description"] == "Alice changed the shared copy"
        assert mirror["shared_folder"] is None


@pytest.mark.asyncio
async def test_projects_choose_between_multiple_collaboration_locations(
    tmp_path, use_data_dir
):
    use_data_dir(tmp_path / "many-teams")
    await db_module.init_db()
    drive_team = tmp_path / "Google Drive" / "Policy team"
    university_team = tmp_path / "University Cloud" / "Methods team"
    await set_shared_root(str(drive_team))
    await set_shared_root(str(university_team))

    policy = await projects.create_project(projects.ProjectCreate(name="Policy study"))
    methods = await projects.create_project(projects.ProjectCreate(name="Methods study"))

    with pytest.raises(HTTPException, match="Choose which collaboration location"):
        await share_project(policy["id"])

    policy_shared = await share_project(policy["id"], str(drive_team))
    methods_shared = await share_project(methods["id"], str(university_team))
    assert Path(policy_shared["folder"]).parent == drive_team
    assert Path(methods_shared["folder"]).parent == university_team
    with pytest.raises(HTTPException, match="already collaborating"):
        await share_project(policy["id"], str(university_team))

    metadata = json.loads(
        (Path(policy_shared["folder"]) / "project.json").read_text(encoding="utf-8")
    )
    assert metadata["name"] == "Policy study"
    assert not list(Path(policy_shared["folder"]).glob(".project.*.tmp"))

    status = await shared_router.shared_status()
    roots = {item["path"]: item for item in status["roots"]}
    assert set(roots) == {str(drive_team), str(university_team)}
    assert roots[str(drive_team)]["project_count"] == 1
    assert roots[str(drive_team)]["linked_project_count"] == 1
    assert roots[str(university_team)]["project_count"] == 1
    assert roots[str(university_team)]["linked_project_count"] == 1
    assert {item["name"] for item in status["discovered"]} == {
        "Policy study",
        "Methods study",
    }

    duplicate_folder = university_team / "Policy duplicate.aqda-project"
    duplicate_folder.mkdir()
    (duplicate_folder / "snapshots").mkdir()
    source_snapshot = next(
        (Path(policy_shared["folder"]) / "snapshots").glob("*.aqda")
    )
    (duplicate_folder / "snapshots" / source_snapshot.name).write_bytes(
        source_snapshot.read_bytes()
    )
    duplicate = next(
        item
        for item in await discover_shared_projects()
        if item["folder"] == str(duplicate_folder)
    )
    assert duplicate["local_project_id"] == policy["id"]
    assert duplicate["linked_project_id"] is None
    with pytest.raises(HTTPException, match="already collaborating"):
        await open_shared_project(str(duplicate_folder))


@pytest.mark.asyncio
async def test_simultaneous_shared_edits_keep_both_versions(tmp_path, use_data_dir):
    shared_root = tmp_path / "Drive"

    alice_data = use_data_dir(tmp_path / "alice")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    alice = await projects.create_project(projects.ProjectCreate(name="Parallel study"))
    folder = Path((await share_project(alice["id"]))["folder"])

    bob_data = use_data_dir(tmp_path / "bob")
    await db_module.init_db()
    await set_shared_root(str(shared_root))
    bob = await open_shared_project(str(folder))

    use_data_dir(alice_data)
    await projects.update_project(
        alice["id"], projects.ProjectUpdate(description="Alice branch")
    )
    await sync_project(alice["id"])

    use_data_dir(bob_data)
    await projects.update_project(
        bob["project_id"], projects.ProjectUpdate(description="Bob branch")
    )
    result = await sync_project(bob["project_id"])
    assert len(result["conflicts"]) == 1
    assert await project_count() == 2
    original = await project_row(bob["project_id"])
    assert original["description"] == "Bob branch"
    assert "single local reference copy" in original["shared_sync_error"]
    assert len(list(shared_root.glob("*.aqda-project"))) == 1

    # One collaborator can keep publishing while the other remains divergent.
    # The existing reference copy advances; neither local projects nor shared
    # project folders multiply on every new remote head.
    for version in range(2, 6):
        use_data_dir(alice_data)
        await projects.update_project(
            alice["id"], projects.ProjectUpdate(description=f"Alice branch {version}")
        )
        await sync_project(alice["id"])

        use_data_dir(bob_data)
        await sync_project(bob["project_id"])
        assert await project_count() == 2
        assert len(list(shared_root.glob("*.aqda-project"))) == 1

    rows = await project_rows()
    mirror = next(row for row in rows if row["id"] != bob["project_id"])
    assert mirror["description"] == "Alice branch 5"
    assert mirror["shared_folder"] is None
    assert "updates automatically" in mirror["shared_sync_error"]
    assert (await projects.get_project(mirror["id"]))["is_conflict_mirror"] == 1
    with pytest.raises(HTTPException, match="cannot be shared separately"):
        await share_project(mirror["id"])

    resolved = await resolve_conflict_copy(mirror["id"], "use_reference")
    assert resolved["project_id"] == bob["project_id"]
    assert resolved["backup_path"]
    assert "previous local version" in resolved["archived_project_name"]
    assert (await project_row(bob["project_id"]))["description"] == "Alice branch 5"
    archive = await project_row(resolved["archived_project_id"])
    assert archive["description"] == "Bob branch"
    assert archive["shared_folder"] is None
    assert await project_count() == 2
    assert len(list(shared_root.glob("*.aqda-project"))) == 1
    for writer in (folder / "snapshots").glob("*.aqda"):
        snapshot = sqlite3.connect(writer)
        try:
            assert snapshot.execute("SELECT description FROM project").fetchone()[0] == (
                "Alice branch 5"
            )
        finally:
            snapshot.close()

    # The collaborator whose current branch won dismisses the other reference
    # locally. Both computers now continue from Alice's same snapshot history.
    use_data_dir(alice_data)
    alice_rows = await project_rows()
    alice_mirror = next(row for row in alice_rows if row["id"] != alice["id"])
    kept = await resolve_conflict_copy(alice_mirror["id"], "keep_current")
    assert kept["project_id"] == alice["id"]
    assert (await project_row(alice_mirror["id"]))["deleted_at"] is not None
    assert (await project_row(alice["id"]))["shared_sync_error"] is None


@pytest.mark.asyncio
async def test_background_sync_retries_after_transient_failure(
    tmp_path, use_data_dir, monkeypatch
):
    use_data_dir(tmp_path / "resilient-sync")
    await db_module.init_db()
    stop_event = asyncio.Event()
    attempts = 0

    async def flaky_sync():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database is locked")
        stop_event.set()
        return []

    monkeypatch.setattr(shared_service, "_stop_event", stop_event)
    monkeypatch.setattr(shared_service, "SYNC_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(shared_service, "sync_all_shared_projects", flaky_sync)
    monkeypatch.setattr(shared_service, "create_daily_backup", lambda: None)
    monkeypatch.setattr(shared_service, "_last_daily_backup_day", None)

    await asyncio.wait_for(shared_service._sync_loop(), timeout=1)

    assert attempts == 2
    health = shared_service.get_sync_health()
    assert health["sync_error"] is None
    assert health["last_checked_at"] is not None
