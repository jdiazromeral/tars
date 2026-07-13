"""Filesystem layout, document identity, and the raw-archive format.

Raw layout:  raw/<connector>/<title-slug>.md   (frontmatter + normalized markdown)
             raw/<connector>/<title-slug>.<ext> (original bytes, when they exist)
Wiki layout: wiki/concepts/<slug>.md            (what things are — the vault's hubs)
             wiki/people/<slug>.md              (who's involved — identity map across tools)
             wiki/notes/<slug>.md               (promoted insights)

Raw files are named by their human title so wiki-links and graph nodes are
readable; the doc id stays inside (frontmatter + DB) as the idempotence key.
A filename is fixed at first ingest and never renamed — links stay stable
even if the title changes later. Slug collisions between different documents
get a short id suffix.

Format v2: concepts are a first-class frontmatter field (`concepts: [...]`) —
the single truth for shelving. The `Concepts: [[...]]` body line is derived
from it at write time (kept so the Obsidian graph clusters) and excluded from
the content hash, so shelving state survives re-sync of mutable sources and
never counts as a content change.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import yaml

from . import db

MARKER = ".tars"
SCHEMA_VERSION = 2  # vault format this tool writes and understands
RAW_DIR = "raw"
WIKI_DIR = "wiki"
NOTES_DIR = "wiki/notes"
CONCEPTS_DIR = "wiki/concepts"
PEOPLE_DIR = "wiki/people"
TASKS_DIR = "tasks"
DIGESTS_DIR = "digests"
INBOX_DIR = "inbox"  # zero-ceremony landing zone; `tars sweep` drains it
LOG_DIR = "log"
INGEST_LOG = "log/ingestions.jsonl"  # append-only ingestion history (add/update/delete)


class NotARootError(Exception):
    pass


class VaultVersionError(Exception):
    pass


def find_root(start: Path | None = None) -> Path:
    if env := os.environ.get("TARS_HOME"):
        root = Path(env).expanduser().resolve()
        if not (root / MARKER).exists():
            raise NotARootError(
                f"TARS_HOME={root} has no {MARKER} marker — "
                f"run `tars init {root}` there first, or fix TARS_HOME"
            )
        return root
    cur = (start or Path.cwd()).resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / MARKER).exists():
            return candidate
    raise NotARootError(
        "not inside a TARS root — run `tars init` here or set TARS_HOME"
    )


def init_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text(f"version: {SCHEMA_VERSION}\n")
    for layer in (RAW_DIR, NOTES_DIR, CONCEPTS_DIR, PEOPLE_DIR, TASKS_DIR,
                  DIGESTS_DIR, INBOX_DIR, LOG_DIR):
        (root / layer).mkdir(parents=True, exist_ok=True)
    _write_gitignore(root)


def _write_gitignore(root: Path) -> None:
    """The vault is a git repo; the index is a disposable cache (`tars reindex`
    rebuilds it). Without this the first `git add` swallows the DB, and it only
    grows. `init` is re-runnable, so append what's missing and leave the user's
    own entries alone."""
    gitignore = root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    present = set(existing.split())
    missing = [p for p in (db.DB_NAME, f"{db.DB_NAME}-wal", f"{db.DB_NAME}-shm")
               if p not in present]
    if not missing:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with gitignore.open("a") as fh:
        fh.write(prefix + "".join(f"{p}\n" for p in missing))


def read_marker(root: Path) -> dict:
    """Parse the .tars marker as vault config. Absent or empty = a valid v1 root
    (the marker started life as an empty sentinel; that history stays valid)."""
    marker = root / MARKER
    text = marker.read_text().strip() if marker.exists() else ""
    if not text:
        return {}
    data = yaml.safe_load(text)
    return data if isinstance(data, dict) else {}


def vault_version(root: Path) -> int:
    """Format version recorded in .tars; unstamped (empty) markers read as v1."""
    return int(read_marker(root).get("version", 1))


def check_version(root: Path) -> None:
    """Refuse a vault whose format doesn't match this tool: newer means upgrade
    the tool; older means run `tars migrate` — either way, never mix formats
    silently."""
    found = vault_version(root)
    if found > SCHEMA_VERSION:
        raise VaultVersionError(
            f"vault at {root} is format v{found}, but this TARS understands up to "
            f"v{SCHEMA_VERSION} — upgrade the tool (`uv sync`)."
        )
    if found < SCHEMA_VERSION:
        raise VaultVersionError(
            f"vault at {root} is format v{found}; this TARS writes v{SCHEMA_VERSION} "
            f"— run `tars migrate` first (it rewrites raw/ in place; back up first)."
        )


def doc_id(connector: str, origin: str) -> str:
    return hashlib.sha256(f"{connector}:{origin}".encode()).hexdigest()[:12]


def content_hash(data: str | bytes) -> str:
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class RawDoc:
    connector: str
    origin: str
    text: str  # source body only — concepts are shelving state, never part of it
    title: str | None = None
    captured_at: str = field(default_factory=now_iso)
    tags: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        return doc_id(self.connector, self.origin)


TRACKING_PARAMS = re.compile(r"^(utm_\w+|gclid|fbclid|msclkid|mc_cid|mc_eid|igshid)$", re.I)


def canonical_url(url: str) -> str:
    """Canonical form of a URL for use as a `web` origin: identity must not
    depend on tracking junk or fragments, or the same page mints duplicates."""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def render_body(doc: RawDoc) -> str:
    """The body as written to disk and indexed: a derived `Concepts:` wiki-link
    line (when shelved) above the verbatim text."""
    if not doc.concepts:
        return doc.text
    links = " ".join(f"[[{slug}]]" for slug in doc.concepts)
    return f"Concepts: {links}\n\n{doc.text}"


def _file_doc_id(path: Path) -> str | None:
    try:
        with path.open() as fh:
            for line in [next(fh, "") for _ in range(3)]:
                if line.startswith("id: "):
                    return line[4:].strip()
    except OSError:
        pass
    return None


def raw_path_for(root: Path, doc: RawDoc, existing: str | None = None) -> Path:
    """Pick the raw file path. `existing` (relative path from the DB) wins so
    a document's filename — and every link to it — stays stable forever."""
    if existing:
        return root / existing
    conn_dir = root / RAW_DIR / slugify(doc.connector)
    base = slugify(doc.title) if doc.title else doc.id
    candidate = conn_dir / f"{base}.md"
    if candidate.exists() and _file_doc_id(candidate) != doc.id:
        candidate = conn_dir / f"{base}-{doc.id[:6]}.md"
    return candidate


def write_raw(root: Path, doc: RawDoc, path: Path,
              source_bytes: bytes | None = None,
              source_ext: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = {
        "id": doc.id,
        "connector": doc.connector,
        "origin": doc.origin,
        "title": doc.title,
        "aliases": [doc.title] if doc.title else [],
        "captured_at": doc.captured_at,
        "tags": doc.tags,
        "concepts": doc.concepts,
        "meta": doc.meta,
    }
    header = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    payload = f"---\n{header}\n---\n\n{render_body(doc)}\n"
    # Raw is truth: leave the file untouched (bytes AND mtime) when nothing changed,
    # so an index rebuild can never churn the archive. Written via temp file +
    # atomic rename so a concurrent reader (e.g. `tars tag` reading the raw file
    # before its own ingest.add) can never observe a half-written file.
    if not path.exists() or path.read_text() != payload:
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(payload)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
    if source_bytes is not None:
        path.with_suffix(f".{source_ext or 'bin'}").write_bytes(source_bytes)
    return path


def read_raw(content_md: Path) -> RawDoc:
    raw = content_md.read_text()
    if not raw.startswith("---\n"):
        raise ValueError(f"{content_md}: missing frontmatter")
    header, _, body = raw[4:].partition("\n---\n")
    fm = yaml.safe_load(header)
    text = body.strip("\n")

    # The Concepts: line is a derived rendering — strip it back out of the body.
    line_concepts: list[str] = []
    if text.startswith("Concepts: "):
        first, _, rest = text.partition("\n")
        line_concepts = re.findall(r"\[\[([^\]|]+)\]\]", first)
        text = rest.lstrip("\n")

    meta = fm.get("meta") or {}
    if "concepts" in fm:  # v2: frontmatter is the single truth
        concepts = fm.get("concepts") or []
    else:  # v1 compat: concepts lived in the body line and/or meta
        concepts = line_concepts or meta.pop("concepts", [])
        meta.pop("concepts", None)

    captured = fm.get("captured_at")
    if isinstance(captured, datetime):  # YAML parses unquoted timestamps eagerly
        captured = captured.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return RawDoc(
        connector=fm["connector"],
        origin=fm["origin"],
        text=text,
        title=fm.get("title"),
        captured_at=captured or now_iso(),
        tags=fm.get("tags") or [],
        concepts=concepts,
        meta=meta,
    )


def iter_raw(root: Path):
    yield from sorted((root / RAW_DIR).glob("*/*.md"))


def slugify(title: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in title)
    slug = "-".join(part for part in slug.split("-") if part)
    if len(slug) > 80:
        # Trim at a word boundary — these names are permanent link targets,
        # and "…-transform-in-goo" (Google) reads like a different word.
        cut = slug[:80]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug or "note"
