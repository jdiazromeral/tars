"""Review themes G (hostile titles) and H (what `rm` leaves behind)."""

import pytest
import yaml
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


def _add(runner, text, *args):
    result = runner.invoke(main, ["add", "-", *args], input=text)
    assert result.exit_code == 0, result.output
    return result.output.split()[1]


# --- G: a title is a label, never markup -----------------------------------------

def test_a_newline_in_a_title_cannot_inject_into_a_hub(root):
    # The injected "## Evil" used to end the Sources section, so every `tars
    # hubs` run added another copy, and doctor stayed clean.
    path, runner = root
    _add(runner, "v", "--title", "Victim", "--concept", "proj")
    _add(runner, "x", "--title", "Weekly\n## Evil\n- [[victim|trusted]]", "--concept", "proj")
    runner.invoke(main, ["hubs"])
    hub = (path / "wiki/concepts/proj.md").read_text()
    runner.invoke(main, ["hubs"])
    assert (path / "wiki/concepts/proj.md").read_text() == hub  # stable, no growth
    assert not any(line.startswith("## Evil") for line in hub.splitlines())
    assert "[[victim|trusted]]" not in hub


def test_brackets_and_pipes_in_a_title_keep_the_link_whole(root):
    path, runner = root
    _add(runner, "x", "--title", "a ]] b | c [[d", "--concept", "proj")
    runner.invoke(main, ["hubs"])
    line = next(x for x in (path / "wiki/concepts/proj.md").read_text().splitlines()
                if x.startswith("- [["))
    assert line.count("[[") == 1 and line.count("]]") == 1 and line.count("|") == 1


@pytest.mark.parametrize("title", ["Postgres vs Mongo: what we decided",
                                   'Why: "x" # y\ntags: [admin]'])
def test_promote_writes_valid_yaml_whatever_the_title(root, title):
    path, runner = root
    doc_id = _add(runner, "body", "--title", "Plain note")
    result = runner.invoke(main, ["promote", doc_id, "--title", title])
    assert result.exit_code == 0, result.output
    note = next(path.glob("wiki/notes/*.md"))
    fm = yaml.safe_load(note.read_text().split("\n---\n", 1)[0][4:])
    assert fm["title"] == title
    assert "tags" not in fm  # nothing injected
    assert fm["source_doc"] == doc_id  # a string, even when the hex id is all digits


def test_promote_refuses_a_name_another_layer_already_links(root):
    # [[victim]] would resolve to either file arbitrarily (doctor: ambiguous-stem).
    path, runner = root
    doc_id = _add(runner, "v", "--title", "Victim")
    result = runner.invoke(main, ["promote", doc_id, "--title", "Victim"])
    assert result.exit_code != 0
    assert "raw/note/victim.md" in result.output
    assert not list(path.glob("wiki/notes/*.md"))


# --- H: rm leaves nothing readable behind in the index ---------------------------

def test_rm_leaves_no_trace_of_the_text_in_the_database_files(root):
    path, runner = root
    _add(runner, "token hunter2zzz and more words", "--title", "secretdoc")
    _add(runner, "an unrelated note", "--title", "other")
    runner.invoke(main, ["rm", "raw/note/secretdoc.md", "--yes"])
    for f in path.glob("tars.db*"):
        assert b"hunter2zzz" not in f.read_bytes(), f.name
    assert "other" in runner.invoke(main, ["search", "unrelated"]).output


def test_rm_names_what_it_cannot_erase(root):
    path, runner = root
    _add(runner, "x", "--title", "secretdoc")
    result = runner.invoke(main, ["rm", "raw/note/secretdoc.md", "--yes"])
    assert "not erased" in result.output
    for place in ("log/ingestions.jsonl", "git history", "backups"):
        assert place in result.output


# --- review of #17 ---------------------------------------------------------------

@pytest.mark.parametrize("existing", ["Victim.md", "VICTIM.md"])
def test_promote_never_overwrites_a_note_differing_only_in_case(root, existing):
    # APFS is case-insensitive: writing victim.md opens Victim.md.
    path, runner = root
    (path / "wiki/notes" / existing).write_text("my hand-written note\n")
    doc_id = _add(runner, "body", "--title", "src")
    result = runner.invoke(main, ["promote", doc_id, "--title", "Victim"])
    assert result.exit_code != 0
    assert (path / "wiki/notes" / existing).read_text() == "my hand-written note\n"


def test_a_blank_title_falls_back_to_the_origin_as_label(root):
    path, runner = root
    _add(runner, "x", "--title", " \n ", "--concept", "p", "--origin", "note:blank")
    runner.invoke(main, ["hubs"])
    hub = (path / "wiki/concepts/p.md").read_text()
    assert "|]]" not in hub and "|note:blank]]" in hub


def test_a_title_is_stored_on_one_line(root):
    # Fixed at capture, so every renderer — code or an agent writing a digest —
    # gets a label that can't start a heading.
    path, runner = root
    _add(runner, "x", "--title", "Weekly\n## Evil", "--origin", "note:w")
    doc = next(store.read_raw(p) for p in path.glob("raw/note/*.md"))
    assert doc.title == "Weekly ## Evil"


def test_rm_says_so_when_the_index_could_not_be_scrubbed(root, monkeypatch):
    from tars import ingest
    path, runner = root
    _add(runner, "token hunter2zzz", "--title", "secretdoc")
    monkeypatch.setattr(ingest, "scrub_index", lambda db: False)  # e.g. another reader busy
    result = runner.invoke(main, ["rm", "raw/note/secretdoc.md", "--yes"])
    assert result.exit_code == 0, result.output
    assert "may still be readable in tars.db" in result.output
    assert "tars reindex" in result.output


def test_scrub_index_reports_failure_instead_of_raising():
    import sqlite3

    from tars import ingest

    class Busy:
        def execute(self, sql, *args):
            if sql.startswith("VACUUM"):
                raise sqlite3.OperationalError("database is locked")
            return self

        def fetchone(self):
            return (0, 0, 0)

        def commit(self):
            pass

    assert ingest.scrub_index(Busy()) is False


def test_reindex_compacts_what_a_blocked_rm_left_behind(root, monkeypatch):
    from tars import ingest
    path, runner = root
    _add(runner, "token hunter2zzz", "--title", "secretdoc")
    real_scrub = ingest.scrub_index
    monkeypatch.setattr(ingest, "scrub_index", lambda db: False)
    runner.invoke(main, ["rm", "raw/note/secretdoc.md", "--yes"])
    monkeypatch.setattr(ingest, "scrub_index", real_scrub)
    runner.invoke(main, ["reindex"])
    for f in path.glob("tars.db*"):
        assert b"hunter2zzz" not in f.read_bytes(), f.name
