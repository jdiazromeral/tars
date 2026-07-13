import json
import subprocess

import pytest
from click.testing import CliRunner

from tars import db, store
from tars.cli import main


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


def test_init_creates_layout(root):
    path, _ = root
    assert (path / ".tars").exists()
    assert (path / "raw").is_dir()
    assert (path / "wiki/notes").is_dir()
    assert (path / "wiki/concepts").is_dir()
    assert (path / "wiki/people").is_dir()
    assert (path / "tasks").is_dir()
    assert (path / "digests").is_dir()
    assert (path / "tars.db").exists()


def test_init_stamps_schema_version(root):
    path, _ = root
    assert f"version: {store.SCHEMA_VERSION}" in (path / ".tars").read_text()
    assert store.vault_version(path) == store.SCHEMA_VERSION


def test_empty_marker_reads_as_v1_and_requires_migration(root):
    # The marker began as an empty sentinel — that reads as v1, and a v1 vault
    # must be migrated before this tool writes to it.
    path, runner = root
    (path / ".tars").write_text("")
    assert store.vault_version(path) == 1
    result = runner.invoke(main, ["status"])
    assert result.exit_code != 0
    assert "tars migrate" in result.output


def test_newer_vault_is_refused(root):
    path, runner = root
    (path / ".tars").write_text(f"version: {store.SCHEMA_VERSION + 1}\n")
    result = runner.invoke(main, ["status"])
    assert result.exit_code != 0
    assert "upgrade the tool" in result.output


def test_tars_home_without_marker_is_refused(tmp_path, monkeypatch):
    # TARS_HOME must be validated like the cwd-walk: pointing it at an
    # unrelated existing directory must refuse, not silently adopt it — and
    # critically, `tars migrate` must not be able to stamp a marker/db there
    # (that was the actual bug: the old error message told you to run
    # `migrate`, which then created a bogus vault in the wrong directory).
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()

    result = runner.invoke(main, ["status"])
    assert result.exit_code != 0
    assert "tars init" in result.output
    assert not (tmp_path / ".tars").exists()

    migrated = runner.invoke(main, ["migrate"])
    assert migrated.exit_code != 0
    assert not (tmp_path / ".tars").exists()
    assert not (tmp_path / "tars.db").exists()


def test_add_note_and_search(root):
    path, runner = root
    text = "We removed the generic invitations user because loyalty points caused performance issues."
    result = runner.invoke(main, ["add", "-", "--title", "invitations user removal",
                                  "--origin", "note:test-1"], input=text)
    assert result.exit_code == 0, result.output
    assert result.output.startswith("added")

    result = runner.invoke(main, ["search", "why invitations loyalty performance"])
    assert result.exit_code == 0, result.output
    assert "invitations user removal" in result.output

    # raw archive holds the frontmatter-wrapped truth, named by human title
    content = path / "raw/note/invitations-user-removal.md"
    assert content.exists()
    assert "origin: note:test-1" in content.read_text()


def test_ingest_is_idempotent(root):
    _, runner = root
    args = ["add", "-", "--origin", "note:same"]
    first = runner.invoke(main, args, input="same text")
    second = runner.invoke(main, args, input="same text")
    changed = runner.invoke(main, args, input="different text now")
    assert first.output.startswith("added")
    assert second.output.startswith("unchanged")
    assert changed.output.startswith("updated")


def test_concurrent_tag_merge_does_not_lose_updates(root):
    # Two "sessions" (separate connections, like two agent processes running
    # `tars tag` at the same time) merge different concepts into the same
    # document concurrently. ingest.add's BEGIN IMMEDIATE serializes the
    # read-merge-write, so every writer sees the previous writer's committed
    # state before merging — without it, the second writer's merge is based on
    # a stale read and can silently drop the first writer's concept. This is
    # deterministic (not a sleep-based race): SQLite only ever lets one write
    # transaction be in flight, so whichever writer commits second necessarily
    # reads the first one's result.
    import threading

    from tars import db as database, ingest

    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:race", "--title", "race"],
                  input="body")

    def tag_with(concept: str) -> None:
        conn = database.connect(path)
        doc = store.read_raw(path / "raw/note/race.md")
        doc.concepts = [concept]
        ingest.add(path, conn, doc)
        conn.close()

    threads = [threading.Thread(target=tag_with, args=(c,)) for c in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    final = store.read_raw(path / "raw/note/race.md")
    assert set(final.concepts) == {"alpha", "beta"}


def test_note_origin_is_content_addressed(root):
    _, runner = root
    thought = "a durable thought worth keeping verbatim"
    first = runner.invoke(main, ["add", "-"], input=thought)
    second = runner.invoke(main, ["add", "-"], input=thought)
    third = runner.invoke(main, ["add", "-"], input="a different thought entirely")
    assert first.output.startswith("added")
    assert second.output.startswith("unchanged")  # same text -> same origin -> no dupe
    assert third.output.startswith("added")


def test_list_documents(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:PROJ-1",
                         "--title", "PROJ-1 thing"], input="body")
    runner.invoke(main, ["add", "-", "--origin", "note:n1"], input="a note")

    all_docs = runner.invoke(main, ["list"])
    assert "jira:PROJ-1" in all_docs.output
    assert "note:n1" in all_docs.output

    only_jira = runner.invoke(main, ["list", "--connector", "jira", "--json"])
    assert only_jira.exit_code == 0, only_jira.output
    payload = json.loads(only_jira.output)
    assert [d["origin"] for d in payload] == ["jira:PROJ-1"]
    assert payload[0]["title"] == "PROJ-1 thing"


def test_file_origin_is_content_addressed(root, tmp_path_factory):
    _, runner = root
    docs = tmp_path_factory.mktemp("docs")
    original = docs / "q3-plan.md"
    original.write_text("# Q3 plan\nship the exporter weekly")
    copy = docs / "elsewhere" / "renamed.md"
    copy.parent.mkdir()
    copy.write_text("# Q3 plan\nship the exporter weekly")  # identical bytes, different path

    first = runner.invoke(main, ["add", str(original)])
    second = runner.invoke(main, ["add", str(copy)])
    assert first.output.startswith("added"), first.output
    assert second.output.startswith("unchanged"), second.output  # same bytes -> dedupe across paths

    listing = json.loads(runner.invoke(main, ["list", "--connector", "file", "--json"]).output)
    assert len(listing) == 1
    assert listing[0]["origin"].startswith("file:")

    # the path survives as provenance in meta, just not as identity
    shown = runner.invoke(main, ["show", listing[0]["id"]])
    assert str(original.resolve()) in shown.output


def test_add_local_file(root, tmp_path_factory):
    _, runner = root
    doc = tmp_path_factory.mktemp("docs") / "meeting.md"
    doc.write_text("# Sync with data team\nDecided to keep snowflake exports weekly.")
    result = runner.invoke(main, ["add", str(doc)])
    assert result.exit_code == 0, result.output

    result = runner.invoke(main, ["search", "snowflake exports weekly", "--json"])
    assert result.exit_code == 0
    assert "meeting" in result.output


def test_reindex_rebuilds_from_raw(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:keep"], input="kubernetes migration rationale")
    (path / "tars.db").unlink()

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 0, result.output
    assert "reindexed 1" in result.output

    result = runner.invoke(main, ["search", "kubernetes migration"])
    assert "note:keep" in result.output


def test_reindex_keeps_filenames_stable_after_title_change(root):
    # A raw filename is fixed at first ingest; a title change followed by an
    # index rebuild must not mint a second file under the new slug.
    path, runner = root
    runner.invoke(main, ["add", "-", "--title", "Weekly Sync", "--origin", "note:w1"],
                  input="first content")
    runner.invoke(main, ["add", "-", "--title", "Weekly Sync Renamed", "--origin", "note:w1"],
                  input="second content")
    original = path / "raw/note/weekly-sync.md"
    assert original.exists()

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 0, result.output
    assert "reindexed 1" in result.output
    assert sorted(p.name for p in path.glob("raw/note/*.md")) == ["weekly-sync.md"]

    # the DB still points at the original file
    listing = json.loads(runner.invoke(main, ["list", "--json"]).output)
    shown = runner.invoke(main, ["show", listing[0]["id"], "--path"])
    assert shown.output.strip() == str(original)


def test_reindex_leaves_unchanged_raw_files_untouched(root):
    # Raw is truth, the DB is a cache: rebuilding the cache must not rewrite
    # the archive (identical bytes, untouched mtime).
    import os

    path, runner = root
    runner.invoke(main, ["add", "-", "--title", "t", "--origin", "note:m"], input="body")
    doc = path / "raw/note/t.md"
    before_bytes, before_mtime = doc.read_bytes(), os.stat(doc).st_mtime_ns

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 0, result.output
    assert doc.read_bytes() == before_bytes
    assert os.stat(doc).st_mtime_ns == before_mtime


def test_promote_creates_linked_note(root):
    path, runner = root
    add = runner.invoke(main, ["add", "-", "--origin", "note:p"], input="raw decision context")
    doc_id = add.output.split()[1]

    result = runner.invoke(main, ["promote", doc_id, "--title", "Why we did the thing"])
    assert result.exit_code == 0, result.output
    note = path / "wiki/notes" / "why-we-did-the-thing.md"
    assert note.exists()
    body = note.read_text()
    assert f"source_doc: {doc_id}" in body
    assert "source_origin: note:p" in body
    assert f"Source: [[{doc_id}|" in body  # Obsidian backlink to the raw doc


def test_connector_override_and_cursor(root):
    path, runner = root
    result = runner.invoke(
        main,
        ["add", "-", "--connector", "granola", "--origin", "granola:m-1",
         "--title", "2026-07-01 sprint review", "--tag", "meeting"],
        input="# sprint review\n\n## Transcript\nwe agreed to ship the exporter",
    )
    assert result.exit_code == 0, result.output
    assert "[granola]" in result.output
    assert list(path.glob("raw/granola/*.md"))

    assert runner.invoke(main, ["cursor", "granola"]).output == ""
    assert runner.invoke(main, ["cursor", "granola", "--set", "2026-07-01T10:00:00Z"]).exit_code == 0
    assert runner.invoke(main, ["cursor", "granola"]).output.strip() == "2026-07-01T10:00:00Z"

    status = runner.invoke(main, ["status"])
    assert "granola" in status.output


def test_cursor_two_phase_advance(root):
    _, runner = root

    # --begin stamps a pending watermark and prints it; the live cursor stays empty
    begin = runner.invoke(main, ["cursor", "jira", "--begin"])
    assert begin.exit_code == 0, begin.output
    stamp = begin.output.strip()
    assert stamp.endswith("Z")
    assert runner.invoke(main, ["cursor", "jira"]).output.strip() == ""

    # --commit promotes pending -> live and echoes the committed value
    commit = runner.invoke(main, ["cursor", "jira", "--commit"])
    assert commit.exit_code == 0, commit.output
    assert commit.output.strip() == stamp
    assert runner.invoke(main, ["cursor", "jira"]).output.strip() == stamp

    # pending is cleared on commit: a second --commit has nothing to promote
    again = runner.invoke(main, ["cursor", "jira", "--commit"])
    assert again.exit_code != 0
    assert "no pending" in again.output

    # a --begin that is never committed (crash mid-sweep) leaves the live watermark intact
    assert runner.invoke(main, ["cursor", "jira", "--begin"]).exit_code == 0
    assert runner.invoke(main, ["cursor", "jira"]).output.strip() == stamp

    # the phases are mutually exclusive
    bad = runner.invoke(main, ["cursor", "jira", "--begin", "--set", "2026-01-01T00:00:00Z"])
    assert bad.exit_code != 0


def test_concepts_and_slug_collision(root):
    path, runner = root
    result = runner.invoke(
        main,
        ["add", "-", "--title", "Weekly Sync", "--origin", "note:w1",
         "--concept", "generic service", "--concept", "platform-team"],
        input="we discussed the glossary rollout",
    )
    assert result.exit_code == 0, result.output
    doc = path / "raw/note/weekly-sync.md"
    assert doc.exists()
    assert "Concepts: [[generic-service]] [[platform-team]]" in doc.read_text()

    # same title, different origin -> distinct file with id suffix, no clobber
    other = runner.invoke(main, ["add", "-", "--title", "Weekly Sync",
                                 "--origin", "note:w2"], input="a different meeting")
    assert other.output.startswith("added")
    files = sorted(p.name for p in path.glob("raw/note/weekly-sync*.md"))
    assert len(files) == 2
    assert "we discussed the glossary rollout" in doc.read_text()  # first file intact

    # filename is fixed at first ingest: content update must not move the file
    runner.invoke(main, ["add", "-", "--title", "Weekly Sync renamed",
                         "--origin", "note:w1"], input="updated content")
    assert "updated content" in doc.read_text()
    assert not (path / "raw/note/weekly-sync-renamed.md").exists()

    # tars tag merges concepts idempotently after the fact — and the content
    # update above must NOT have wiped the shelving (concepts are not content)
    doc_id = doc.read_text().split("id: ")[1].split("\n")[0]
    tag = runner.invoke(main, ["tag", doc_id, "--concept", "platform-team",
                               "--concept", "atlas"])
    assert tag.exit_code == 0, tag.output
    first_line = doc.read_text().split("---\n\n")[1].split("\n")[0]
    assert first_line == "Concepts: [[generic-service]] [[platform-team]] [[atlas]]"


def test_untag_removes_concepts(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--title", "Roadmap thing", "--origin", "note:r",
                         "--concept", "atlas", "--concept", "proj-q3-2026"],
                  input="some work")
    doc = path / "raw/note/roadmap-thing.md"
    doc_id = doc.read_text().split("id: ")[1].split("\n")[0]

    # remove one concept, keep the other
    r = runner.invoke(main, ["untag", doc_id, "--concept", "atlas"])
    assert r.exit_code == 0, r.output
    first_line = doc.read_text().split("---\n\n")[1].split("\n")[0]
    assert first_line == "Concepts: [[proj-q3-2026]]"

    # removing the last concept drops the line entirely
    r2 = runner.invoke(main, ["untag", doc_id, "--concept", "proj-q3-2026"])
    assert r2.exit_code == 0, r2.output
    assert not doc.read_text().split("---\n\n")[1].startswith("Concepts:")

    # removing a concept that isn't there is harmless
    r3 = runner.invoke(main, ["untag", doc_id, "--concept", "atlas"])
    assert r3.exit_code == 0, r3.output


def test_normalization_applies_on_ingest(root):
    path, runner = root
    (path / "vocab.yml").write_text("AcmeCloud: [AcneCloud]\nAcme: [Acne]\n")
    r = runner.invoke(main, ["add", "-", "--origin", "note:v"],
                      input="Shipped AcneCloud; it is a Acne thing")
    assert r.exit_code == 0, r.output
    content = next(path.glob("raw/note/*.md")).read_text()
    assert "AcmeCloud" in content and "AcneCloud" not in content
    assert "Acme thing" in content and "Acne thing" not in content
    # word boundary: 'AcneCloud' must not be mangled by the bare 'Acne' rule
    assert "AcmeCloudcloud" not in content


def test_normalize_command_and_idempotency(root):
    path, runner = root
    # ingest before any vocab exists → raw keeps the typo
    runner.invoke(main, ["add", "-", "--origin", "note:n"], input="AcneCloud rocks")
    assert "AcneCloud" in next(path.glob("raw/note/*.md")).read_text()
    (path / "vocab.yml").write_text("AcmeCloud: [AcneCloud]\n")

    first = runner.invoke(main, ["normalize"])
    assert first.exit_code == 0, first.output
    assert "normalized 1 document" in first.output
    assert "AcmeCloud" in next(path.glob("raw/note/*.md")).read_text()

    second = runner.invoke(main, ["normalize"])
    assert "normalized 0 document" in second.output


def test_show_and_status(root):
    _, runner = root
    add = runner.invoke(main, ["add", "-", "--origin", "note:s", "--title", "t"], input="hello world")
    doc_id = add.output.split()[1]

    show = runner.invoke(main, ["show", doc_id])
    assert "hello world" in show.output

    status = runner.invoke(main, ["status"])
    assert "documents: 1" in status.output
    assert "note: 1" in status.output


def test_resync_preserves_manual_tags_and_reports_unchanged(root):
    # THE bug this format exists to kill: a re-sync of an unchanged mutable
    # source must neither clobber concepts added later with `tars tag` nor
    # report the document as updated.
    path, runner = root
    sync_args = ["add", "-", "--connector", "jira", "--origin", "jira:PROJ-1",
                 "--title", "PROJ-1 Fix the thing", "--concept", "platform-team"]
    first = runner.invoke(main, sync_args, input="# PROJ-1 Fix the thing")
    assert first.output.startswith("added")
    doc_id = first.output.split()[1]

    runner.invoke(main, ["tag", doc_id, "--concept", "atlas"])
    resync = runner.invoke(main, sync_args, input="# PROJ-1 Fix the thing")
    assert resync.output.startswith("unchanged"), resync.output

    doc = path / "raw/jira/proj-1-fix-the-thing.md"
    body_first_line = doc.read_text().split("---\n\n")[1].split("\n")[0]
    assert body_first_line == "Concepts: [[platform-team]] [[atlas]]"

    # real content change still upserts — and still keeps the manual tag
    changed = runner.invoke(main, sync_args, input="# PROJ-1 Fix the thing\n\nnew comment")
    assert changed.output.startswith("updated")
    assert "[[atlas]]" in doc.read_text().split("---\n\n")[1].split("\n")[0]


def test_concepts_live_in_frontmatter_not_in_hash(root):
    path, runner = root
    add = runner.invoke(main, ["add", "-", "--origin", "note:c", "--title", "c",
                               "--concept", "alpha"], input="body text")
    doc_id = add.output.split()[1]
    doc = store.read_raw(path / "raw/note/c.md")
    assert doc.concepts == ["alpha"]
    assert doc.text == "body text"  # concepts line stripped on read
    # tagging alone must not change the content hash → a pure re-add stays unchanged
    runner.invoke(main, ["tag", doc_id, "--concept", "beta"])
    again = runner.invoke(main, ["add", "-", "--origin", "note:c", "--title", "c"],
                          input="body text")
    assert again.output.startswith("unchanged")


def test_migrate_v1_vault(root):
    path, runner = root
    # hand-build a v1 vault: concepts in the body line, no frontmatter key
    (path / ".tars").write_text("version: 1\n")
    v1 = path / "raw/jira/proj-9-old-format.md"
    v1.parent.mkdir(parents=True, exist_ok=True)
    v1.write_text("""---
id: 000000000009
connector: jira
origin: jira:PROJ-9
title: PROJ-9 old format
aliases: [PROJ-9 old format]
captured_at: 2026-07-01T00:00:00Z
tags: [jira]
meta:
  concepts: [platform-team]
---

Concepts: [[platform-team]] [[atlas]]

# PROJ-9 old format
""")
    blocked = runner.invoke(main, ["status"])
    assert blocked.exit_code != 0 and "tars migrate" in blocked.output

    migrated = runner.invoke(main, ["migrate"])
    assert migrated.exit_code == 0, migrated.output
    assert store.vault_version(path) == store.SCHEMA_VERSION

    doc = store.read_raw(v1)
    assert doc.concepts == ["platform-team", "atlas"]  # body line won, meta copy dropped
    assert "concepts" not in doc.meta
    assert doc.text.startswith("# PROJ-9 old format")
    assert v1.exists()  # filename untouched

    # searchable after the reindex that migrate runs, and idempotent
    found = runner.invoke(main, ["search", "old format"])
    assert "PROJ-9" in found.output
    assert "nothing to do" in runner.invoke(main, ["migrate"]).output


def test_hubs_regenerates_sources_as_derived_view(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:h1", "--title", "First meeting",
                         "--concept", "atlas"], input="notes one")
    runner.invoke(main, ["add", "-", "--origin", "note:h2", "--title", "Second meeting",
                         "--concept", "atlas"], input="notes two")
    hub = path / "wiki/concepts/atlas.md"
    hub.write_text("""# Atlas

The search team.

## Notes

- [[some-note]]

## Sources

- [[first-meeting|First meeting]] — where the plan was set
- [[stale-doc|Gone]] — no longer shelved here
""")
    result = runner.invoke(main, ["hubs"])
    assert result.exit_code == 0, result.output
    text = hub.read_text()
    assert "The search team." in text                       # description preserved
    assert "- [[some-note]]" in text                        # Notes preserved
    assert "— where the plan was set" in text               # relevance clause preserved
    assert "- [[second-meeting|Second meeting]]" in text    # missing entry added
    assert "stale-doc" not in text                          # unshelved entry dropped

    # a concept with no page yet gets a skeleton
    runner.invoke(main, ["add", "-", "--origin", "note:h3", "--title", "Third",
                         "--concept", "brand-new"], input="notes three")
    runner.invoke(main, ["hubs"])
    assert "- [[third|Third]]" in (path / "wiki/concepts/brand-new.md").read_text()


def test_rm_deletes_and_reports_references(root):
    path, runner = root
    add = runner.invoke(main, ["add", "-", "--origin", "note:secret", "--title", "Oops"],
                        input="accidentally pasted secret")
    doc_id = add.output.split()[1]
    task = path / "tasks/2026-07-08-check.md"
    task.write_text("do the thing\n\nSource: [[oops|Oops]]\n")

    result = runner.invoke(main, ["rm", doc_id, "--yes"])
    assert result.exit_code == 0, result.output
    assert not (path / "raw/note/oops.md").exists()
    assert "still referenced in tasks/2026-07-08-check.md" in result.output
    assert runner.invoke(main, ["search", "accidentally pasted"]).output.startswith("no results")
    assert runner.invoke(main, ["show", doc_id]).exit_code != 0


def test_web_origin_is_canonicalized():
    assert store.canonical_url(
        "HTTPS://Example.com/Post?utm_source=x&utm_campaign=y&id=3#section"
    ) == "https://example.com/Post?id=3"
    assert store.canonical_url("https://example.com/a?b=1") == "https://example.com/a?b=1"


def test_migrate_drops_dead_entity_tables(root, tmp_path):
    # DBs created before the cleanup carried empty entities tables; connect()
    # must drop them so the schema stops advertising a layer nothing populates.
    import sqlite3

    from tars import db as database

    path, _ = root
    legacy = sqlite3.connect(path / "tars.db")
    legacy.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
            UNIQUE (kind, name));
        CREATE TABLE IF NOT EXISTS doc_entities (
            doc_id TEXT NOT NULL, entity_id INTEGER NOT NULL,
            UNIQUE (doc_id, entity_id));
    """)
    legacy.close()

    db = database.connect(path)
    tables = {r["name"] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "entities" not in tables
    assert "doc_entities" not in tables
    assert {"documents", "chunks", "sync_state"} <= tables


def test_slugify_truncates_at_word_boundary():
    # Raw filenames are permanent link targets; the 80-char cap must trim at a
    # hyphen, not mid-word ("…-in-the-media-up" for "upload" reads wrong).
    title = ("TICKET-1018 Fix crop and resize sample thumbnail "
             "image processing in the media upload pipeline")
    assert store.slugify(title) == (
        "ticket-1018-fix-crop-and-resize-sample-thumbnail-image-processing-in-the-media")
    assert store.slugify("x" * 70 + " media assets") == "x" * 70 + "-media"
    assert store.slugify("x" * 90) == "x" * 80  # no boundary to trim at
    assert len(store.slugify("word " * 40)) <= 80


def test_search_is_accent_insensitive(root):
    # The corpus mixes Spanish and English; "reunion" must find "reunión" and
    # vice versa — accent-folding beats English-only stemming for this vault.
    _, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:es", "--title", "Reunión traducciones"],
                  input="Reunión con María sobre la traducción del catálogo")
    hit = runner.invoke(main, ["search", "reunion traduccion"])
    assert "note:es" in hit.output, hit.output
    accented = runner.invoke(main, ["search", "reunión maría"])
    assert "note:es" in accented.output, accented.output


def test_porter_index_is_rebuilt_in_place(root):
    # DBs indexed with the old English-only porter tokenizer are rebuilt on
    # connect — nobody has to remember to reindex after upgrading.
    import sqlite3

    from tars import db as database

    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:mx", "--title", "m"],
                  input="publicación de artículos")
    # simulate the legacy index: recreate chunks_fts with porter and repopulate
    legacy = sqlite3.connect(path / "tars.db")
    legacy.executescript("""
        DROP TABLE chunks_fts;
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text, content='chunks', content_rowid='id', tokenize='porter unicode61');
        INSERT INTO chunks_fts(rowid, text) SELECT id, text FROM chunks;
    """)
    legacy.close()

    db = database.connect(path)
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()["sql"]
    assert "porter" not in sql and "remove_diacritics 2" in sql
    hit = runner.invoke(main, ["search", "publicacion articulos"])
    assert "note:mx" in hit.output, hit.output


def test_init_creates_inbox(root):
    path, _ = root
    assert (path / "inbox").is_dir()


def test_init_gitignores_the_disposable_db(root):
    path, _ = root
    ignored = (path / ".gitignore").read_text().split()
    assert db.DB_NAME in ignored
    assert f"{db.DB_NAME}-wal" in ignored
    assert f"{db.DB_NAME}-shm" in ignored


def test_init_appends_to_an_existing_gitignore_without_clobbering_it(root):
    # `tars init` is re-runnable on a live vault: the user's own ignores survive,
    # and a vault whose .gitignore predates this fix still gets the DB covered.
    path, runner = root
    (path / ".gitignore").write_text("secrets.env")  # no trailing newline
    assert runner.invoke(main, ["init", str(path)]).exit_code == 0
    lines = (path / ".gitignore").read_text().split()
    assert lines == ["secrets.env", db.DB_NAME, f"{db.DB_NAME}-wal", f"{db.DB_NAME}-shm"]


def test_init_gitignore_is_idempotent(root):
    path, runner = root
    assert runner.invoke(main, ["init", str(path)]).exit_code == 0
    assert runner.invoke(main, ["init", str(path)]).exit_code == 0
    lines = (path / ".gitignore").read_text().split()
    assert lines == sorted(set(lines), key=lines.index)  # no duplicates


def test_sweep_ingests_inbox_drops(root):
    path, runner = root
    inbox = path / "inbox"
    (inbox / "idea-atlas-tracker.md").write_text("# Idea\nreplace the excel with a jira tracker")
    (inbox / "2026-07-08.md").write_text("Llamar a María por lo de la beca")
    (inbox / ".gitkeep").write_text("")

    r = runner.invoke(main, ["sweep"])
    assert r.exit_code == 0, r.output
    assert "swept 2" in r.output
    assert not list(inbox.glob("*.md"))          # drops removed after ingest
    assert (inbox / ".gitkeep").exists()          # dotfiles untouched

    listing = json.loads(runner.invoke(main, ["list", "--connector", "note", "--json"]).output)
    titles = {d["title"] for d in listing}
    assert "idea atlas tracker" in titles                     # filename-derived
    assert "Llamar a María por lo de la beca" in titles        # date filename → first line

    doc = store.read_raw(path / "raw/note/idea-atlas-tracker.md")
    assert doc.meta["source"] == "inbox"

    # the same content re-dropped dedupes (content-addressed origin)
    (inbox / "again.md").write_text("Llamar a María por lo de la beca")
    r2 = runner.invoke(main, ["sweep"])
    assert "unchanged" in r2.output
    assert len(json.loads(
        runner.invoke(main, ["list", "--connector", "note", "--json"]).output)) == 2

    # empty inbox re-sweep is a no-op; non-text drops are skipped, not eaten
    (inbox / "photo.jpg").write_bytes(b"\xff\xd8")
    r3 = runner.invoke(main, ["sweep"])
    assert "swept 0" in r3.output and "skipped" in r3.output
    assert (inbox / "photo.jpg").exists()


def test_backup_writes_and_prunes_bundles(root, tmp_path_factory):
    path, runner = root
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)
    dest = tmp_path_factory.mktemp("bundles")
    for stamp in ("20260101-000001", "20260102-000001", "20260103-000001"):
        (dest / f"tars-vault-{stamp}.bundle").write_bytes(b"old")

    r = runner.invoke(main, ["backup", str(dest), "--keep", "2"])
    assert r.exit_code == 0, r.output
    bundles = sorted(dest.glob("tars-vault-*.bundle"))
    assert len(bundles) == 2                       # 3 old + 1 new, pruned to 2
    assert bundles[-1].stat().st_size > 100        # the real bundle survived
    assert subprocess.run(["git", "bundle", "verify", str(bundles[-1])],
                          capture_output=True).returncode == 0


def test_vocab_rules_scope_to_connectors(root):
    # STT corrections are only "more faithful than raw" for STT sources: a web
    # article about fiber optics must NOT be rewritten to fix a Granola
    # hearing problem. Scoped entries apply to their connectors; legacy flat
    # entries stay global.
    path, runner = root
    (path / "vocab.yml").write_text(
        "AcmeCloud:\n"
        "  variants: [AcneCloud]\n"
        "  connectors: [granola]\n"
        "Zephyr: [Zephir]\n"   # legacy flat form -> every connector
    )
    granola = runner.invoke(
        main, ["add", "-", "--connector", "granola", "--origin", "granola:v1",
               "--title", "daily"],
        input="AcneCloud rollout discussed, Zephir team aligned")
    assert granola.exit_code == 0, granola.output
    text = store.read_raw(path / "raw/granola/daily.md").text
    assert "AcmeCloud" in text and "AcneCloud" not in text   # scoped rule fired
    assert "Zephyr team" in text                              # global rule fired

    note = runner.invoke(
        main, ["add", "-", "--origin", "note:fiber", "--title", "optics"],
        input="AcneCloud is my favourite fiber optics vendor, says Zephir")
    assert note.exit_code == 0, note.output
    text = store.read_raw(path / "raw/note/optics.md").text
    assert "AcneCloud" in text            # granola-scoped rule must NOT fire here
    assert "fiber optics" in text         # innocent words untouched
    assert "says Zephyr" in text           # global rule still applies

    # tars normalize respects scope too: no doc should change on re-run
    assert "normalized 0" in runner.invoke(main, ["normalize"]).output


def _fake_gh(payloads):
    """Return a _gh_json stand-in serving canned responses keyed by path prefix."""
    def fake(*args):
        for key, value in payloads.items():
            if any(str(a).startswith(key) for a in args):
                return value
        raise AssertionError(f"unexpected gh call: {args}")
    return fake


GH_PR = {
    "number": 7, "title": "Fix login redirect loop",
    "user": {"login": "octocat"}, "state": "open", "draft": False,
    "created_at": "2026-07-01T10:00:00Z", "updated_at": "2026-07-02T09:00:00Z",
    "merged_at": None, "additions": 40, "deletions": 12, "changed_files": 3,
    "html_url": "https://github.com/acme/webapp/pull/7",
    "head": {"ref": "fix/capacity"}, 
    "base": {"ref": "master", "repo": {"full_name": "acme/webapp"}},
    "body": "Capacity was checked against the wrong session.",
}


def test_github_connector_sync_and_refresh(root, monkeypatch):
    from tars.connectors import github as gh_mod

    path, runner = root
    (path / "connectors.yml").write_text("github:\n  orgs: [acme]\n")
    search_hit = {"number": 7,
                  "repository_url": "https://api.github.com/repos/acme/webapp"}
    payloads = {
        "user": {"login": "octocat"},
        "-X": [search_hit],  # search/issues call starts with -X GET
        "repos/acme/webapp/pulls/7": GH_PR,
        "repos/acme/webapp/issues/7/comments": [],
        "repos/acme/webapp/pulls/7/reviews": [],
    }
    def route(*args):
        joined = " ".join(str(a) for a in args)
        if "search/issues" in joined:
            return [search_hit]
        if joined.startswith("user"):
            return {"login": "octocat"}
        if "pulls/7/reviews" in joined:
            return payloads["repos/acme/webapp/pulls/7/reviews"]
        if "issues/7/comments" in joined:
            return payloads["repos/acme/webapp/issues/7/comments"]
        if "pulls/7/comments" in joined:
            return []
        if "pulls/7" in joined:
            return payloads["repos/acme/webapp/pulls/7"]
        raise AssertionError(f"unexpected gh call: {joined}")
    monkeypatch.setattr(gh_mod, "_gh_json", route)

    first = runner.invoke(main, ["sync", "github"])
    assert first.exit_code == 0, first.output
    assert "added" in first.output
    doc = path / "raw/github/webapp-7-fix-login-redirect-loop.md"
    assert doc.exists()
    text = doc.read_text()
    assert "origin: github:acme/webapp#7" in text
    assert "Capacity was checked against the wrong session." in text
    assert "+40 −12 across 3 files" in text
    assert runner.invoke(main, ["cursor", "github"]).output.strip()  # watermark persisted

    # unchanged re-sync is a no-op
    second = runner.invoke(main, ["sync", "github"])
    assert "1 unchanged" in second.output

    # a review lands → refresh upserts, discussion captured verbatim
    payloads["repos/acme/webapp/pulls/7/reviews"] = [{
        "user": {"login": "hubot"}, "state": "APPROVED",
        "submitted_at": "2026-07-03T08:00:00Z", "body": "LGTM, ship it"}]
    third = runner.invoke(main, ["sync", "github"])
    assert "1 updated" in third.output
    assert "review:APPROVED" in doc.read_text() and "LGTM, ship it" in doc.read_text()

    # searchable like everything else
    hit = runner.invoke(main, ["search", "login redirect"])
    assert "github:acme/webapp#7" in hit.output


def test_github_connector_reviewer_sweep(root, monkeypatch):
    from tars.connectors import github as gh_mod

    path, runner = root
    (path / "connectors.yml").write_text(
        "github:\n  orgs: [acme]\n  reviewers: [octocat]\n")
    authored_hit = {"number": 7,
                    "repository_url": "https://api.github.com/repos/acme/webapp"}
    review_hit = {"number": 8,
                  "repository_url": "https://api.github.com/repos/acme/webapp"}
    review_pr = dict(GH_PR, number=8, title="Add dark mode toggle",
                     user={"login": "monalisa"},
                     html_url="https://github.com/acme/webapp/pull/8")

    def route(*args):
        joined = " ".join(str(a) for a in args)
        if "search/issues" in joined:
            if "author:octocat" in joined:
                return [authored_hit]
            # the PR shows up under both review qualifiers — must dedup
            if "review-requested:octocat" in joined or "reviewed-by:octocat" in joined:
                return [review_hit]
            raise AssertionError(f"unexpected search: {joined}")
        if joined.startswith("user"):
            return {"login": "octocat"}
        if "pulls/7" in joined and "comments" not in joined and "reviews" not in joined:
            return GH_PR
        if "pulls/8" in joined and "comments" not in joined and "reviews" not in joined:
            return review_pr
        if "comments" in joined or "reviews" in joined:
            return []
        raise AssertionError(f"unexpected gh call: {joined}")
    monkeypatch.setattr(gh_mod, "_gh_json", route)

    result = runner.invoke(main, ["sync", "github"])
    assert result.exit_code == 0, result.output
    # authored sweep + both review sweeps report separately
    assert "github/octocat: 1 added" in result.output
    assert "github/review-requested:octocat: 1 added" in result.output
    assert "github/reviewed-by:octocat: 0 added" in result.output  # deduped
    assert (path / "raw/github/webapp-8-add-dark-mode-toggle.md").exists()
    text = (path / "raw/github/webapp-8-add-dark-mode-toggle.md").read_text()
    assert "origin: github:acme/webapp#8" in text
    assert "author: monalisa" in text


def test_github_connector_refuses_unscoped_sweep(root):
    path, runner = root
    result = runner.invoke(main, ["sync", "github"])  # no connectors.yml at all
    assert result.exit_code != 0
    assert "will not sweep your whole account" in result.output
    assert runner.invoke(main, ["cursor", "github"]).output.strip() == ""  # no watermark


def test_github_discussion_excludes_bots_and_quotes_bodies(root, monkeypatch):
    from tars.connectors import github as gh_mod

    path, runner = root
    (path / "connectors.yml").write_text("github:\n  orgs: [acme]\n  ignore_authors: [ci-noise]\n")
    search_hit = {"number": 7,
                  "repository_url": "https://api.github.com/repos/acme/webapp"}
    def route(*args):
        joined = " ".join(str(a) for a in args)
        if "search/issues" in joined:
            return [search_hit]
        if joined.startswith("user"):
            return {"login": "octocat"}
        if "issues/7/comments" in joined:
            return [
                {"user": {"login": "github-actions[bot]"}, "created_at": "2026-07-01T11:00:00Z",
                 "body": "## Coverage report\n" + "x" * 5000},
                {"user": {"login": "ci-noise"}, "created_at": "2026-07-01T11:01:00Z",
                 "body": "token refreshed"},
                {"user": {"login": "monalisa"}, "created_at": "2026-07-01T12:00:00Z",
                 "body": "### Careful here\nthis breaks login flows"},
            ]
        if "pulls/7/reviews" in joined or "pulls/7/comments" in joined:
            return []
        if "pulls/7" in joined:
            return GH_PR
        raise AssertionError(joined)
    monkeypatch.setattr(gh_mod, "_gh_json", route)

    assert runner.invoke(main, ["sync", "github"]).exit_code == 0
    text = (path / "raw/github/webapp-7-fix-login-redirect-loop.md").read_text()
    assert "monalisa" in text and "this breaks login flows" in text
    assert "github-actions" not in text and "Coverage report" not in text  # bots dropped
    assert "token refreshed" not in text                                    # ignore_authors dropped
    assert "> ### Careful here" in text   # body markdown blockquoted, can't break structure


def test_ingest_canonicalizes_trailing_newlines(root):
    # A connector passing text with a trailing newline (github's assembler did)
    # must hash identically to what read_raw returns, or every doc is
    # permanently "drifted" and re-syncs churn as updated forever.
    from tars import db as database, ingest
    from tars.store import RawDoc

    path, runner = root
    db = database.connect(path)
    doc_id, status = ingest.add(path, db, RawDoc(
        connector="github", origin="github:o/r#1", text="# PR body\n\ncontent\n",
        title="r#1 t"))
    assert status == "added"
    again, status2 = ingest.add(path, db, RawDoc(
        connector="github", origin="github:o/r#1", text="# PR body\n\ncontent\n",
        title="r#1 t"))
    assert status2 == "unchanged"
    stored = db.execute("SELECT content_hash FROM documents WHERE id=?", (doc_id,)).fetchone()
    reread = store.read_raw(path / "raw/github/r-1-t.md")
    assert store.content_hash(reread.text) == stored["content_hash"]  # no drift


def test_doctor_accepts_escaped_pipe_table_links(root):
    # Obsidian escapes the alias pipe as \| inside Markdown tables; the target
    # of [[stem\|label]] is stem, not "stem\" — that's a valid link, not rot.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:d1", "--title", "Real Doc"],
                  input="body")
    (path / "wiki/concepts/roadmap.md").write_text(
        "# Roadmap\n\n| when | what |\n|---|---|\n"
        "| Q3 | [[real-doc\\|Real Doc]] |\n| Q4 | [[missing-doc\\|Gone]] |\n"
        "\n## Sources\n\n- [[real-doc|Real Doc]]\n")
    result = runner.invoke(main, ["doctor"])
    out = result.output
    assert "real-doc" not in out.replace("missing-doc", "")  # valid escaped link accepted
    assert "missing-doc" in out                              # genuinely dangling still caught


def test_search_chunk_flag(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:chunky", "--title", "chunky"],
                  input="alpha beta gamma\n\nmore context about beta here")
    plain = json.loads(runner.invoke(main, ["search", "beta", "--json"]).output)
    verbose = json.loads(runner.invoke(main, ["search", "beta", "-v", "--json"]).output)
    assert "chunk" not in plain[0]  # omitted entirely, not emitted as null
    assert "beta" in verbose[0]["chunk"]


def test_search_json_is_compact(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:c1", "--title", "compact"],
                  input="compactness check")
    out = runner.invoke(main, ["search", "compactness", "--json"]).output
    assert json.loads(out)  # valid JSON…
    assert "\n" not in out.strip()  # …on a single line, no pretty-print padding


def test_show_head_truncates(root):
    _, runner = root
    text = "\n\n".join(f"paragraph {i} lorem ipsum" for i in range(40))
    add = runner.invoke(main, ["add", "-", "--origin", "note:long", "--title", "long"],
                        input=text)
    doc_id = add.output.split()[1]
    result = runner.invoke(main, ["show", doc_id, "--head", "5"])
    assert len(result.output.strip().splitlines()) == 6  # 5 lines + continuation marker
    assert "more lines" in result.output
    full = runner.invoke(main, ["show", doc_id])
    assert "more lines" not in full.output


def test_show_grep_slices_with_context(root):
    _, runner = root
    text = "\n".join(f"line number {i}" for i in range(100))
    add = runner.invoke(main, ["add", "-", "--origin", "note:grep", "--title", "grep me"],
                        input=text)
    doc_id = add.output.split()[1]
    result = runner.invoke(main, ["show", doc_id, "--grep", "line NUMBER 42", "-C", "1"])
    assert "line number 42" in result.output  # case-insensitive match
    assert "line number 41" in result.output and "line number 43" in result.output
    assert "line number 50" not in result.output
    nothing = runner.invoke(main, ["show", doc_id, "--grep", "absent-term"])
    assert "no matches" in nothing.output
    combined = runner.invoke(main, ["show", doc_id, "--head", "3", "--grep", "x"])
    assert combined.exit_code != 0  # mutually exclusive views
    bad = runner.invoke(main, ["show", doc_id, "--grep", "["])
    assert bad.exit_code != 0 and "bad --grep pattern" in bad.output


def test_list_since_filters_by_captured_at(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:s1", "--title", "since one"],
                  input="first")
    everything = json.loads(runner.invoke(main, ["list", "--json"]).output)
    past = json.loads(runner.invoke(main, ["list", "--since", "2000-01-01", "--json"]).output)
    future = json.loads(runner.invoke(main, ["list", "--since", "2999-01-01", "--json"]).output)
    assert len(past) == len(everything) == 1
    assert future == []
