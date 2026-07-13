"""Idempotent ingestion: raw archive write + index upsert, keyed by (connector, origin)."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import ingestlog, normalize, store
from .store import RawDoc

CHUNK_TARGET = 1600  # characters; roughly 400 tokens


def chunk_text(text: str, target: int = CHUNK_TARGET) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= target * 1.5:
            pieces.append(paragraph)
        else:
            for start in range(0, len(paragraph), target):
                pieces.append(paragraph[start:start + target])
    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if buffer and len(buffer) + len(piece) > target:
            chunks.append(buffer)
            buffer = piece
        else:
            buffer = f"{buffer}\n\n{piece}" if buffer else piece
    if buffer:
        chunks.append(buffer)
    return chunks


def add(root: Path, db: sqlite3.Connection, doc: RawDoc,
        source_bytes: bytes | None = None, source_ext: str | None = None,
        raw_dir: str | None = None, concepts_mode: str = "merge",
        log: bool = True) -> tuple[str, str]:
    """Ingest one document. Returns (doc_id, status) with status in added/updated/unchanged.

    `raw_dir` pins the raw file location when the DB has no row to remember it
    (reindex reads docs *from* their raw files) — a filename is fixed at first
    ingest and must never move, even across an index rebuild.

    Concepts are shelving state, not content: by default (`concepts_mode="merge"`)
    a re-ingest unions the incoming concepts with the stored ones, so a re-sync
    can add shelving but never remove it — only an explicit `untag` (which passes
    "replace") takes concepts away. The content hash covers `doc.text` alone, so
    a shelving change never masquerades as a content change.

    The read of the existing row, the merge, and the write all happen inside one
    BEGIN IMMEDIATE transaction: it takes SQLite's write lock before the read, so
    two concurrent `add` calls on the same doc (e.g. two `tars tag` invocations
    racing) can't both read the same stale `concepts` and have the second one's
    write silently clobber the first one's merge.
    """
    if concepts_mode not in ("merge", "replace"):
        raise ValueError(f"concepts_mode must be merge or replace, got {concepts_mode!r}")
    # Canonicalize: read_raw strips outer newlines, so text must be hashed in
    # that same form or a connector passing a trailing "\n" (github did) makes
    # every stored hash stale on re-read — permanent db-drift + upsert churn.
    doc.text = doc.text.strip("\n")
    rules = normalize.load_rules(root)
    if rules:
        doc.text = normalize.apply(doc.text, rules, doc.connector)
    digest = store.content_hash(doc.text)

    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT content_hash, raw_dir, concepts FROM documents WHERE id = ?", (doc.id,)
        ).fetchone()
        if existing and concepts_mode == "merge":
            stored = json.loads(existing["concepts"] or "[]")
            doc.concepts = list(dict.fromkeys(stored + doc.concepts))
        if (existing and existing["content_hash"] == digest
                and json.loads(existing["concepts"] or "[]") == doc.concepts):
            db.rollback()
            return doc.id, "unchanged"

        path = store.raw_path_for(root, doc, existing["raw_dir"] if existing else raw_dir)
        raw_path = store.write_raw(root, doc, path, source_bytes, source_ext)
        db.execute(
            """
            INSERT INTO documents (id, connector, origin, title, captured_at,
                                   content_hash, raw_dir, concepts, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                captured_at = excluded.captured_at,
                content_hash = excluded.content_hash,
                raw_dir = excluded.raw_dir,
                concepts = excluded.concepts,
                meta = excluded.meta
            """,
            (doc.id, doc.connector, doc.origin, doc.title, doc.captured_at,
             digest, str(raw_path.relative_to(root)), json.dumps(doc.concepts),
             json.dumps(doc.meta)),
        )
        db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc.id,))
        # Index the rendered body (concepts line included) so searching a
        # concept slug surfaces everything shelved under it.
        db.executemany(
            "INSERT INTO chunks (doc_id, seq, text) VALUES (?, ?, ?)",
            [(doc.id, seq, text)
             for seq, text in enumerate(chunk_text(store.render_body(doc)))],
        )
        db.commit()
    except BaseException:
        db.rollback()
        raise

    status = "updated" if existing else "added"
    if log:  # append-only ingestion history; a cache rebuild passes log=False
        ingestlog.log_ingestion(root, action=status, doc_id=doc.id,
                                connector=doc.connector, origin=doc.origin, title=doc.title)
    return doc.id, status


def reindex(root: Path, db: sqlite3.Connection) -> int:
    """Rebuild the whole index from raw/. The DB is a cache; raw/ is truth.

    Rebuilding the cache is not an ingestion event, so `log=False` keeps it out
    of the append-only ingestion log — otherwise every reindex would re-log the
    whole corpus as freshly `added`.
    """
    with db:
        db.execute("DELETE FROM documents")
    count = 0
    for content_md in store.iter_raw(root):
        add(root, db, store.read_raw(content_md),
            raw_dir=str(content_md.relative_to(root)), log=False)
        count += 1
    return count
