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
