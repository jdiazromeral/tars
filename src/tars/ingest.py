"""Idempotent ingestion: raw archive write + index upsert, keyed by (connector, origin)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from . import extract, ingestlog, normalize, store
from .store import RawDoc

CHUNK_TARGET = 1600  # characters; roughly 400 tokens


@dataclass
class Resolved:
    """A capture target turned into its identity (connector + origin) and content."""
    connector: str
    origin: str
    extracted: extract.Extracted


def note_origin(text: str) -> str:
    """Content-addressed origin for the user's own words: re-pasting dedupes."""
    return f"note:{store.content_hash(text)[:12]}"


def resolve_target(target: str, *, stdin: TextIO | None = None,
                   origin: str | None = None) -> Resolved:
    """Map a `tars add` target — '-' (text on `stdin`), a URL, or a file path —
    to its connector, stable origin, and extracted content.

    This is where built-in captures get their identity. Defaults are chosen so
    re-capture never mints a duplicate: notes and files are content-addressed
    (a file's path is provenance, kept in meta, not identity), URLs are
    canonicalized so tracking params and fragments can't split one page in
    two. An explicit `origin` overrides the default, turning the capture into
    a mutable slot that later captures update in place.

    Raises extract.ExtractionError for an empty note or a missing file; fetch
    and parse errors propagate as-is.
    """
    if target == "-":
        text = (stdin.read() if stdin else "").strip()
        if not text:
            raise extract.ExtractionError("stdin was empty")
        return Resolved("note", origin or note_origin(text), extract.Extracted(text=text))
    if target.startswith(("http://", "https://")):
        extracted = extract.from_url(target)
        # Web captures keep only the extracted text — no sidecar of the fetched
        # bytes, even for a PDF link (a file capture of the same PDF keeps one).
        extracted.source_bytes = extracted.source_ext = None
        return Resolved("web", origin or store.canonical_url(target), extracted)
    path = Path(target).expanduser()
    if not path.exists():
        raise extract.ExtractionError(f"no such file: {target}")
    file_origin = origin or f"file:{store.content_hash(path.read_bytes())[:12]}"
    return Resolved("file", file_origin, extract.from_file(path))


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
        log: bool = True, append: bool = False) -> tuple[str, str]:
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

    `append=True` makes `doc.text` an addition to the stored text instead of a
    replacement: the existing raw text is read inside that same transaction and
    `doc.text` is joined to its end with one newline, so two concurrent appends
    can't lose each other. With no existing document it is a plain add, and
    re-appending lines the body already ends with is a no-op.

    The ingestion event is logged before the raw write (see `ingestlog`) and
    inside the write lock, so the log's order matches the commit order.
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

    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT content_hash, raw_dir, concepts FROM documents WHERE id = ?", (doc.id,)
        ).fetchone()
        if existing and append:
            previous = store.read_raw(root / existing["raw_dir"]).text
            # A retried append (the command ran, the caller never saw the output)
            # must not duplicate its lines; matching whole trailing lines keeps it
            # a no-op without swallowing a fragment that merely ends a longer line.
            if previous != doc.text and not previous.endswith(f"\n{doc.text}"):
                doc.text = f"{previous}\n{doc.text}".strip("\n")
            else:
                doc.text = previous
        digest = store.content_hash(doc.text)
        if existing and concepts_mode == "merge":
            stored = json.loads(existing["concepts"] or "[]")
            doc.concepts = list(dict.fromkeys(stored + doc.concepts))
        if (existing and existing["content_hash"] == digest
                and json.loads(existing["concepts"] or "[]") == doc.concepts):
            db.rollback()
            return doc.id, "unchanged"

        path = store.raw_path_for(root, doc, existing["raw_dir"] if existing else raw_dir)
        status = "updated" if existing else "added"
        if log:  # append-only history, written first; a cache rebuild passes log=False
            ingestlog.log_ingestion(root, action=status, doc_id=doc.id,
                                    connector=doc.connector, origin=doc.origin, title=doc.title)
        raw_path = store.write_raw(root, doc, path, source_bytes, source_ext)
        index_doc(db, doc, digest, str(raw_path.relative_to(root)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return doc.id, status


def index_doc(db: sqlite3.Connection, doc: RawDoc, digest: str, raw_dir: str) -> None:
    """Write one document's index row and chunks — the cache side only, never raw.

    Runs inside the caller's transaction and does not commit: `add` wraps it
    with the raw write, `reindex` with the whole rebuild.
    """
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
         digest, raw_dir, json.dumps(doc.concepts), json.dumps(doc.meta)),
    )
    db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc.id,))
    # Index the rendered body (concepts line included) so searching a
    # concept slug surfaces everything shelved under it.
    db.executemany(
        "INSERT INTO chunks (doc_id, seq, text) VALUES (?, ?, ?)",
        [(doc.id, seq, text)
         for seq, text in enumerate(chunk_text(store.render_body(doc)))],
    )


def remove(root: Path, db: sqlite3.Connection, doc_id: str) -> sqlite3.Row:
    """Delete a capture everywhere — raw file, source sidecar, index row —
    logging the deletion first (see `ingestlog`). Returns the removed
    document's row (connector, origin, title, raw_dir). Raises LookupError for
    an unknown id.

    The redaction path: wiki-links pointing at the capture are left alone for
    the caller to report (`doctor.references_to`), never rewritten here.
    """
    row = db.execute(
        "SELECT connector, origin, title, raw_dir FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if not row:
        raise LookupError(f"no document with id {doc_id}")
    ingestlog.log_ingestion(root, action="deleted", doc_id=doc_id,
                            connector=row["connector"], origin=row["origin"], title=row["title"])
    for target in store.raw_files(root / row["raw_dir"]):
        target.unlink(missing_ok=True)
    with db:
        db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return row


def reindex(root: Path, db: sqlite3.Connection) -> tuple[int, list[tuple[Path, str]]]:
    """Rebuild the whole index from raw/. The DB is a cache; raw/ is truth.

    A pure read of the archive: each file is parsed and indexed as it stands —
    never rewritten, never re-normalized (applying vocab is `tars normalize`'s
    job), so a rebuild can't drop a byte. Not an ingestion event either, so
    nothing is logged.

    The rebuild is one transaction: readers keep the old index until it
    commits, and a crash halfway rolls back to it instead of leaving a
    half-empty cache. A file that doesn't parse is skipped and returned with
    the reason, so one bad file can't take the other documents down with it.
    Returns (documents indexed, [(unparseable path, reason)]).
    """
    count, unparseable = 0, []
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DELETE FROM documents")
        for content_md in store.iter_raw(root):
            try:
                doc = store.read_raw(content_md)
            except ValueError as exc:
                unparseable.append((content_md, str(exc).removeprefix(f"{content_md}: ")))
                continue
            index_doc(db, doc, store.content_hash(doc.text),
                      str(content_md.relative_to(root)))
            count += 1
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return count, unparseable
