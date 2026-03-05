"""Database setup and connection management using aiosqlite."""

import aiosqlite
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("AQDA_DATA_DIR", Path.home() / ".aqda"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    modified_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS document (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT DEFAULT 'text',
    transcript TEXT DEFAULT NULL,
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
    deleted_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS coding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES document(id) ON DELETE CASCADE,
    code_id INTEGER NOT NULL REFERENCES code(id) ON DELETE CASCADE,
    start_pos INTEGER NOT NULL,
    end_pos INTEGER NOT NULL,
    selected_text TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    deleted_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS memo (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES project(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES document(id) ON DELETE CASCADE,
    code_id INTEGER REFERENCES code(id) ON DELETE SET NULL,
    coding_id INTEGER REFERENCES coding(id) ON DELETE SET NULL,
    title TEXT DEFAULT '',
    content TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    modified_at TEXT DEFAULT (datetime('now'))
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
    ('schema_version', '5');

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
}

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


def _db_path(project_id: int | None = None) -> Path:
    """Get path to the main database."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / "aqda.db"


async def get_db() -> aiosqlite.Connection:
    """Get a database connection."""
    db = await aiosqlite.connect(_db_path())
    db.row_factory = aiosqlite.Row
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
            except Exception:
                pass  # Column may already exist from fresh schema
        await db.execute(
            "INSERT OR REPLACE INTO setting (key, value) VALUES ('schema_version', ?)",
            (str(target),),
        )
        await db.commit()


async def init_db():
    """Initialize the database schema and run migrations."""
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
        await _run_migrations(db)
    finally:
        await db.close()
