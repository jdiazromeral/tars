"""Adds never replace your words.

Text you or an agent authored (note, agent, activity) only grows: a re-add
with different text is refused, `--append` adds to it and keeps what it
doesn't restate, and appending to a slot that doesn't exist is an error
unless `--create` says that's intended. Synced sources still refresh in
place — that's the source changing, not you.
"""

import pytest
from click.testing import CliRunner

from tars import store
from tars.cli import main


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


def add(runner, text, *args):
    return runner.invoke(main, ["add", "-", *args], input=text)


@pytest.mark.parametrize("connector", ["note", "agent", "activity"])
def test_readd_with_different_text_is_refused(root, connector):
    path, runner = root
    slot = ["--connector", connector, "--origin", f"{connector}:running", "--title", "Running"]
    assert add(runner, "first thought", *slot).exit_code == 0

    result = add(runner, "a different thought", *slot)
    assert result.exit_code != 0
    assert "--append" in result.output
    assert store.read_raw(path / f"raw/{connector}/running.md").text == "first thought"


def test_synced_source_still_refreshes_in_place(root):
    path, runner = root
    slot = ["--connector", "jira", "--origin", "jira:PROJ-1", "--title", "PROJ-1"]
    add(runner, "estimate: 2 sprints", *slot)

    result = add(runner, "estimate: 3 sprints", *slot)
    assert result.output.startswith("updated"), result.output
    assert store.read_raw(path / "raw/jira/proj-1.md").text == "estimate: 3 sprints"


def test_append_keeps_title_tags_and_meta_it_does_not_restate(root):
    path, runner = root
    add(runner, "first idea", "--origin", "note:ideas", "--title", "Ideas", "--tag", "t1")

    result = add(runner, "second idea", "--append", "--origin", "note:ideas", "--tag", "t2")
    assert result.exit_code == 0, result.output
    doc = store.read_raw(path / "raw/note/ideas.md")
    assert doc.text == "first idea\nsecond idea"
    assert doc.title == "Ideas"
    assert doc.tags == ["t1", "t2"]


def test_append_to_a_missing_slot_needs_create(root):
    path, runner = root
    result = add(runner, "x", "--append", "--origin", "note:typo-slot")
    assert result.exit_code != 0
    assert "--create" in result.output
    assert not list(path.glob("raw/note/*.md"))

    result = add(runner, "x", "--append", "--create", "--origin", "note:day", "--title", "Day")
    assert result.output.startswith("added"), result.output


def test_create_only_makes_sense_with_append(root):
    _, runner = root
    result = add(runner, "x", "--create", "--origin", "note:x")
    assert result.exit_code != 0
    assert "--append" in result.output


def test_readd_without_a_title_keeps_the_stored_one(root):
    path, runner = root
    add(runner, "same text", "--connector", "jira", "--origin", "jira:P-2", "--title", "P-2 Title")

    result = add(runner, "same text", "--connector", "jira", "--origin", "jira:P-2")
    assert result.output.startswith("unchanged"), result.output
    assert store.read_raw(path / "raw/jira/p-2-title.md").title == "P-2 Title"


def test_a_refused_readd_logs_nothing(root):
    path, runner = root
    add(runner, "first", "--origin", "note:once", "--title", "Once")
    assert add(runner, "second", "--origin", "note:once", "--title", "Once").exit_code != 0

    log = (path / "log/ingestions.jsonl").read_text().splitlines()
    assert len(log) == 1 and '"added"' in log[0]


@pytest.mark.parametrize("command", ["tag", "untag"])
def test_shelving_never_overwrites_a_concurrent_append(root, monkeypatch, command):
    # tag/untag used to read the raw file outside the write lock and re-add the
    # whole document, so an append landing in between was overwritten by the
    # stale snapshot. Replay that interleaving: the moment the shelving command
    # has read the file, another connection appends to it.
    import threading

    from tars import db as database
    from tars import ingest

    path, runner = root
    first = add(runner, "- entry one", "--append", "--create", "--connector", "activity",
                "--origin", "activity:d1", "--title", "d1", "--concept", "keep")
    doc_id = first.output.split()[1]

    def append_entry():
        conn = database.connect(path)
        ingest.add(path, conn, store.RawDoc(connector="activity", origin="activity:d1",
                                            text="- entry two"), append=True, create=False)
        conn.close()

    real_read = store.read_raw
    racers = []

    def read_then_race(p, *args, **kwargs):
        doc = real_read(p, *args, **kwargs)
        if not racers:
            racers.append(threading.Thread(target=append_entry))
            racers[0].start()
            racers[0].join(timeout=1)  # done if nothing holds the lock; blocked if shelving does
        return doc

    monkeypatch.setattr(store, "read_raw", read_then_race)
    concept = "keep" if command == "untag" else "x"
    result = runner.invoke(main, [command, doc_id, "--concept", concept])
    assert result.exit_code == 0, (result.output, repr(result.exception))
    racers[0].join(timeout=15)
    monkeypatch.undo()

    doc = store.read_raw(path / "raw/activity/d1.md")
    assert doc.text.splitlines() == ["- entry one", "- entry two"]
    assert doc.concepts == ([] if command == "untag" else ["keep", "x"])


def test_the_guard_checks_the_raw_file_not_the_index(root):
    # The index can be stale (a hand-edit before finalize); comparing against it
    # let a re-add of the old text, with a new concept, overwrite the edit.
    path, runner = root
    add(runner, "line A", "--origin", "note:n", "--title", "n")
    raw = path / "raw/note/n.md"
    raw.write_text(raw.read_text().rstrip("\n") + "\nline B\n")

    result = add(runner, "line A", "--origin", "note:n", "--title", "n", "--concept", "foo")
    assert result.exit_code != 0
    assert store.read_raw(raw).text == "line A\nline B"


def test_a_vocab_rule_added_later_does_not_refuse_the_same_words(root):
    # The stored words win; applying the new rule is `tars normalize`'s job.
    path, runner = root
    add(runner, "hello Acneson world", "--origin", "note:v", "--title", "v")
    (path / "vocab.yml").write_text("Acne:\n  variants: [Acneson]\n")

    result = add(runner, "hello Acneson world", "--origin", "note:v", "--title", "v")
    assert result.exit_code == 0, result.output
    assert store.read_raw(path / "raw/note/v.md").text == "hello Acneson world"


def test_a_retried_append_still_records_a_new_tag(root):
    path, runner = root
    add(runner, "x", "--append", "--create", "--origin", "note:z", "--title", "z")

    result = add(runner, "x", "--append", "--origin", "note:z", "--tag", "new")
    assert result.output.startswith("updated"), result.output
    assert store.read_raw(path / "raw/note/z.md").tags == ["new"]


def test_append_takes_text_on_stdin_only(root, tmp_path):
    # Appending a file or URL replaced the stored title and source sidecar.
    _, runner = root
    other = tmp_path / "notes2.md"
    other.write_text("file body")
    result = runner.invoke(main, ["add", str(other), "--append", "--origin", "note:z"])
    assert result.exit_code != 0
    assert "stdin" in result.output


def test_normalize_never_overwrites_a_concurrent_append(root, monkeypatch):
    # normalize read each file outside the lock, then rewrote it: an append
    # landing in between was lost. Replay that interleaving.
    import threading

    from tars import db as database
    from tars import ingest

    path, runner = root
    add(runner, "- met Acneson", "--append", "--create", "--connector", "activity",
        "--origin", "activity:d1", "--title", "d1")
    (path / "vocab.yml").write_text("Acne:\n  variants: [Acneson]\n")

    def append_entry():
        conn = database.connect(path)
        ingest.add(path, conn, store.RawDoc(connector="activity", origin="activity:d1",
                                            text="- entry two"), append=True, create=False)
        conn.close()

    real_read = store.read_raw
    racers = []

    def read_then_race(p, *args, **kwargs):
        doc = real_read(p, *args, **kwargs)
        if not racers:
            racers.append(threading.Thread(target=append_entry))
            racers[0].start()
            racers[0].join(timeout=1)
        return doc

    monkeypatch.setattr(store, "read_raw", read_then_race)
    result = runner.invoke(main, ["normalize"])
    assert result.exit_code == 0, (result.output, repr(result.exception))
    racers[0].join(timeout=15)
    monkeypatch.undo()

    assert store.read_raw(path / "raw/activity/d1.md").text.splitlines() == [
        "- met Acne", "- entry two"]


# --- second review of #12: decide against the raw file, never the index ---

def test_append_without_an_index_row_keeps_the_day(root):
    # tars.db is a disposable cache: with it gone, an append must still find
    # the day's raw file (by id) instead of starting it over.
    path, runner = root
    for line in ("- one", "- two"):
        add(runner, line, "--append", "--create", "--connector", "activity",
            "--origin", "activity:d", "--title", "d")
    for db_file in path.glob("tars.db*"):
        db_file.unlink()

    result = add(runner, "- three", "--append", "--create", "--connector", "activity",
                 "--origin", "activity:d", "--title", "d")
    assert result.output.startswith("updated"), result.output
    assert store.read_raw(path / "raw/activity/d.md").text == "- one\n- two\n- three"


def test_readd_without_an_index_row_is_still_refused(root):
    path, runner = root
    add(runner, "my words", "--origin", "note:slot", "--title", "slot")
    for db_file in path.glob("tars.db*"):
        db_file.unlink()

    assert add(runner, "other words", "--origin", "note:slot", "--title", "slot").exit_code != 0
    assert store.read_raw(path / "raw/note/slot.md").text == "my words"


def test_sweep_skips_a_drop_it_cannot_ingest_and_keeps_going(root):
    path, runner = root
    add(runner, "drop text", "--title", "drop")
    raw = path / "raw/note/drop.md"
    raw.write_text(raw.read_text().replace("tags: []", "tags: [oops"))
    (path / "inbox/a.txt").write_text("drop text")
    (path / "inbox/b.txt").write_text("other text")

    result = runner.invoke(main, ["sweep"])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.output
    assert "a.txt" in result.output
    assert (path / "inbox/a.txt").exists()          # kept, not lost
    assert not (path / "inbox/b.txt").exists()      # the rest still swept


def test_same_words_with_a_narrowed_vocab_rule_are_unchanged(root):
    # A content-addressed origin *is* the text: the same origin means the same
    # words, whatever vocab did to the stored copy since.
    path, runner = root
    (path / "vocab.yml").write_text("Acme:\n  variants: [acne]\n")
    add(runner, "we use acne", "--title", "acme")
    (path / "vocab.yml").write_text("Acme:\n  variants: [acne]\n  connectors: [granola]\n")

    result = add(runner, "we use acne", "--title", "acme")
    assert result.output.startswith("unchanged"), result.output


def test_append_keeps_a_raw_title_and_concepts_the_index_lags_behind(root):
    path, runner = root
    add(runner, "- one", "--append", "--create", "--connector", "activity",
        "--origin", "activity:d", "--title", "d", "--concept", "auth")
    raw = path / "raw/activity/d.md"
    raw.write_text(raw.read_text().replace("title: d\n", "title: Renamed\n")
                   .replace("- auth\n", "- auth\n- billing\n"))  # index not reindexed

    add(runner, "- two", "--append", "--connector", "activity", "--origin", "activity:d")
    doc = store.read_raw(raw)
    assert doc.title == "Renamed"
    assert doc.concepts == ["auth", "billing"]


def test_readd_of_the_same_words_keeps_tags_and_takes_a_new_title(root):
    path, runner = root
    add(runner, "same", "--origin", "note:x", "--title", "Old", "--tag", "t1")

    result = add(runner, "same", "--origin", "note:x", "--title", "New", "--concept", "foo")
    assert result.output.startswith("updated"), result.output
    doc = store.read_raw(path / "raw/note/old.md")
    assert (doc.title, doc.tags, doc.concepts) == ("New", ["t1"], ["foo"])


def test_api_append_defaults_to_not_creating(root):
    from tars import db as database
    from tars import ingest

    path, _ = root
    with pytest.raises(ingest.NoSuchDocument):
        ingest.add(path, database.connect(path),
                   store.RawDoc(connector="note", origin="note:typo", text="x"), append=True)
