from pathlib import Path

import pytest

import aqda.db as db_module
from aqda.routers import projects
from aqda.services.shared_projects import (
    discover_shared_projects,
    open_shared_project,
    set_shared_root,
    share_project,
    sync_project,
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
    assert "kept both versions" in original["shared_sync_error"]
    assert len(list(shared_root.glob("*.aqda-project"))) == 2
