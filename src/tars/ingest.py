"""Idempotent ingestion: raw archive write + index upsert, keyed by (connector, origin)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
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


# Connectors whose text is authored here — your notes, agent work records, the
# day's activity — rather than synced from a source that owns it. Their text
# only grows: a re-add with different text is refused (WouldReplace), never
# applied. A synced source (jira, gmail, web, ...) refreshes in place.
AUTHORED_CONNECTORS = frozenset({"note", "agent", "activity"})


class WouldReplace(Exception):
    """A re-add would replace authored text; append to it or use a new origin."""


class NoSuchDocument(Exception):
    """`append` without `create` named a slot that holds no document."""


def add(root: Path, db: sqlite3.Connection, doc: RawDoc,
        source_bytes: bytes | None = None, source_ext: str | None = None,
        append: bool = False, create: bool = True) -> tuple[str, str]:
    """Ingest one document. Returns (doc_id, status) with status in added/updated/unchanged.

    Adds never replace authored text: for a connector in AUTHORED_CONNECTORS,
    re-adding *different* text to an existing document raises WouldReplace —
    checked against the raw file, not the possibly stale index; a difference
    that is only a vocab rule added since keeps the stored words. The one
    sanctioned rewrite is `renormalize`. A re-add that doesn't restate the
    title (None) keeps the stored one.

    Concepts are shelving state, not content: a re-ingest unions the incoming
    concepts with the stored ones, so a re-sync can add shelving but never
    remove it — only `shelve` (tars untag) takes concepts away. The content hash
    covers `doc.text` alone, so a shelving change never masquerades as a
    content change.

    The read of the existing row, the merge, and the write all happen inside one
    BEGIN IMMEDIATE transaction: it takes SQLite's write lock before the read, so
    two concurrent `add` calls on the same doc (e.g. two syncs racing) can't both
    read the same stale `concepts` and have the second one's write silently
    clobber the first one's merge.

    `append=True` makes `doc.text` an addition to the stored text instead of a
    replacement: the existing raw text is read inside that same transaction and
    `doc.text` is joined to its end with one newline, so two concurrent appends
    can't lose each other. With no existing document it is a plain add, and
    re-appending lines the body already ends with is a no-op. An append also
    keeps the stored title, tags and meta it doesn't restate (tags merge). With
    `create=False`, appending to a slot that holds nothing raises
    NoSuchDocument instead of starting a new document from a typo.

    The ingestion event is logged before the raw write (see `ingestlog`) and
    inside the write lock, so the log's order matches the commit order.
    """
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
            "SELECT content_hash, raw_dir, concepts, title FROM documents WHERE id = ?",
            (doc.id,),
        ).fetchone()
        if append and not existing and not create:
            raise NoSuchDocument(f"no document at {doc.origin}")
        if existing and doc.title is None:
            doc.title = existing["title"]
        # The raw file, not the index, is what an add must not lose: the index
        # can be stale (a hand-edit before finalize), so compare against the file.
        stored_doc = None
        if existing and (append or doc.connector in AUTHORED_CONNECTORS):
            stored_doc = store.read_raw(root / existing["raw_dir"])
        if stored_doc is not None and append:
            previous = stored_doc.text
            # A retried append (the command ran, the caller never saw the output)
            # must not duplicate its lines; matching whole trailing lines keeps it
            # a no-op without swallowing a fragment that merely ends a longer line.
            if previous != doc.text and not previous.endswith(f"\n{doc.text}"):
                doc.text = f"{previous}\n{doc.text}".strip("\n")
            else:
                doc.text = previous
            doc.tags = list(dict.fromkeys(stored_doc.tags + doc.tags))
            doc.meta = {**stored_doc.meta, **doc.meta}
        elif stored_doc is not None and doc.text != stored_doc.text:
            # Authored text only grows. A difference that is only a vocab rule
            # added since keeps the stored words — rewriting them is normalize's job.
            if rules and normalize.apply(stored_doc.text, rules, doc.connector) == doc.text:
                doc.text = stored_doc.text
            else:
                raise WouldReplace(f"{doc.origin} already holds different text")
        digest = store.content_hash(doc.text)
        if existing:
            stored = json.loads(existing["concepts"] or "[]")
            doc.concepts = list(dict.fromkeys(stored + doc.concepts))
        unchanged = bool(existing and existing["content_hash"] == digest
                         and json.loads(existing["concepts"] or "[]") == doc.concepts)
        if unchanged and append and stored_doc is not None:
            # a retried append may still carry a new title, tag or meta
            unchanged = (doc.title, doc.tags, doc.meta) == (
                stored_doc.title, stored_doc.tags, stored_doc.meta)
        if unchanged:
            db.rollback()
            return doc.id, "unchanged"

        path = store.raw_path_for(root, doc, existing["raw_dir"] if existing else None)
        status = "updated" if existing else "added"
        ingestlog.log_ingestion(root, action=status, doc_id=doc.id,
                                connector=doc.connector, origin=doc.origin, title=doc.title)
        raw_path = store.write_raw(root, doc, path, source_bytes, source_ext)
        index_doc(db, doc, digest, str(raw_path.relative_to(root)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return doc.id, status


def shelve(root: Path, db: sqlite3.Connection, doc_id: str,
           add_concepts: Sequence[str] = (),
           remove_concepts: Sequence[str] = ()) -> tuple[str, list[str]]:
    """Add and/or remove concepts on one document — shelving, never content.

    The raw file is read *inside* the write lock and its text written back as
    read, so an append landing concurrently can't be overwritten by a stale
    snapshot (tag/untag used to read outside the lock and re-add the whole
    document). Returns ("updated" | "unchanged", the resulting concepts).
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute("SELECT raw_dir FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NoSuchDocument(f"no document with id {doc_id}")
        path = root / row["raw_dir"]
        doc = store.read_raw(path)
        dropped = set(remove_concepts)
        concepts = [c for c in dict.fromkeys([*doc.concepts, *add_concepts])
                    if c not in dropped]
        if concepts == doc.concepts:
            db.rollback()
            return "unchanged", concepts
        doc.concepts = concepts
        ingestlog.log_ingestion(root, action="updated", doc_id=doc.id,
                                connector=doc.connector, origin=doc.origin, title=doc.title)
        store.write_raw(root, doc, path)
        index_doc(db, doc, store.content_hash(doc.text), row["raw_dir"])
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return "updated", concepts


def renormalize(root: Path, db: sqlite3.Connection, raw_path: Path,
                rules: list[normalize.Rule]) -> str:
    """Apply vocab rules to one raw file: the one sanctioned rewrite of
    authored text. Read, rewrite and reindex under one write lock (as in
    `shelve`), so an append landing concurrently can't be overwritten by a
    stale snapshot. Returns "updated" or "unchanged"."""
    db.execute("BEGIN IMMEDIATE")
    try:
        doc = store.read_raw(raw_path)
        text = normalize.apply(doc.text, rules, doc.connector)
        if text == doc.text:
            db.rollback()
            return "unchanged"
        doc.text = text
        ingestlog.log_ingestion(root, action="updated", doc_id=doc.id,
                                connector=doc.connector, origin=doc.origin, title=doc.title)
        store.write_raw(root, doc, raw_path)
        index_doc(db, doc, store.content_hash(text), str(raw_path.relative_to(root)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return "updated"


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

    Every file is parsed first, outside the write lock, so concurrent writers
    wait only for the DB writes. Those run in one transaction: readers keep
    the old index until it commits, and a crash rolls back to it.

    A file that doesn't parse is skipped and returned with the reason, and
    its existing index row is *kept*: dropping it would make the next
    `add --append` or re-sync of that origin see no document and overwrite
    the file. The row is replaced once the file parses again.
    Returns (documents indexed, [(unparseable path, reason)]).
    """
    docs: list[tuple[RawDoc, str]] = []
    unparseable: list[tuple[Path, str]] = []
    for content_md in store.iter_raw(root):
        try:
            docs.append((store.read_raw(content_md), str(content_md.relative_to(root))))
        except store.UnparseableRaw as exc:
            unparseable.append((content_md, exc.reason))
    keep = {str(path.relative_to(root)) for path, _ in unparseable}

    db.execute("BEGIN IMMEDIATE")
    try:
        stale = [(row["id"],) for row in db.execute("SELECT id, raw_dir FROM documents")
                 if row["raw_dir"] not in keep]
        db.executemany("DELETE FROM documents WHERE id = ?", stale)
        for doc, raw_dir in docs:
            index_doc(db, doc, store.content_hash(doc.text), raw_dir)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return len(docs), unparseable
