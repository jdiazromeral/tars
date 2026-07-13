"""The tars CLI. Agents call this; they never reimplement the pipeline."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import click

from . import db as database
from . import doctor as doctor_mod
from . import extract, hubs as hubs_mod, ingest, ingestlog, normalize as normalize_mod
from . import search as search_mod, store, view
from .connectors import CONNECTORS
from .store import RawDoc


def _open(start: Path | None = None):
    try:
        root = store.find_root(start)
        store.check_version(root)
    except (store.NotARootError, store.VaultVersionError) as exc:
        raise click.ClickException(str(exc))
    return root, database.connect(root)


@click.group()
@click.version_option()
def main():
    """TARS Answers from Raw Sources — local-first second brain."""


@main.command()
@click.argument("path", type=click.Path(path_type=Path), default=".")
def init(path: Path):
    """Create a TARS root (inbox/, raw/, wiki/, tasks/, digests/, log/, tars.db) at PATH."""
    store.init_root(path.resolve())
    db = database.connect(path.resolve())
    db.close()
    click.echo(f"initialized TARS v{store.SCHEMA_VERSION} root at {path.resolve()}")


@main.command()
@click.argument("target")
@click.option("--title", help="Override the extracted title.")
@click.option("--tag", "tags", multiple=True, help="Tag(s) to attach; repeatable.")
@click.option("--origin", help="Stable origin key (defaults per target type).")
@click.option("--connector", "connector_override",
              help="Record under this connector (for skill-fed sources, e.g. granola).")
@click.option("--concept", "concepts", multiple=True,
              help="Concept slug(s) this capture belongs to; repeatable. "
                   "Prepends a 'Concepts:' wiki-link line so the vault graph clusters.")
def add(target: str, title: str | None, tags: tuple[str, ...], origin: str | None,
        connector_override: str | None, concepts: tuple[str, ...]):
    """Capture TARGET: a URL, a file path, or '-' for pasted text on stdin."""
    root, db = _open()
    source_bytes = source_ext = None

    if target == "-":
        text = sys.stdin.read().strip()
        if not text:
            raise click.ClickException("stdin was empty")
        connector = "note"
        # Content-addressed by default so re-pasting the same text dedupes
        # (idempotence, like every synced connector's stable id). Pass an explicit
        # --origin to treat a note as a mutable slot you update in place instead.
        doc_origin = origin or f"note:{store.content_hash(text)[:12]}"
        extracted = extract.Extracted(text=text, title=title)
    elif target.startswith(("http://", "https://")):
        connector = "web"
        # Canonicalized so tracking params / fragments can't mint duplicates.
        doc_origin = origin or store.canonical_url(target)
        extracted = _try(lambda: extract.from_url(target))
    else:
        path = Path(target).expanduser()
        if not path.exists():
            raise click.ClickException(f"no such file: {target}")
        connector = "file"
        # Content-addressed by the file's raw bytes, so the same document dedupes
        # across paths/machines; the path is kept in meta as provenance, not identity.
        # Pass an explicit --origin to track a path as a mutable slot instead.
        doc_origin = origin or f"file:{store.content_hash(path.read_bytes())[:12]}"
        extracted = _try(lambda: extract.from_file(path))
        source_bytes, source_ext = extracted.source_bytes, extracted.source_ext

    doc = RawDoc(
        connector=connector_override or connector,
        origin=doc_origin,
        text=extracted.text,
        title=title or extracted.title,
        tags=list(tags),
        concepts=[store.slugify(c) for c in concepts],
        meta=extracted.meta,
    )
    doc_id, status = ingest.add(root, db, doc, source_bytes, source_ext)
    click.echo(f"{status}  {doc_id}  [{doc.connector}] {doc.title or doc_origin}")


def _try(fn):
    try:
        return fn()
    except extract.ExtractionError as exc:
        raise click.ClickException(str(exc))
    except Exception as exc:  # network errors, bad PDFs, ...
        raise click.ClickException(f"{type(exc).__name__}: {exc}")


@main.command()
@click.argument("query")
@click.option("-k", "limit", default=8, show_default=True, help="Max documents returned.")
@click.option("--connector", help="Restrict to one connector (web, file, note, ...).")
@click.option("--raw-match", is_flag=True, help="Pass QUERY straight to FTS5 (advanced syntax).")
@click.option("-v", "--chunk", "with_chunk", is_flag=True,
              help="Include each hit's best-matching chunk verbatim (~1.6k chars) — "
                   "usually enough to answer from without `tars show`-ing the whole doc.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def search(query: str, limit: int, connector: str | None, raw_match: bool,
           with_chunk: bool, as_json: bool):
    """Search the index; returns ranked documents with snippets and provenance."""
    _, db = _open()
    try:
        hits = search_mod.search(db, query, k=limit, connector=connector,
                                 raw_match=raw_match, with_chunk=with_chunk)
    except ValueError as exc:
        raise click.ClickException(str(exc))
    except Exception as exc:  # bad --raw-match syntax reaches sqlite directly
        raise click.ClickException(f"search failed: {exc}")
    if as_json:
        click.echo(json.dumps(
            [{k: v for k, v in vars(h).items() if v is not None} for h in hits],
            ensure_ascii=False))
        return
    if not hits:
        click.echo("no results")
        return
    for hit in hits:
        click.echo(f"{hit.doc_id}  [[{hit.file}]]  [{hit.connector}] {hit.title or hit.origin}")
        click.echo(f"    {hit.snippet}")
        click.echo(f"    ({hit.origin})")
        if hit.chunk:
            for line in hit.chunk.splitlines():
                click.echo(f"    | {line}")


@main.command()
@click.argument("doc_id")
@click.option("--path", "path_only", is_flag=True, help="Print the raw file path only.")
@click.option("--head", type=click.IntRange(min=1),
              help="Print only the first N lines (frontmatter + opening).")
@click.option("--grep", "pattern",
              help="Print only lines matching this case-insensitive regex, with context.")
@click.option("-C", "--context", default=3, show_default=True,
              help="Context lines around each --grep match.")
def show(doc_id: str, path_only: bool, head: int | None, pattern: str | None, context: int):
    """Print a captured document (frontmatter + full normalized text).

    A capture can be tens of thousands of tokens (meeting transcripts); --head
    and --grep carve out just the needed slice — reach for them before a full
    print. --grep output carries 1-based line numbers so a follow-up can aim
    wider (-C) or deeper at the same spot.
    """
    if sum((path_only, head is not None, pattern is not None)) > 1:
        raise click.ClickException("choose at most one of --path / --head / --grep")
    root, db = _open()
    row = db.execute("SELECT raw_dir FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        raise click.ClickException(f"no document with id {doc_id}")
    raw_path = root / row["raw_dir"]
    if path_only:
        click.echo(raw_path)
        return
    text = raw_path.read_text()
    if pattern is not None:
        try:
            click.echo(view.grep(text, pattern, context))
        except re.error as exc:
            raise click.ClickException(f"bad --grep pattern: {exc}")
    elif head is not None:
        click.echo(view.head(text, head))
    else:
        click.echo(text)


@main.command(name="list")
@click.option("--connector", help="Restrict to one connector (jira, granola, web, ...).")
@click.option("--since", help="Only documents captured at/after this ISO date or timestamp "
                              "(e.g. 2026-07-09 or 2026-07-09T08:00:00Z).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def list_(connector: str | None, since: str | None, as_json: bool):
    """List ingested documents (id, origin, title). Enumerate what a connector holds
    to refresh mutable sources or reconcile against what's currently in scope;
    --since scopes to what a sweep just landed without dumping the whole corpus."""
    _, db = _open()
    sql = "SELECT id, connector, origin, title, captured_at FROM documents"
    conditions, params = [], []
    if connector:
        conditions.append("connector = ?")
        params.append(connector)
    if since:
        conditions.append("captured_at >= ?")
        params.append(since)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY connector, origin"
    rows = db.execute(sql, tuple(params)).fetchall()
    if as_json:
        click.echo(json.dumps([dict(r) for r in rows], ensure_ascii=False))
        return
    for row in rows:
        click.echo(f"{row['id']}  [{row['connector']}] {row['origin']}  {row['title'] or ''}")


@main.command()
@click.argument("doc_id")
@click.option("--concept", "concepts", multiple=True, required=True,
              help="Concept slug(s) to attach; repeatable. Merges with existing ones.")
def tag(doc_id: str, concepts: tuple[str, ...]):
    """Attach concept wiki-links to an already-captured document (idempotent merge)."""
    root, db = _open()
    row = db.execute("SELECT raw_dir FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        raise click.ClickException(f"no document with id {doc_id}")
    doc = store.read_raw(root / row["raw_dir"])
    doc.concepts = [store.slugify(c) for c in concepts]
    _, status = ingest.add(root, db, doc)  # default merge unions with stored concepts
    merged = store.read_raw(root / row["raw_dir"]).concepts
    click.echo(f"{status}  {doc_id}  concepts: {', '.join(merged)}")


@main.command()
@click.argument("doc_id")
@click.option("--concept", "concepts", multiple=True, required=True,
              help="Concept slug(s) to remove; repeatable. Leaves the others intact.")
def untag(doc_id: str, concepts: tuple[str, ...]):
    """Remove concept wiki-links from a document (idempotent; the inverse of tag)."""
    root, db = _open()
    row = db.execute("SELECT raw_dir FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not row:
        raise click.ClickException(f"no document with id {doc_id}")
    doc = store.read_raw(root / row["raw_dir"])
    remove = {store.slugify(c) for c in concepts}
    doc.concepts = [slug for slug in doc.concepts if slug not in remove]
    _, status = ingest.add(root, db, doc, concepts_mode="replace")
    click.echo(f"{status}  {doc_id}  concepts: {', '.join(doc.concepts) or '(none)'}")


@main.command()
@click.argument("doc_id")
@click.option("--title", required=True, help="Title for the promoted note.")
def promote(doc_id: str, title: str):
    """Create a note skeleton in notes/ linked to DOC_ID; fill in the insight after."""
    root, db = _open()
    row = db.execute(
        "SELECT connector, origin, title, raw_dir FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if not row:
        raise click.ClickException(f"no document with id {doc_id}")
    source_stem = Path(row["raw_dir"]).stem
    note_path = root / store.NOTES_DIR / f"{store.slugify(title)}.md"
    if note_path.exists():
        raise click.ClickException(f"note already exists: {note_path}")
    note_path.write_text(
        f"""---
title: {title}
promoted_at: {store.now_iso()}
source_doc: {doc_id}
source_origin: {row['origin']}
source_connector: {row['connector']}
---

<!-- distilled insight goes here -->

Source: [[{source_stem}|{row['title'] or row['origin']}]]
""")
    click.echo(note_path)


@main.command()
@click.argument("connector", required=False)
def sync(connector: str | None):
    """Run a registered code connector (e.g. github). Lists them without args.

    Skill-mediated connectors (jira, gmail, granola, slack) sync through their
    `tars:sync-*` skills, not here — their only channel is an MCP the agent holds.
    """
    if not CONNECTORS:
        raise click.ClickException(
            "no synced connectors registered yet — see src/tars/connectors/__init__.py"
        )
    if connector is None:
        for name in sorted(CONNECTORS):
            click.echo(name)
        return
    if connector not in CONNECTORS:
        raise click.ClickException(f"unknown connector: {connector}")
    root, db = _open()
    cursor_row = db.execute(
        "SELECT cursor FROM sync_state WHERE connector = ?", (connector,)
    ).fetchone()
    try:
        new_cursor = CONNECTORS[connector](root, db, cursor_row["cursor"] if cursor_row else None)
    except RuntimeError as exc:  # scope/config/transport errors: no cursor persisted
        raise click.ClickException(str(exc))
    with db:
        db.execute(
            "INSERT INTO sync_state (connector, cursor, last_sync) VALUES (?, ?, ?) "
            "ON CONFLICT(connector) DO UPDATE SET cursor = excluded.cursor, "
            "last_sync = excluded.last_sync",
            (connector, new_cursor, store.now_iso()),
        )


@main.command()
@click.argument("connector")
@click.option("--set", "value", help="Set the watermark directly (manual / ad-hoc correction).")
@click.option("--begin", is_flag=True,
              help="Stamp now() into a pending watermark; call before an incremental sweep.")
@click.option("--commit", is_flag=True,
              help="Promote the pending watermark to live; call only after a sweep ingests cleanly.")
def cursor(connector: str, value: str | None, begin: bool, commit: bool):
    """Read or advance the sync watermark for a connector (used by skill-fed syncs).

    Two-phase advance keeps the watermark safe by construction: `--begin` stamps
    the sweep-start time into a pending slot, `--commit` promotes it only once the
    sweep has ingested everything. Ad-hoc pulls call neither, so they cannot move
    the watermark; a crash between the two leaves the live watermark untouched, so
    the next sweep simply re-scans from where it left off.
    """
    if sum((value is not None, begin, commit)) > 1:
        raise click.ClickException("choose exactly one of --set / --begin / --commit")
    _, db = _open()

    if begin:
        stamp = store.now_iso()
        with db:
            db.execute(
                "INSERT INTO sync_state (connector, pending_cursor) VALUES (?, ?) "
                "ON CONFLICT(connector) DO UPDATE SET pending_cursor = excluded.pending_cursor",
                (connector, stamp),
            )
        click.echo(stamp)
        return

    if commit:
        row = db.execute(
            "SELECT pending_cursor FROM sync_state WHERE connector = ?", (connector,)
        ).fetchone()
        if not row or not row["pending_cursor"]:
            raise click.ClickException(
                f"no pending watermark for {connector} — run `cursor {connector} --begin` first"
            )
        pending = row["pending_cursor"]
        with db:
            db.execute(
                "UPDATE sync_state SET cursor = ?, pending_cursor = NULL, last_sync = ? "
                "WHERE connector = ?",
                (pending, store.now_iso(), connector),
            )
        click.echo(pending)
        return

    if value is None:
        row = db.execute(
            "SELECT cursor FROM sync_state WHERE connector = ?", (connector,)
        ).fetchone()
        if row and row["cursor"]:
            click.echo(row["cursor"])
        return
    with db:
        db.execute(
            "INSERT INTO sync_state (connector, cursor, last_sync) VALUES (?, ?, ?) "
            "ON CONFLICT(connector) DO UPDATE SET cursor = excluded.cursor, "
            "last_sync = excluded.last_sync",
            (connector, value, store.now_iso()),
        )


@main.command()
def reindex():
    """Rebuild tars.db from raw/ (the DB is a disposable cache)."""
    root, db = _open()
    count = ingest.reindex(root, db)
    click.echo(f"reindexed {count} documents")


@main.command()
def migrate():
    """Upgrade the vault format in place (v1 → v2).

    v2 moves concepts into raw frontmatter (`concepts:`) as the single truth;
    the body `Concepts:` line becomes a derived rendering and stops counting
    toward content hashes. Rewrites every raw file, restamps the marker, and
    reindexes. Idempotent — a v2 vault is a no-op. Back up first.
    """
    try:
        root = store.find_root()
    except store.NotARootError as exc:
        raise click.ClickException(str(exc))
    found = store.vault_version(root)
    if found == store.SCHEMA_VERSION:
        click.echo(f"vault already at v{store.SCHEMA_VERSION} — nothing to do")
        return
    if found > store.SCHEMA_VERSION:
        raise click.ClickException(
            f"vault is v{found}, newer than this tool — upgrade the tool instead"
        )
    db = database.connect(root)
    rewritten = 0
    for content_md in store.iter_raw(root):
        doc = store.read_raw(content_md)  # v1-compat parse pulls concepts out of the body
        store.write_raw(root, doc, content_md)
        rewritten += 1
    (root / store.MARKER).write_text(f"version: {store.SCHEMA_VERSION}\n")
    count = ingest.reindex(root, db)
    click.echo(f"migrated vault at {root} to v{store.SCHEMA_VERSION}: "
               f"rewrote {rewritten} raw files, reindexed {count}")


@main.command()
def hubs():
    """Regenerate every concept hub's `## Sources` from shelving data.

    Hub membership is a derived view over the raw files' concepts — run this
    after tagging instead of hand-appending entries. Descriptions, `## Notes`,
    and hand-written relevance clauses on surviving entries are preserved.
    """
    root, db = _open()
    written, created = hubs_mod.regenerate(root, db)
    click.echo(f"hubs: {written} page(s) rewritten, {created} created")


@main.command(name="log")
@click.option("-n", "limit", default=20, show_default=True,
              help="Max events to show (0 = all).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def log_cmd(limit: int, as_json: bool):
    """Show the ingestion log: add / update / delete events, newest first."""
    root, _ = _open()
    entries = ingestlog.read_log(root)
    shown = entries[:limit] if limit else entries
    if as_json:
        click.echo(json.dumps(shown, ensure_ascii=False))
        return
    if not entries:
        click.echo("no ingestion events logged yet")
        return
    for e in shown:
        click.echo(f"{e['ts']}  {e['action']:8}  {e['id']}  "
                   f"[{e['connector']}] {e.get('title') or e.get('origin') or ''}")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def doctor(as_json: bool):
    """Check vault invariants: dangling links, unhubbed concepts, DB/raw drift.

    Read-only — reports problems and the fix command, never mutates anything.
    Exits non-zero when it finds issues, so it's scriptable.
    """
    root, db = _open()
    findings = doctor_mod.run(root, db)
    if as_json:
        click.echo(json.dumps([vars(f) for f in findings], ensure_ascii=False))
    elif not findings:
        click.echo("clean — no invariant violations found")
    else:
        for f in findings:
            click.echo(f"{f.check}  {f.path}  {f.detail}")
        click.echo(f"{len(findings)} issue(s) found")
    if findings:
        sys.exit(1)


@main.command()
def finalize():
    """Close a sync or edit batch: clear index drift, regenerate hubs, verify.

    The deterministic finishers every ingestion should end with, in one step —
    so `tars add`/`sync` and hand-edits don't leave the vault half-wired:

    \b
      1. reindex  — only when the DB has drifted from raw/ (keeps it cheap)
      2. hubs     — rebuild every concept hub's `## Sources` from shelving
      3. doctor   — re-check invariants

    Idempotent and safe to run anytime. Reindex runs before hubs so hubs derive
    from a current index. Exits non-zero if doctor still finds issues after the
    auto-fixes, so it stays scriptable.
    """
    root, db = _open()

    if any(f.check == "db-drift" for f in doctor_mod.run(root, db)):
        count = ingest.reindex(root, db)
        click.echo(f"reindex: {count} document(s) (drift cleared)")
    else:
        click.echo("reindex: skipped (no drift)")

    written, created = hubs_mod.regenerate(root, db)
    click.echo(f"hubs:    {written} rewritten, {created} created")

    findings = doctor_mod.run(root, db)
    if not findings:
        click.echo("doctor:  clean")
        return
    for f in findings:
        click.echo(f"  {f.check}  {f.path}  {f.detail}")
    click.echo(f"doctor:  {len(findings)} issue(s) remain")
    sys.exit(1)


@main.command()
@click.argument("doc_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def rm(doc_id: str, yes: bool):
    """Delete a captured document everywhere: raw file, sidecar source, index row.

    The sanctioned redaction path (an accidental capture, a pasted secret).
    Reports every wiki-link that still points at the deleted file; run
    `tars hubs` afterwards to drop it from concept pages.
    """
    root, db = _open()
    row = db.execute(
        "SELECT connector, raw_dir, title, origin FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if not row:
        raise click.ClickException(f"no document with id {doc_id}")
    raw_path = root / row["raw_dir"]
    stem = raw_path.stem
    targets = sorted(raw_path.parent.glob(f"{stem}.*"))
    if not yes:
        names = ", ".join(t.name for t in targets) or row["raw_dir"]
        click.confirm(f"delete {names} and its index entry?", abort=True)
    for target in targets:
        target.unlink(missing_ok=True)
    with db:
        db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    ingestlog.log_ingestion(root, action="deleted", doc_id=doc_id,
                            connector=row["connector"], origin=row["origin"], title=row["title"])
    click.echo(f"deleted  {doc_id}  [{row['origin']}] {row['title'] or ''}")
    for layer in (store.WIKI_DIR, store.TASKS_DIR, store.DIGESTS_DIR, store.RAW_DIR):
        for md in sorted((root / layer).rglob("*.md")):
            if f"[[{stem}" in md.read_text():
                click.echo(f"  still referenced in {md.relative_to(root)}")


def _inbox_title(path: Path, text: str) -> str:
    """Filename wins when it's meaningful; date-ish or generic names fall back
    to the note's first line."""
    stem = path.stem.strip()
    if re.fullmatch(r"[\d\-_. ]*", stem) or stem.lower() in {"note", "new note", "untitled"}:
        first = text.lstrip().splitlines()[0].lstrip("# ").strip()
        return first[:60] or stem or "inbox note"
    return stem.replace("-", " ").replace("_", " ")


@main.command()
def sweep():
    """Ingest every text file dropped in inbox/ as a note, then remove it.

    inbox/ is the zero-ceremony landing zone: anything that can write a file
    there (a phone folder-sync, a folder action, `cat >>`) is a capture path.
    Sweep is plumbing — origins are content-addressed so the same drop never
    duplicates; shelving stays the agent's job afterwards (`tars tag` + hubs).
    """
    root, db = _open()
    inbox = root / store.INBOX_DIR
    inbox.mkdir(exist_ok=True)
    swept = skipped = 0
    for f in sorted(p for p in inbox.iterdir() if p.is_file()):
        if f.name.startswith("."):
            continue
        if f.suffix.lower() not in {".md", ".txt", ""}:
            click.echo(f"skipped  {f.name}  (not plain text — capture it with `tars add`)")
            skipped += 1
            continue
        text = f.read_text(errors="replace").strip()
        if not text:
            f.unlink()  # an empty drop carries nothing — just clear it
            continue
        doc = RawDoc(
            connector="note",
            origin=f"note:{store.content_hash(text)[:12]}",
            text=text,
            title=_inbox_title(f, text),
            meta={"source": "inbox", "inbox_file": f.name},
        )
        doc_id, status = ingest.add(root, db, doc)
        f.unlink()
        swept += 1
        click.echo(f"{status}  {doc_id}  {f.name} → {doc.title}")
    click.echo(f"swept {swept} file(s)" + (f", {skipped} skipped" if skipped else ""))


@main.command()
@click.argument("dest", type=click.Path(path_type=Path), required=False)
@click.option("--keep", type=click.IntRange(min=1),
              help="Prune after writing: keep only the newest N bundles in DEST.")
def backup(dest: Path | None, keep: int | None):
    """Write a full git bundle of the vault to DEST (or $TARS_BACKUP_DIR).

    The vault is local-only by design; bundles are the off-machine escape
    hatch — copy them to an encrypted disk or private storage. Restore with
    `git clone <bundle> <vault-dir>`.
    """
    import os
    import subprocess
    from datetime import datetime

    root, _ = _open()
    if dest is None:
        env = os.environ.get("TARS_BACKUP_DIR")
        if not env:
            raise click.ClickException("pass DEST or set TARS_BACKUP_DIR")
        dest = Path(env)
    if not (root / ".git").exists():
        raise click.ClickException(
            f"vault at {root} is not a git repo — `git init` and commit it first"
        )
    if subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                      capture_output=True, text=True).stdout.strip():
        click.echo("warning: vault has uncommitted changes — they will NOT be in the bundle",
                   err=True)
    dest = dest.expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    bundle = dest / f"tars-vault-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bundle"
    result = subprocess.run(["git", "-C", str(root), "bundle", "create",
                             str(bundle), "--all"], capture_output=True, text=True)
    if result.returncode != 0:
        raise click.ClickException(f"git bundle failed: {result.stderr.strip()}")
    if keep:
        for old in sorted(dest.glob("tars-vault-*.bundle"))[:-keep]:
            old.unlink()
            click.echo(f"pruned {old.name}", err=True)
    click.echo(bundle)


@main.command()
def normalize():
    """Apply vocab.yml canonicalization to every raw doc + reindex (fixes capture typos)."""
    root, db = _open()
    rules = normalize_mod.load_rules(root)
    if not rules:
        raise click.ClickException(f"no {normalize_mod.VOCAB_FILE} at {root} — nothing to do")
    changed = 0
    for content_md in store.iter_raw(root):
        doc = store.read_raw(content_md)
        before = doc.text
        if normalize_mod.apply(before, rules, doc.connector) != before:
            _, status = ingest.add(root, db, doc)  # add() re-applies + rewrites raw + reindexes
            if status != "unchanged":
                changed += 1
                click.echo(f"  normalized {doc.id}  {content_md.name}")
    click.echo(f"normalized {changed} document(s)")


@main.command()
def status():
    """Corpus overview: documents per connector, notes, sync state."""
    root, db = _open()
    click.echo(f"root: {root}")
    click.echo(f"vault format: v{store.vault_version(root)}")
    rows = db.execute(
        "SELECT connector, COUNT(*) AS n FROM documents GROUP BY connector ORDER BY n DESC"
    ).fetchall()
    total = sum(r["n"] for r in rows)
    click.echo(f"documents: {total}")
    for row in rows:
        click.echo(f"  {row['connector']}: {row['n']}")
    notes = list((root / store.NOTES_DIR).glob("*.md"))
    click.echo(f"notes: {len(notes)}")
    for row in db.execute("SELECT connector, last_sync FROM sync_state ORDER BY connector"):
        click.echo(f"sync {row['connector']}: last {row['last_sync']}")
