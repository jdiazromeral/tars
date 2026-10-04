"""Idempotent ingestion: raw archive write + index upsert, keyed by (connector, origin)."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Sequence
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
    """No document matches: an append slot that holds nothing, or a reference
    (see `find_doc`) that names no document."""


def _raw_path_ref(ref: str) -> str:
    """A raw file path as the index stores it (`raw/<connector>/<name>.md`),
    from what a user or agent holds: the absolute path `tars show --path`
    prints, or a raw/ path without `.md`. Anything else is returned as is."""
    parts = Path(ref).parts
    if len(parts) >= 3 and parts[-3] == "raw":
        return "/".join(parts[-3:-1] + (parts[-1].removesuffix(".md") + ".md",))
    return ref


class AmbiguousRef(LookupError):
    """A reference matched more than one document; `candidates` are their rows."""

    def __init__(self, ref: str, candidates: list[sqlite3.Row]):
        super().__init__(ref)
        self.ref = ref
        self.candidates = candidates


_DOC_COLUMNS = "id, connector, origin, title, raw_dir, concepts"


def find_doc(db: sqlite3.Connection, ref: str, *, loose: bool = True) -> sqlite3.Row:
    """The one document a reference names, as an index row.

    Exact forms first, each stopping at its match: the id; the origin
    (`jira:PROJ-123`; a URL is canonicalized as `web` origins are); the raw
    file's path (`raw/jira/proj-123.md`). Then, unless `loose=False`, the two
    loose forms *together*, so a tie between them is never settled silently:
    the file name — bare or as a `[[wiki-link#heading|alias]]` — and the
    source's own key (`PROJ-123`, `owner/repo#13`: the origin after its
    scheme), both case-insensitive. A path or URL is never cut down to a file
    name. No match raises NoSuchDocument; several in one step, AmbiguousRef.
    """
    ref = ref.strip()
    origins = {ref}
    if ref.lower().startswith(("http://", "https://")):
        origins.add(store.canonical_url(ref))
    steps = [("id = ?", (ref,)),
             (f"origin IN ({', '.join('?' * len(origins))})", tuple(origins)),
             ("raw_dir = ?", (_raw_path_ref(ref),))]
    if loose:
        link = store.WIKI_LINK_RE.fullmatch(ref)
        # a link's #heading / #^block names a spot inside the file, not the file
        name = link.group(1).split("#", 1)[0].strip() if link else ref.removesuffix(".md")
        # Fold both sides in Python: SQLite's lower() folds ASCII only (Ó, İ).
        db.create_function("tars_fold", 1, lambda s: s.casefold() if s else s,
                           deterministic=True)
        steps.append(("substr(tars_fold(raw_dir), -length(?1)) = ?1 "
                      "OR (instr(origin, ':') > 0 "
                      "AND tars_fold(substr(origin, instr(origin, ':') + 1)) = ?2)",
                      (f"/{name.casefold()}.md", ref.casefold())))
    for condition, params in steps:
        rows = db.execute(f"SELECT {_DOC_COLUMNS} FROM documents WHERE {condition} "
                          "ORDER BY connector, origin", params).fetchall()
        if len(rows) == 1:
            return rows[0]
        if rows:
            raise AmbiguousRef(ref, rows)
    raise NoSuchDocument(ref)


def add(root: Path, db: sqlite3.Connection, doc: RawDoc,
        source_bytes: bytes | None = None, source_ext: str | None = None,
        append: bool = False, create: bool = False, retitle: bool = False) -> tuple[str, str]:
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
        row = db.execute(
            "SELECT content_hash, raw_dir, concepts FROM documents WHERE id = ?", (doc.id,)
        ).fetchone()
        # Decide against the raw file, never the index: the index only helps
        # find the file, and it can be stale (a hand rename/delete) or missing.
        stored_path = _locate(root, doc, row, append=append)
        stored = None
        if stored_path is not None:
            try:
                stored = store.read_raw(stored_path)
            except store.UnparseableRaw:
                if append or doc.connector in AUTHORED_CONNECTORS or _addressed(doc):
                    raise  # never write over words we can't read
                # a synced snapshot: the re-sync repairs it, keeping the index's shelving
                if row:
                    doc.concepts = list(dict.fromkeys(
                        json.loads(row["concepts"] or "[]") + doc.concepts))
        if append and stored_path is None and not create:
            raise NoSuchDocument(f"no document at {doc.origin}")
        if stored is not None:
            _merge_stored(doc, stored, append=append, rules=rules, retitle=retitle)
        digest = store.content_hash(doc.text)

        if stored is not None and _same(doc, stored):
            assert stored_path is not None
            raw_dir = str(stored_path.relative_to(root))
            if not (row and row["content_hash"] == digest and row["raw_dir"] == raw_dir
                    and json.loads(row["concepts"] or "[]") == doc.concepts):
                index_doc(db, stored, digest, raw_dir)  # catch a stale index up from raw
            db.commit()
            return doc.id, "unchanged"

        path = stored_path or store.raw_path_for(root, doc)
        status = "updated" if (stored_path is not None or row) else "added"
        ingestlog.log_ingestion(root, action=status, doc_id=doc.id,
                                connector=doc.connector, origin=doc.origin, title=doc.title)
        raw_path = store.write_raw(root, doc, path, source_bytes, source_ext)
        index_doc(db, doc, digest, str(raw_path.relative_to(root)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return doc.id, status


# `note:<hash>` / `file:<hash>`: the origin is a hash of the captured content,
# so the same origin means the same words or bytes, whatever vocab did since.
_CONTENT_ADDRESSED = re.compile(r"^(note|file):[0-9a-f]{12}$")


def _addressed(doc: RawDoc) -> bool:
    return bool(_CONTENT_ADDRESSED.match(doc.origin))


def _locate(root: Path, doc: RawDoc, row, *, append: bool) -> Path | None:
    """This document's raw file, matched by the id inside it — never by trusting
    the index blindly. The row's path counts only if that file still carries
    this id (a hand rename or delete makes it stale). Without a usable row, a
    new document checks only the path it would be written to; the connector-dir
    scan runs only when it matters and is rare: a stale row, or an append with
    no row (a lost tars.db)."""
    if row:
        path = root / row["raw_dir"]
        if path.exists() and store.file_doc_id(path) == doc.id:
            return path
    if row or append:
        return store.find_raw(root, doc.connector, doc.id)
    candidate = store.raw_path_for(root, doc)
    if candidate.exists() and store.file_doc_id(candidate) == doc.id:
        return candidate
    return None


def _merge_stored(doc: RawDoc, stored: RawDoc, *, append: bool,
                  rules: list[normalize.Rule], retitle: bool = False) -> None:
    """Fold the stored document (read from its raw file) into the incoming one.

    Shelving only grows (concepts union) and a title the caller doesn't restate
    is kept. An append joins the text and merges tags and meta. Authored text
    only grows: a re-add with different text raises WouldReplace, unless the
    origin is content-addressed (same origin, same words) or the difference is
    only a vocab rule added since — then the stored words win. Tags, like
    concepts, only grow. A synced source replaces its text and meta: the source
    owns them. A content-addressed re-add keeps the stored provenance and title
    (unless `retitle`: an explicit --title).
    """
    doc.concepts = list(dict.fromkeys(stored.concepts + doc.concepts))
    doc.tags = list(dict.fromkeys(stored.tags + doc.tags))  # labels only grow, like shelving
    addressed = _addressed(doc)
    if doc.title is None or (addressed and not retitle):
        # a re-drop under a new filename isn't a rename; an explicit --title is
        doc.title = stored.title or doc.title
    if append:
        # A retried append (the command ran, the caller never saw the output)
        # must not duplicate its lines; matching whole trailing lines keeps it
        # a no-op without swallowing a fragment that merely ends a longer line.
        if stored.text != doc.text and not stored.text.endswith(f"\n{doc.text}"):
            doc.text = f"{stored.text}\n{doc.text}".strip("\n")
        else:
            doc.text = stored.text
    elif doc.connector in AUTHORED_CONNECTORS or addressed:
        if doc.text != stored.text:
            renormalized = normalize.apply(stored.text, rules, doc.connector) if rules else None
            vocab_only = renormalized == doc.text
            if not (addressed or vocab_only):
                raise WouldReplace(f"{doc.origin} already holds different text")
            doc.text = stored.text
    if addressed:  # the same capture again keeps its provenance (path, inbox_file, date)
        doc.meta = {**doc.meta, **stored.meta}
        doc.captured_at = stored.captured_at
    elif append or doc.connector in AUTHORED_CONNECTORS:
        doc.meta = {**stored.meta, **doc.meta}


def _same(doc: RawDoc, stored: RawDoc) -> bool:
    """Nothing to write. Meta counts only where it's merged (authored text): a
    synced source's meta can carry volatile fields that would churn every sync."""
    fields = (doc.text, doc.title, doc.tags, doc.concepts)
    if fields != (stored.text, stored.title, stored.tags, stored.concepts):
        return False
    return doc.connector not in AUTHORED_CONNECTORS or doc.meta == stored.meta


def _rewrite_locked(root: Path, db: sqlite3.Connection, locate: Callable[[], Path],
                    change: Callable[[RawDoc], bool]) -> tuple[bool, RawDoc]:
    """Read one raw file, change it, write it back and reindex — all under one
    write lock, so a concurrent append can't be overwritten by a stale snapshot.

    `locate` runs inside the lock and returns the file; `change` edits the doc
    in place and returns whether anything changed (False: nothing is written).
    The shared protocol behind `shelve` and `renormalize`.
    """
    db.execute("BEGIN IMMEDIATE")
    try:
        path = locate()
        doc = store.read_raw(path)
        if not change(doc):
            db.rollback()
            return False, doc
        ingestlog.log_ingestion(root, action="updated", doc_id=doc.id,
                                connector=doc.connector, origin=doc.origin, title=doc.title)
        store.write_raw(root, doc, path)
        index_doc(db, doc, store.content_hash(doc.text), str(path.relative_to(root)))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return True, doc


def shelve(root: Path, db: sqlite3.Connection, doc_id: str,
           add_concepts: Sequence[str] = (),
           remove_concepts: Sequence[str] = ()) -> tuple[str, list[str]]:
    """Add and/or remove concepts on one document — shelving, never content.
    Returns ("updated" | "unchanged", the resulting concepts)."""
    def locate() -> Path:
        row = db.execute("SELECT raw_dir FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            raise NoSuchDocument(f"no document with id {doc_id}")
        return root / row["raw_dir"]

    dropped = set(remove_concepts)

    def change(doc: RawDoc) -> bool:
        concepts = [c for c in dict.fromkeys([*doc.concepts, *add_concepts])
                    if c not in dropped]
        if concepts == doc.concepts:
            return False
        doc.concepts = concepts
        return True

    changed, doc = _rewrite_locked(root, db, locate, change)
    return ("updated" if changed else "unchanged"), doc.concepts


def renormalize(root: Path, db: sqlite3.Connection, raw_path: Path,
                rules: list[normalize.Rule]) -> str:
    """Apply vocab rules to one raw file: the one sanctioned rewrite of
    authored text. Returns "updated" or "unchanged"."""
    def change(doc: RawDoc) -> bool:
        text = normalize.apply(doc.text, rules, doc.connector)
        if text == doc.text:
            return False
        doc.text = text
        return True

    changed, _ = _rewrite_locked(root, db, lambda: raw_path, change)
    return "updated" if changed else "unchanged"


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


def annotate(root: Path, db: sqlite3.Connection, target: sqlite3.Row, text: str,
             title: str | None = None, concepts: Sequence[str] = ()) -> tuple[str, str]:
    """Record the user's words about `target` (a `find_doc` row) as a note of
    their own: `meta.annotates_id` holds the target's id for tars, a top-level
    `annotates: "[[stem]]"` property is the link Obsidian reads, and the note inherits its
    concepts, so it shelves into the same hubs. The target is never touched, so
    a re-sync of it can't lose the annotation. The origin hashes target and
    text together: the same words twice on one target are one note (a retried
    command is a no-op), on two targets two notes. Returns (doc_id, status).
    """
    shelved = json.loads(target["concepts"] or "[]")
    doc = RawDoc(
        connector="note",
        origin=note_origin(f"{target['id']}\n{text}"),
        text=text,
        # Never derived from the target's title: `tars rm` of a target (say, one
        # with a pasted secret in its title) keeps its annotations, which must
        # not carry that title along in their own title or file name.
        title=title or text.partition("\n")[0][:80].strip(),
        concepts=list(dict.fromkeys([*shelved, *(store.slugify(c) for c in concepts)])),
        meta={"annotates_id": target["id"]},
        annotates=f"[[{store.stem_of(target['raw_dir'])}]]",
    )
    return add(root, db, doc, retitle=title is not None)


def annotations_of(db: sqlite3.Connection, doc_id: str) -> list[sqlite3.Row]:
    """The annotations that point at `doc_id`, oldest first."""
    return db.execute(
        "SELECT id, title, captured_at, raw_dir FROM documents "
        "WHERE json_extract(meta, '$.annotates_id') = ? ORDER BY captured_at, id",
        (doc_id,)).fetchall()


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
