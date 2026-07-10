import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

import aqda.db as db_module
from aqda.routers import projects
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
