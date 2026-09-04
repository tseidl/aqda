"""Database setup and connection management using aiosqlite."""

import aiosqlite
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("AQDA_DATA_DIR", Path.home() / ".aqda"))

_SCHEMA_TEMPLATE = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    modified_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT DEFAULT NULL,
    lineage_id TEXT DEFAULT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    head_snapshot_id TEXT DEFAULT NULL,
    shared_folder TEXT DEFAULT NULL,
    shared_previous_folder TEXT DEFAULT NULL,
    shared_last_published_revision INTEGER DEFAULT NULL,
    shared_last_snapshot_id TEXT DEFAULT NULL,
    shared_last_sync_at TEXT DEFAULT NULL,
    shared_sync_error TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS document (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT DEFAULT 'text',
    transcript TEXT DEFAULT NULL,
    label TEXT DEFAULT '',
    exclude_from_ai INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    modified_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS code (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES code(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    color TEXT DEFAULT '#6366f1',
    sort_order INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT DEFAULT NULL,
    deletion_batch_id TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS coding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    code_id INTEGER NOT NULL REFERENCES code(id) ON DELETE CASCADE,
    start_pos INTEGER NOT NULL,
    end_pos INTEGER NOT NULL,
    selected_text TEXT NOT NULL,
    coder TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT DEFAULT NULL,
    deletion_batch_id TEXT DEFAULT NULL,
    offset_unit TEXT NOT NULL DEFAULT 'codepoint',
    repair_status TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS memo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id) ON DELETE CASCADE,
    code_id INTEGER REFERENCES code(id) ON DELETE SET NULL,
    coding_id INTEGER REFERENCES coding(id) ON DELETE SET NULL,
    start_pos INTEGER DEFAULT NULL,
    end_pos INTEGER DEFAULT NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    modified_at TEXT DEFAULT (datetime('now')),
    offset_unit TEXT NOT NULL DEFAULT 'codepoint',
    repair_status TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS document_variable (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT DEFAULT '',
    UNIQUE(document_id, key)
);

CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Default settings
INSERT OR IGNORE INTO setting (key, value) VALUES
    ('ollama_url', 'http://localhost:11434'),
    ('llm_model', ''),
    ('embedding_model', ''),
    ('chunk_size', '500'),
    ('chunk_overlap', '50'),
    ('filename_pattern', ''),
    ('filename_variables', ''),
    ('whisper_model', 'base'),
    ('coder_name', ''),
    ('shared_folder', ''),
    ('shared_folders', '[]'),
    ('device_id', ''),
    ('shared_update_backups', '10'),
    ('chunk_mode', 'fixed'),
    ('schema_version', '__SCHEMA_VERSION__');

CREATE TABLE IF NOT EXISTS project_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    parent_snapshot_id TEXT DEFAULT NULL,
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_snapshot_project ON project_snapshot(project_id, created_at);

CREATE TABLE IF NOT EXISTS code_deletion_batch (
    id TEXT PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    root_code_id INTEGER NOT NULL,
    deleted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shared_ignored_head (
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    snapshot_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, snapshot_id)
);

CREATE TABLE IF NOT EXISTS shared_conflict_branch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    source_lineage_id TEXT NOT NULL,
    anchor_snapshot_id TEXT NOT NULL,
    latest_snapshot_id TEXT NOT NULL,
    latest_snapshot_path TEXT NOT NULL,
    conflict_project_id INTEGER REFERENCES project(id) ON DELETE SET NULL,
    conflict_base_revision INTEGER DEFAULT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(project_id, anchor_snapshot_id)
);

CREATE TABLE IF NOT EXISTS embedding_cache (
    id TEXT PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    start_pos INTEGER NOT NULL,
    end_pos INTEGER NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_embedding_project ON embedding_cache(project_id, model);
CREATE INDEX IF NOT EXISTS idx_embedding_document ON embedding_cache(document_id, model);
"""

# Migrations keyed by target version. Each runs if schema_version < target.
MIGRATIONS = {
    2: [
        "ALTER TABLE code ADD COLUMN deleted_at TEXT DEFAULT NULL",
        "ALTER TABLE coding ADD COLUMN deleted_at TEXT DEFAULT NULL",
    ],
    3: [
        """CREATE TABLE IF NOT EXISTS embedding_cache (
            id TEXT PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
            project_id INTEGER NOT NULL,
            model TEXT NOT NULL,
            start_pos INTEGER NOT NULL,
            end_pos INTEGER NOT NULL,
            chunk_text TEXT NOT NULL,
            embedding BLOB NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_embedding_project ON embedding_cache(project_id, model)",
        "CREATE INDEX IF NOT EXISTS idx_embedding_document ON embedding_cache(document_id, model)",
    ],
    4: [
        "ALTER TABLE project ADD COLUMN deleted_at TEXT DEFAULT NULL",
    ],
    5: [
        "ALTER TABLE document ADD COLUMN transcript TEXT DEFAULT NULL",
    ],
    6: [
        "ALTER TABLE document ADD COLUMN label TEXT DEFAULT ''",
        "ALTER TABLE document ADD COLUMN exclude_from_ai INTEGER DEFAULT 0",
        "ALTER TABLE memo ADD COLUMN start_pos INTEGER DEFAULT NULL",
        "ALTER TABLE memo ADD COLUMN end_pos INTEGER DEFAULT NULL",
        "INSERT OR IGNORE INTO setting (key, value) VALUES ('coder_name', '')",
    ],
    7: [
        "ALTER TABLE coding ADD COLUMN coder TEXT DEFAULT ''",
    ],
    8: [
        "ALTER TABLE project ADD COLUMN lineage_id TEXT DEFAULT NULL",
        "ALTER TABLE project ADD COLUMN revision INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE project ADD COLUMN head_snapshot_id TEXT DEFAULT NULL",
        """CREATE TABLE IF NOT EXISTS project_snapshot (
            snapshot_id TEXT PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            parent_snapshot_id TEXT DEFAULT NULL,
            revision INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            created_by TEXT DEFAULT ''
        )""",
        "CREATE INDEX IF NOT EXISTS idx_snapshot_project ON project_snapshot(project_id, created_at)",
    ],
    9: [
        "ALTER TABLE project ADD COLUMN shared_folder TEXT DEFAULT NULL",
        "ALTER TABLE project ADD COLUMN shared_last_published_revision INTEGER DEFAULT NULL",
        "ALTER TABLE project ADD COLUMN shared_last_snapshot_id TEXT DEFAULT NULL",
        "ALTER TABLE project ADD COLUMN shared_last_sync_at TEXT DEFAULT NULL",
        "ALTER TABLE project ADD COLUMN shared_sync_error TEXT DEFAULT NULL",
        "ALTER TABLE code ADD COLUMN deletion_batch_id TEXT DEFAULT NULL",
        "ALTER TABLE coding ADD COLUMN deletion_batch_id TEXT DEFAULT NULL",
        "ALTER TABLE coding ADD COLUMN offset_unit TEXT NOT NULL DEFAULT 'legacy_utf16'",
        "ALTER TABLE coding ADD COLUMN repair_status TEXT DEFAULT NULL",
        "ALTER TABLE memo ADD COLUMN offset_unit TEXT NOT NULL DEFAULT 'legacy_utf16'",
        "ALTER TABLE memo ADD COLUMN repair_status TEXT DEFAULT NULL",
        "INSERT OR IGNORE INTO setting (key, value) VALUES ('shared_folder', '')",
        "INSERT OR IGNORE INTO setting (key, value) VALUES ('device_id', '')",
        """CREATE TABLE IF NOT EXISTS code_deletion_batch (
            id TEXT PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            root_code_id INTEGER NOT NULL,
            deleted_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS shared_ignored_head (
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            snapshot_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (project_id, snapshot_id)
        )""",
    ],
    10: [
        """CREATE TABLE IF NOT EXISTS shared_conflict_branch (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
            source_lineage_id TEXT NOT NULL,
            anchor_snapshot_id TEXT NOT NULL,
            latest_snapshot_id TEXT NOT NULL,
            latest_snapshot_path TEXT NOT NULL,
            conflict_project_id INTEGER REFERENCES project(id) ON DELETE SET NULL,
            conflict_base_revision INTEGER DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(project_id, anchor_snapshot_id)
        )""",
    ],
    11: [
        "ALTER TABLE project ADD COLUMN shared_previous_folder TEXT DEFAULT NULL",
        "INSERT OR IGNORE INTO setting (key, value) VALUES ('shared_folders', '[]')",
    ],
}

LATEST_SCHEMA_VERSION = max(MIGRATIONS)
# A fresh database is created at the latest version; deriving it here keeps the
# migration table the single source of truth.
SCHEMA = _SCHEMA_TEMPLATE.replace("__SCHEMA_VERSION__", str(LATEST_SCHEMA_VERSION))

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


def _db_path(project_id: int | None = None) -> Path:
    """Get path to the main database."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "aqda.db"


async def get_db() -> aiosqlite.Connection:
    """Get a database connection."""
    db = await aiosqlite.connect(_db_path(), timeout=15)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA busy_timeout=15000")
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def _run_migrations(db: aiosqlite.Connection):
    """Apply pending schema migrations."""
    cursor = await db.execute(
        "SELECT value FROM setting WHERE key='schema_version'"
    )
    row = await cursor.fetchone()
    current = int(row["value"]) if row else 1

    for target in sorted(MIGRATIONS.keys()):
        if current >= target:
            continue
        for sql in MIGRATIONS[target]:
            try:
                await db.execute(sql)
            except Exception as e:
                # An ALTER that re-adds an existing column is expected (the fresh
                # schema already has it); anything else is a real failure we must
                # not hide, or we'd silently bump the version on a broken DB.
                if "duplicate column name" not in str(e).lower():
                    raise
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('schema_version', ?)",
            (str(target),),
        )
        await db.commit()

    # Lineage IDs are application-generated UUIDs. Populate legacy rows after
    # migration 8 rather than relying on a SQLite expression as a column default.
    cursor = await db.execute("SELECT id FROM project WHERE lineage_id IS NULL OR lineage_id='' ")
    for row in await cursor.fetchall():
        await db.execute(
            "UPDATE project SET lineage_id=? WHERE id=?",
            (str(uuid.uuid4()), row["id"]),
        )
    await db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_project_lineage ON project(lineage_id)"
    )
    cursor = await db.execute("SELECT value FROM setting WHERE key='device_id'")
    device = await cursor.fetchone()
    if not device or not device["value"].strip():
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('device_id', ?)",
            (str(uuid.uuid4()),),
        )
    await db.commit()


async def touch_project(db: aiosqlite.Connection, project_id: int | None):
    """Record a substantive project change for ordering and snapshot conflict checks."""
    if project_id is None:
        return
    await db.execute(
        "UPDATE project SET revision=revision+1, modified_at=datetime('now') WHERE id=?",
        (project_id,),
    )


async def touch_projects(db: aiosqlite.Connection, project_ids):
    """Touch each distinct project ID once within the caller's transaction."""
    for project_id in sorted({pid for pid in project_ids if pid is not None}):
        await touch_project(db, project_id)


def create_backup(label: str = "backup") -> Path:
    """Create and verify a consistent backup of the live database.

    The SQLite backup API is safe while other connections are open. Backups use
    immutable timestamped names so cloud-sync tools never observe a half-replaced
    database file.
    """
    source_path = _db_path()
    if not source_path.exists():
        raise FileNotFoundError("AQDA database does not exist")

    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in label)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_path = backup_dir / f"aqda-{safe_label}-{stamp}.db"

    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {result}")
    except Exception:
        destination.close()
        source.close()
        backup_path.unlink(missing_ok=True)
        raise
    else:
        destination.close()
        source.close()
    return backup_path


def _existing_schema_version(path: Path) -> int | None:
    """Read a database version without creating or migrating the database."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT value FROM setting WHERE key='schema_version'"
        ).fetchone()
        return int(row[0]) if row else 1
    except sqlite3.DatabaseError:
        return 1
    finally:
        connection.close()


def _prune_backups(prefix: str, keep: int) -> None:
    backup_dir = DATA_DIR / "backups"
    if not backup_dir.exists():
        return
    matches = sorted(backup_dir.glob(f"aqda-{prefix}-*.db"), reverse=True)
    for stale in matches[keep:]:
        stale.unlink(missing_ok=True)


def create_shared_update_backup(keep: int) -> Path:
    """Back up before a collaborator snapshot replaces local data, keeping the newest ``keep``.

    Active collaboration can fast-forward the local project many times a day, and
    each backup is a full copy of the database, so these are pruned like the daily ones.
    """
    path = create_backup("before-shared-update")
    _prune_backups("before-shared-update", keep=max(1, keep))
    return path


def create_daily_backup() -> Path | None:
    """Create at most one verified rolling backup per UTC day."""
    backup_dir = DATA_DIR / "backups"
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    already_created = (
        any(backup_dir.glob(f"aqda-daily-{day}T*.db"))
        if backup_dir.exists()
        else False
    )
    if already_created:
        return None
    path = create_backup("daily")
    _prune_backups("daily", keep=7)
    return path


async def init_db():
    """Initialize the database schema and run migrations."""
    path = _db_path()
    existing_version = _existing_schema_version(path)
    if existing_version is not None and existing_version < LATEST_SCHEMA_VERSION:
        create_backup(f"before-migration-v{LATEST_SCHEMA_VERSION}")
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
        await _run_migrations(db)
        from aqda.services.offsets import repair_legacy_offsets

        await repair_legacy_offsets(db)
        await db.commit()
    finally:
        await db.close()
    if existing_version is not None:
        create_daily_backup()
