"""The tars CLI. Agents call this; they never reimplement the pipeline."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import click

from . import backup as backup_mod
from . import db as database
from . import doctor as doctor_mod
from . import extract, inbox, ingest, ingestlog, store, syncstate, view
from . import hubs as hubs_mod
from . import normalize as normalize_mod
from . import search as search_mod
from .connectors import CONNECTORS
from .connectors import slack as slack_mod
from .store import RawDoc


def _open(start: Path | None = None):
    try:
        root = store.find_root(start)
        store.check_version(root)
    except (store.NotARootError, store.VaultVersionError) as exc:
        raise click.ClickException(str(exc))
    return root, database.connect(root)


def _doc_row(db, doc_id: str):
    """The index row (connector, origin, title, raw_dir) for DOC_ID, or a clean CLI error."""
    row = db.execute(
        "SELECT connector, origin, title, raw_dir FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()
    if not row:
        raise click.ClickException(f"no document with id {doc_id}")
    return row


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
@click.option("--append", "append", is_flag=True,
              help="Append the text to the end of the existing document with this "
                   "--origin (read + write under one DB lock), keeping the title and "
                   "tags it doesn't restate. Fails if no document has that origin.")
@click.option("--create", "create", is_flag=True,
              help="With --append: start the document if it doesn't exist yet "
                   "(e.g. the first entry of the day).")
def add(target: str, title: str | None, tags: tuple[str, ...], origin: str | None,
        connector_override: str | None, concepts: tuple[str, ...], append: bool,
        create: bool):
    """Capture TARGET: a URL, a file path, or '-' for pasted text on stdin.

    Your words are never replaced: re-adding different text to an existing
    note/agent/activity document is refused — use --append to add to it.
    """
    if append and not origin:
        raise click.ClickException("--append requires --origin (the document to append to)")
    if create and not append:
        raise click.ClickException("--create only applies with --append")
    if append and target != "-":
        raise click.ClickException("--append takes text on stdin ('-'), not a file or URL")
    root, db = _open()
    resolved = _try(lambda: ingest.resolve_target(target, stdin=sys.stdin, origin=origin))
    extracted = resolved.extracted
    doc = RawDoc(
        connector=connector_override or resolved.connector,
        origin=resolved.origin,
        text=extracted.text,
        title=title or extracted.title,
        tags=list(tags),
        concepts=[store.slugify(c) for c in concepts],
        meta=extracted.meta,
    )
    try:
        doc_id, status = ingest.add(root, db, doc, extracted.source_bytes,
                                    extracted.source_ext, append=append,
                                    create=create or not append)
    except store.UnparseableRaw as exc:  # appending to a doc whose raw file is broken
        raise click.ClickException(f"{exc} — repair it before writing to this document")
    except ingest.WouldReplace as exc:
        raise click.ClickException(
            f"{exc}; your words are never replaced — add to it with --append, "
            f"or capture a new note with a different --origin")
    except ingest.NoSuchDocument as exc:
        raise click.ClickException(
            f"{exc} to append to — check the origin (tars list), or pass --create "
            f"to start it")
    click.echo(f"{status}  {doc_id}  [{doc.connector}] {doc.title or doc.origin}")


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
    row = _doc_row(db, doc_id)
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
    status, merged = _shelve(root, db, doc_id,
                             add_concepts=[store.slugify(c) for c in concepts])
    click.echo(f"{status}  {doc_id}  concepts: {', '.join(merged)}")


@main.command()
@click.argument("doc_id")
@click.option("--concept", "concepts", multiple=True, required=True,
              help="Concept slug(s) to remove; repeatable. Leaves the others intact.")
def untag(doc_id: str, concepts: tuple[str, ...]):
    """Remove concept wiki-links from a document (idempotent; the inverse of tag)."""
    root, db = _open()
    status, left = _shelve(root, db, doc_id,
                           remove_concepts=[store.slugify(c) for c in concepts])
    click.echo(f"{status}  {doc_id}  concepts: {', '.join(left) or '(none)'}")


def _shelve(root, db, doc_id: str, **change):
    try:
        return ingest.shelve(root, db, doc_id, **change)
    except ingest.NoSuchDocument:
        raise click.ClickException(f"no document with id {doc_id}")
    except store.UnparseableRaw as exc:
        raise click.ClickException(f"{exc} — repair it before shelving this document")


@main.command()
@click.argument("doc_id")
@click.option("--title", required=True, help="Title for the promoted note.")
def promote(doc_id: str, title: str):
    """Create a note skeleton in notes/ linked to DOC_ID; fill in the insight after."""
    root, db = _open()
    row = _doc_row(db, doc_id)
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
    try:
        new_cursor = CONNECTORS[connector](root, db, syncstate.get_cursor(db, connector))
    except RuntimeError as exc:  # scope/config/transport errors: no cursor persisted
        raise click.ClickException(str(exc))
    syncstate.set_cursor(db, connector, new_cursor)


@main.command()
@click.argument("connector")
@click.option("--set", "value", help="Set the watermark directly (manual / ad-hoc correction).")
@click.option("--begin", is_flag=True,
              help="Stamp now() into a pending watermark; call before an incremental sweep.")
@click.option("--commit", is_flag=True,
              help="Promote the pending watermark to live; "
                   "call only after a sweep ingests cleanly.")
def cursor(connector: str, value: str | None, begin: bool, commit: bool):
    """Read or advance the sync watermark for a connector (used by skill-fed syncs).

    Two-phase advance keeps the watermark safe by construction: `--begin` stamps
    the sweep-start time into a pending slot, `--commit` promotes it only once the
    sweep has ingested everything. Ad-hoc pulls call neither, so they cannot move
    the watermark; a crash between the two leaves the live watermark untouched, so
    the next sweep simply re-scans from where it left off.

    CONNECTOR is a free-form key: connectors that sweep several scopes keep one
    watermark per scope with a namespaced key (e.g. `slack/<CHANNEL_ID>`), so
    each scope brackets independently and one failure never stalls the rest.
    """
    if sum((value is not None, begin, commit)) > 1:
        raise click.ClickException("choose exactly one of --set / --begin / --commit")
    _, db = _open()
    if begin:
        click.echo(syncstate.begin(db, connector))
    elif commit:
        try:
            click.echo(syncstate.commit(db, connector))
        except LookupError as exc:
            raise click.ClickException(f"{exc} — run `cursor {connector} --begin` first")
    elif value is not None:
        syncstate.set_cursor(db, connector, value)
    elif current := syncstate.get_cursor(db, connector):
        click.echo(current)


@main.command()
def reindex():
    """Rebuild tars.db from raw/ (the DB is a disposable cache)."""
    root, db = _open()
    count, unparseable = ingest.reindex(root, db)
    click.echo(f"reindexed {count} documents")
    _exit_if_unparseable(root, unparseable)


def _exit_if_unparseable(root: Path, unparseable: list[tuple[Path, str]],
                         rerun: str = "tars reindex") -> None:
    """Name every raw file a pass had to skip, then exit non-zero."""
    if not unparseable:
        return
    for path, reason in unparseable:
        click.echo(f"  unparseable  {path.relative_to(root)}  {reason} — skipped")
    click.echo(f"{len(unparseable)} raw file(s) skipped; repair them and re-run `{rerun}`")
    sys.exit(1)


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
    rewritten, unparseable = 0, []
    for content_md in store.iter_raw(root):
        try:
            doc = store.read_raw(content_md, v1=True)  # pulls concepts out of the body line
        except store.UnparseableRaw as exc:
            unparseable.append((content_md, exc.reason))
            continue
        store.write_raw(root, doc, content_md)
        rewritten += 1
    if unparseable:  # the marker stays at the old version until every file is migrated
        click.echo(f"rewrote {rewritten} raw files; vault left at v{found}")
        _exit_if_unparseable(root, unparseable, rerun="tars migrate")
    (root / store.MARKER).write_text(f"version: {store.SCHEMA_VERSION}\n")
    count, unparseable = ingest.reindex(root, db)
    click.echo(f"migrated vault at {root} to v{store.SCHEMA_VERSION}: "
               f"rewrote {rewritten} raw files, reindexed {count}")
    _exit_if_unparseable(root, unparseable)


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
      1. reindex  — only when the DB has drifted from raw/, naming each
                    drifted file first (a hand-edit stays visible)
      2. hubs     — rebuild every concept hub's `## Sources` from shelving
      3. doctor   — re-check invariants

    Idempotent and safe to run anytime. Reindex runs before hubs so hubs derive
    from a current index. Exits non-zero if doctor still finds issues after the
    auto-fixes, so it stays scriptable.
    """
    root, db = _open()

    drift = [f for f in doctor_mod.db_drift(root, db) if f.check == "db-drift"]
    if drift:
        # Name every drifted file before reindex absorbs it: a stale content
        # hash is usually a hand-edit to raw/, and a bare count would hide it.
        for f in drift:
            click.echo(f"  {f.path}  {f.detail.removesuffix(' — run `tars reindex`')}")
        # Unparseable files resurface as doctor findings below, so the list isn't echoed twice.
        count, _ = ingest.reindex(root, db)
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
    row = _doc_row(db, doc_id)
    raw_path = root / row["raw_dir"]
    if not yes:
        names = ", ".join(t.name for t in store.raw_files(raw_path)) or row["raw_dir"]
        click.confirm(f"delete {names} and its index entry?", abort=True)
    removed = ingest.remove(root, db, doc_id)
    click.echo(f"deleted  {doc_id}  [{removed['origin']}] {removed['title'] or ''}")
    for ref in doctor_mod.references_to(root, raw_path.stem):
        click.echo(f"  still referenced in {ref}")


@main.command()
def sweep():
    """Ingest every text file dropped in inbox/ as a note, then remove it.

    inbox/ is the zero-ceremony landing zone: anything that can write a file
    there (a phone folder-sync, a folder action, `cat >>`) is a capture path.
    Sweep is plumbing — origins are content-addressed so the same drop never
    duplicates; shelving stays the agent's job afterwards (`tars tag` + hubs).
    """
    root, db = _open()
    drops = inbox.sweep(root, db)
    for d in drops:
        if d.status == "skipped":
            click.echo(f"skipped  {d.file}  (not plain text — capture it with `tars add`)")
        elif d.status == "kept":
            click.echo(f"kept     {d.file}  left in inbox/: {d.title}")
        elif d.status != "empty":
            click.echo(f"{d.status}  {d.doc_id}  {d.file} → {d.title}")
    swept = sum(d.status not in ("skipped", "empty", "kept") for d in drops)
    skipped = sum(d.status in ("skipped", "kept") for d in drops)
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
    root, _ = _open()
    if dest is None:
        env = os.environ.get("TARS_BACKUP_DIR")
        if not env:
            raise click.ClickException("pass DEST or set TARS_BACKUP_DIR")
        dest = Path(env)
    dest = dest.expanduser()
    if backup_mod.has_uncommitted_changes(root):
        click.echo("warning: vault has uncommitted changes — they will NOT be in the bundle",
                   err=True)
    try:
        bundle = backup_mod.create_bundle(root, dest)
    except backup_mod.BackupError as exc:
        raise click.ClickException(str(exc))
    for old in backup_mod.prune(dest, keep) if keep else []:
        click.echo(f"pruned {old.name}", err=True)
    click.echo(bundle)


@main.command()
def normalize():
    """Apply vocab.yml canonicalization to every raw doc + reindex (fixes capture typos)."""
    root, db = _open()
    rules = normalize_mod.load_rules(root)
    if not rules:
        raise click.ClickException(f"no {normalize_mod.VOCAB_FILE} at {root} — nothing to do")
    changed, unparseable = 0, []
    for content_md in store.iter_raw(root):
        try:
            # cheap check first: take the write lock only for files that will change
            doc = store.read_raw(content_md)
            if normalize_mod.apply(doc.text, rules, doc.connector) == doc.text:
                continue
            status = ingest.renormalize(root, db, content_md, rules)
        except store.UnparseableRaw as exc:
            unparseable.append((content_md, exc.reason))
            continue
        if status == "updated":
            changed += 1
            click.echo(f"  normalized  {content_md.relative_to(root)}")
    click.echo(f"normalized {changed} document(s)")
    _exit_if_unparseable(root, unparseable, rerun="tars normalize")


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


@main.group()
def slack():
    """Slack sweep helpers. Selection is code; the MCP does the transport."""


@slack.command("select")
@click.option("--channel", required=True,
              help="Channel ID being swept (checked against the allowlist).")
@click.option("--channel-type", default="public_channel", show_default=True,
              help="public_channel | private_channel | mpim | im.")
def slack_select(channel: str, channel_type: str):
    """Pick which messages of a fetched window become captured threads.

    Reads `conversations.history` JSON on stdin (a list, or an object with a
    `messages` key) and writes the selection as JSON on stdout, so the skill
    never has to apply the rules by reading English.
    """
    root, _ = _open()
    try:
        cfg = slack_mod.load_config(root)
        slack_mod.validate_channel(channel, channel_type, cfg)
    except RuntimeError as exc:
        raise click.ClickException(str(exc))
    payload = json.loads(sys.stdin.read() or "[]")
    messages = payload.get("messages", []) if isinstance(payload, dict) else payload
    report = slack_mod.select(messages, cfg)
    click.echo(json.dumps({
        "selected": [vars(s) for s in report.selected],
        "skipped": report.skipped,
        "truncated": report.truncated,
    }, indent=2))
