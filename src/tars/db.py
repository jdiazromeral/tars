"""SQLite catalog: documents, chunks, FTS5 index, sync cursors.

The whole database is a cache — `tars reindex` rebuilds it from raw/.
The `chunks.embedding` column is reserved so a vector layer can be added
later without a schema migration.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DB_NAME = "tars.db"

# unicode61 + remove_diacritics 2, NOT porter: the corpus mixes Spanish and
# English, and Porter is an English-only stemmer that does nothing for
# "reunión/reuniones" while accent-sensitivity silently loses "reunion" ↔
# "reunión" matches. Accent-folding both languages beats stemming one.
FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS documents (
    id            TEXT PRIMARY KEY,
    connector     TEXT NOT NULL,
    origin        TEXT NOT NULL,
    title         TEXT,
    captured_at   TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    raw_dir       TEXT NOT NULL,
    concepts      TEXT NOT NULL DEFAULT '[]',
    meta          TEXT NOT NULL DEFAULT '{{}}',
    UNIQUE (connector, origin)
);

CREATE TABLE IF NOT EXISTS chunks (
    id        INTEGER PRIMARY KEY,
    doc_id    TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq       INTEGER NOT NULL,
    text      TEXT NOT NULL,
    embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

{FTS_DDL}

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;

CREATE TABLE IF NOT EXISTS sync_state (
    connector      TEXT PRIMARY KEY,
    cursor         TEXT,
    last_sync      TEXT,
    pending_cursor TEXT
);
"""


def _migrate(db: sqlite3.Connection) -> None:
    """Idempotent, additive schema catch-up for DBs created before a column existed.
    The DB is a cache, but sync cursors are the one bit of state not rebuilt by
    reindex, so we grow the table in place rather than force a manual reset."""
    cols = {row["name"] for row in db.execute("PRAGMA table_info(sync_state)")}
    if "pending_cursor" not in cols:
        db.execute("ALTER TABLE sync_state ADD COLUMN pending_cursor TEXT")
    doc_cols = {row["name"] for row in db.execute("PRAGMA table_info(documents)")}
    if "concepts" not in doc_cols:
        db.execute("ALTER TABLE documents ADD COLUMN concepts TEXT NOT NULL DEFAULT '[]'")
    # A speculative structured layer that nothing ever wrote to — shelving went
    # to documents.concepts and identity to wiki/people/ instead.
    db.execute("DROP TABLE IF EXISTS doc_entities")
    db.execute("DROP TABLE IF EXISTS entities")
    # Tokenizer change (porter → unicode61 accent-folding): rebuild the FTS
    # table in place from chunks — the index is a cache, so this is cheap and
    # nobody has to remember to reindex.
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
    ).fetchone()
    if row and "porter" in row["sql"]:
        db.execute("DROP TABLE chunks_fts")
        db.executescript(FTS_DDL)
        db.execute("INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks")


def connect(root: Path) -> sqlite3.Connection:
    db = sqlite3.connect(root / DB_NAME, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    # WAL + busy timeout: concurrent agent sessions must queue, not crash.
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA busy_timeout = 10000")
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()  # migration DML (e.g. the FTS rebuild) must not ride on the caller's tx
    return db
