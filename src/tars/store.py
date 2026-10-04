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
import unicodedata
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
    # An annotation's target as a wiki-link ("[[stem]]"), written as a top-level
    # property because that is what Obsidian reads as a link (backlinks); tars
    # resolves the target by meta["annotates_id"]. None for every other doc.
    annotates: str | None = None

    @property
    def id(self) -> str:
        return doc_id(self.connector, self.origin)


TRACKING_PARAMS = re.compile(r"^(utm_\w+|gclid|fbclid|msclkid|mc_cid|mc_eid|igshid)$", re.I)


# A [[stem]] / [[stem|alias]] / [[stem\\|alias]] wiki-link (the escaped pipe is
# Obsidian's form inside a markdown table); group 1 is the stem.
WIKI_LINK_RE = re.compile(r"\[\[([^\]|\\]+)(?:\\?\|[^\]]*)?\]\]")


def stem_of(raw_dir: str) -> str:
    """A raw file's name without `.md` — its [[wiki-link]] target."""
    return Path(raw_dir).stem


def canonical_url(url: str) -> str:
    """Canonical form of a URL for use as a `web` origin: identity must not
    depend on tracking junk or fragments, or the same page mints duplicates."""
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not TRACKING_PARAMS.match(k)])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def concepts_line(concepts: list[str]) -> str:
    """The derived shelving line, exactly as `render_body` writes it."""
    return "Concepts: " + " ".join(f"[[{slug}]]" for slug in concepts)


def render_body(doc: RawDoc) -> str:
    """The body as written to disk and indexed: a derived `Concepts:` wiki-link
    line (when shelved) above the verbatim text."""
    if not doc.concepts:
        return doc.text
    return f"{concepts_line(doc.concepts)}\n\n{doc.text}"


def file_doc_id(path: Path) -> str | None:
    try:
        with path.open() as fh:
            for line in [next(fh, "") for _ in range(3)]:
                if line.startswith("id: "):
                    return line[4:].strip()
    except OSError:
        pass
    return None


def find_raw(root: Path, connector: str, doc_id: str) -> Path | None:
    """The raw file carrying DOC_ID, found by reading ids in its connector dir —
    for when the index has no row (a lost or not-yet-rebuilt tars.db)."""
    for path in sorted((root / RAW_DIR / slugify(connector)).glob("*.md")):
        if file_doc_id(path) == doc_id:
            return path
    return None


def raw_path_for(root: Path, doc: RawDoc, existing: str | None = None) -> Path:
    """Pick the raw file path. `existing` (relative path from the DB) wins so
    a document's filename — and every link to it — stays stable forever."""
    if existing:
        return root / existing
    conn_dir = root / RAW_DIR / slugify(doc.connector)
    base = slugify(doc.title) if doc.title else doc.id
    candidate = conn_dir / f"{base}.md"
    if candidate.exists():
        if file_doc_id(candidate) == doc.id:
            return candidate  # this doc already owns the name
        # Someone else's file — possibly under another Unicode form of the same
        # name, which APFS treats as this one: never write over it.
        return conn_dir / f"{base}-{doc.id[:6]}.md"
    # Collisions are checked across the whole vault, not just this connector
    # dir: a capture titled "Design Patterns" must not shadow the concept hub
    # of the same slug, or every [[design-patterns]] link becomes ambiguous.
    if any(unicodedata.normalize("NFC", f.stem) == base for f in linkable_files(root)):
        return conn_dir / f"{base}-{doc.id[:6]}.md"
    return candidate


class _DoubleQuoted(str):
    """Emitted as "…": Obsidian's own form for a link inside a property."""


class _Dumper(yaml.SafeDumper):
    """SafeDumper plus _DoubleQuoted, kept local: registering on yaml.SafeDumper
    itself would change every safe_dump in the process."""


_Dumper.add_representer(
    _DoubleQuoted, lambda dumper, data: dumper.represent_scalar(
        "tag:yaml.org,2002:str", str(data), style='"'))


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
    if doc.annotates:
        frontmatter["annotates"] = _DoubleQuoted(doc.annotates)
    if "annotates_id" in doc.meta:  # hex; quoted, or YAML 1.2 reads 0361… as a number
        frontmatter["meta"] = {**doc.meta, "annotates_id": _DoubleQuoted(doc.meta["annotates_id"])}
    header = yaml.dump(frontmatter, Dumper=_Dumper, sort_keys=False,
                       allow_unicode=True).strip()
    payload = f"---\n{header}\n---\n\n{render_body(doc)}\n"
    # Raw is truth: leave the file untouched (bytes AND mtime) when nothing changed,
    # so an index rebuild can never churn the archive. Written via temp file +
    # atomic rename so a concurrent reader (another `tars` process, Obsidian)
    # can never observe a half-written file.
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


def raw_files(content_md: Path) -> list[Path]:
    """A capture's files on disk: the content .md plus any source sidecar
    `write_raw` put next to it under the same stem."""
    return sorted(content_md.parent.glob(f"{content_md.stem}.*"))


class UnparseableRaw(ValueError):
    """A raw file that can't be read back as a document: unreadable, not UTF-8,
    or frontmatter that is missing, invalid YAML, or the wrong shape. The one
    error every reader catches to skip and name a bad file instead of crashing."""

    def __init__(self, path: Path, reason: str):
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _is_str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _frontmatter_problem(fm) -> str | None:
    """Why this frontmatter can't become a RawDoc, or None. Checks values, not
    just keys: a `connector: null` would otherwise get as far as the DB."""
    if not isinstance(fm, dict):
        return "frontmatter is not a mapping"
    for key in ("connector", "origin"):
        if not isinstance(fm.get(key), str) or not fm[key]:
            return f"`{key}` must be a non-empty string"
    if fm.get("title") is not None and not isinstance(fm["title"], str):
        return "`title` must be a string"
    for key in ("tags", "concepts"):
        if fm.get(key) is not None and not _is_str_list(fm[key]):
            return f"`{key}` must be a list of strings"
    if fm.get("meta") is not None and not isinstance(fm["meta"], dict):
        return "`meta` must be a mapping"
    if fm.get("annotates") is not None and not isinstance(fm["annotates"], str):
        return "`annotates` must be a single [[link]] string"
    if fm.get("captured_at") is not None and not isinstance(fm["captured_at"], (str, datetime)):
        return "`captured_at` must be a timestamp"
    return None


def read_raw(content_md: Path, v1: bool = False) -> RawDoc:
    """Parse one raw file. `v1=True` is for `migrate` only — every other command
    refuses a v1 vault, so outside it a file without a `concepts:` key is v2
    with no concepts, never a reason to fall back to the v1 body-line parse."""
    try:
        raw = content_md.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        raise UnparseableRaw(content_md, f"unreadable ({type(exc).__name__})") from exc
    if not raw.startswith("---\n"):
        raise UnparseableRaw(content_md, "missing frontmatter")
    header, _, body = raw[4:].partition("\n---\n")
    try:
        fm = yaml.safe_load(header)
    except yaml.YAMLError as exc:
        raise UnparseableRaw(content_md, "frontmatter is not valid YAML") from exc
    problem = _frontmatter_problem(fm)
    if problem:
        raise UnparseableRaw(content_md, problem)
    text = body.strip("\n")

    # The Concepts: line is a derived rendering — strip it back out of the body.
    # v2: only its exact rendering of the frontmatter concepts is derived; any
    # other first line (an unshelved note that starts "Concepts: ...") is content.
    # v1 (migrate only, no `concepts` key): the body line held the concepts.
    legacy = v1 and "concepts" not in fm
    line_concepts: list[str] = []
    if not legacy:
        first, _, rest = text.partition("\n")
        if fm.get("concepts") and first == concepts_line(fm["concepts"]):
            text = rest.lstrip("\n")
    elif text.startswith("Concepts: "):
        first, _, rest = text.partition("\n")
        line_concepts = re.findall(r"\[\[([^\]|]+)\]\]", first)
        text = rest.lstrip("\n")

    meta = fm.get("meta") or {}
    if not legacy:  # v2: frontmatter is the single truth
        concepts = fm.get("concepts") or []
    else:  # v1 compat: concepts lived in the body line and/or meta
        concepts = line_concepts or meta.pop("concepts", [])
        meta.pop("concepts", None)

    captured = fm.get("captured_at")
    if isinstance(captured, datetime):  # YAML parses unquoted timestamps eagerly
        captured = captured.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if "annotates" in meta and "annotates_id" not in meta:  # #13's key, before the rename
        meta["annotates_id"] = meta.pop("annotates")

    return RawDoc(
        connector=fm["connector"],
        origin=fm["origin"],
        text=text,
        title=fm.get("title"),
        captured_at=captured or now_iso(),
        tags=fm.get("tags") or [],
        concepts=concepts,
        meta=meta,
        annotates=fm.get("annotates"),
    )


def iter_raw(root: Path):
    yield from sorted((root / RAW_DIR).glob("*/*.md"))


# Layers whose filenames own a name in the vault's flat wiki-link namespace.
# Obsidian resolves [[stem]] by basename across the whole vault, ignoring
# directories, so a raw capture and a concept hub sharing a stem are two graph
# nodes competing for every link to that name. Digests are excluded: they link
# out, nothing links back at them.
LINKABLE_LAYERS = (CONCEPTS_DIR, PEOPLE_DIR, NOTES_DIR, TASKS_DIR)


def linkable_files(root: Path):
    """Every file that owns a stem in the flat [[wiki-link]] namespace."""
    yield from iter_raw(root)
    for layer in LINKABLE_LAYERS:
        yield from sorted((root / layer).glob("*.md"))


def slugify(title: str) -> str:
    # Compose first: an NFD title (macOS/Finder names) splits "ó" into "o" plus
    # a combining mark, which isn't alnum ("reunio-n"), and the NFC and NFD
    # forms of one title would name the same file on APFS without comparing equal.
    title = unicodedata.normalize("NFC", title)
    slug = "".join(c.lower() if c.isalnum() else "-" for c in title)
    slug = "-".join(part for part in slug.split("-") if part)
    if len(slug) > 80:
        # Trim at a word boundary — these names are permanent link targets,
        # and "…-transform-in-goo" (Google) reads like a different word.
        cut = slug[:80]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else cut
    return slug or "note"
