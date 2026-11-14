"""Code colour schemes are listed in three places; they must say the same."""

import re
from pathlib import Path

import pytest

import aqda.db as db_module
from aqda.routers import codes, projects
from aqda.services.coding_runs import CODE_COLOR_SCHEMES, next_code_color

FRONTEND = Path(__file__).resolve().parents[1] / "aqda" / "frontend" / "src"
HEX_LIST = r"\[((?:\s*'#[0-9a-fA-F]{6}'\s*,?)+)\]"


def hex_colors(text: str) -> list[str]:
    return [color.lower() for color in re.findall(r"#[0-9a-fA-F]{6}", text)]


def backend_schemes() -> dict[str, list[str]]:
    return {name: [c.lower() for c in colors] for name, colors in CODE_COLOR_SCHEMES.items()}


def test_code_tree_lists_the_same_schemes():
    source = (FRONTEND / "components" / "CodeTree.tsx").read_text(encoding="utf-8")
    block = source.split("const COLOR_SCHEMES", 1)[1].split("};", 1)[0]
    found = {
        (quoted or bare): hex_colors(colors)
        for quoted, bare, colors in re.findall(rf"(?:'([^']+)'|(\w+)):\s*{HEX_LIST}", block)
    }
    assert found == backend_schemes()


def test_settings_page_lists_the_same_schemes():
    source = (FRONTEND / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    found = {
        name: hex_colors(colors)
        for name, colors in re.findall(rf"name:\s*'([^']+)',\s*colors:\s*{HEX_LIST}", source)
    }
    assert found == backend_schemes()


@pytest.mark.asyncio
async def test_next_code_color_follows_the_chosen_scheme(tmp_path, use_data_dir):
    use_data_dir(tmp_path / "data")
    await db_module.init_db()
    project = await projects.create_project(projects.ProjectCreate(name="Colours"))
    db = await db_module.get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES "
            "('color_scheme', 'Color-blind safe')"
        )
        await db.commit()
        scheme = CODE_COLOR_SCHEMES["Color-blind safe"]
        assert len(scheme) == len(set(scheme)) == 8
        assert await next_code_color(db, project["id"]) == scheme[0]
        await codes.create_code(codes.CodeCreate(project_id=project["id"], name="First"))
        assert await next_code_color(db, project["id"]) == scheme[1]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_new_installation_has_no_stored_scheme(tmp_path, use_data_dir):
    """Without a stored choice the "Default" scheme applies; the other schemes are opt-in."""
    use_data_dir(tmp_path / "new")
    await db_module.init_db()
    db = await db_module.get_db()
    try:
        row = await (
            await db.execute("SELECT value FROM setting WHERE key='color_scheme'")
        ).fetchone()
        assert row is None
        project = await projects.create_project(projects.ProjectCreate(name="Plain"))
        assert await next_code_color(db, project["id"]) == CODE_COLOR_SCHEMES["Default"][0]
    finally:
        await db.close()
