"""Raw is truth: reading the archive must never change it.

Rebuilding the index, checking invariants, or parsing a file are reads. None
of them may drop a line, rewrite a file, or let one bad file take the rest of
the index down with it.
"""

import pytest
from click.testing import CliRunner

from tars import db as db_mod
from tars import ingest, store
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


def test_unshelved_body_starting_with_concepts_survives(root):
    # Only tars writes the derived `Concepts:` line, and only for a shelved doc;
    # a user's own text that happens to start that way is content, not markup.
    path, runner = root
    text = "Concepts: auth, billing - agenda for Monday\n\nSecond paragraph."
    _add(runner, text, "--title", "Agenda")
    raw = path / "raw/note/agenda.md"

    assert store.read_raw(raw).text == text
    assert runner.invoke(main, ["doctor"]).exit_code == 0
    assert runner.invoke(main, ["reindex"]).exit_code == 0
    assert store.read_raw(raw).text == text


def test_shelved_doc_keeps_its_own_concepts_line(root):
    # The derived line is stripped; a user line below it with the same prefix is kept.
    path, runner = root
    text = "Concepts: the user's own line\nbody"
    _add(runner, text, "--title", "Shelved", "--concept", "auth")

    doc = store.read_raw(path / "raw/note/shelved.md")
    assert doc.text == text
    assert doc.concepts == ["auth"]


def test_reindex_never_rewrites_raw(root):
    # Hand-added frontmatter and a vocab rule added after capture are both
    # things a rebuild used to "fix" by rewriting the file; a cache rebuild
    # has no business touching the archive. Normalizing is `tars normalize`'s job.
    path, runner = root
    _add(runner, "we use acne for billing", "--title", "Billing")
    raw = path / "raw/note/billing.md"
    raw.write_text(raw.read_text().replace("meta: {}\n", "meta: {}\nreviewed: true\n"))
    (path / "vocab.yml").write_text("Acme:\n  variants: [acne]\n")
    before = raw.read_bytes()

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 0, result.output
    assert raw.read_bytes() == before


def test_reindex_skips_unparseable_file_and_indexes_the_rest(root):
    # One truncated file must not leave the index a third full: every other
    # document is indexed, the bad one is named, and the exit code says so.
    # Its old row stays (see test_unparseable_file_keeps_its_index_row).
    path, runner = root
    for name in ("alpha", "bravo", "charlie"):
        _add(runner, f"{name} body", "--title", name)
    (path / "raw/note/bravo.md").write_text("")

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 1
    assert "raw/note/bravo.md" in result.output
    conn = db_mod.connect(path)
    rows = {r["raw_dir"] for r in conn.execute("SELECT raw_dir FROM documents")}
    assert rows == {"raw/note/alpha.md", "raw/note/bravo.md", "raw/note/charlie.md"}


def test_reindex_failure_keeps_the_old_index(root, monkeypatch):
    # The rebuild is one transaction: a crash halfway leaves the previous
    # index intact instead of a committed DELETE and a partial refill.
    path, runner = root
    for name in ("alpha", "bravo", "charlie"):
        _add(runner, f"{name} body", "--title", name)

    calls = []
    real = ingest.index_doc

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("disk on fire")
        return real(*args, **kwargs)

    monkeypatch.setattr(ingest, "index_doc", flaky)
    conn = db_mod.connect(path)
    with pytest.raises(RuntimeError):
        ingest.reindex(path, conn)
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 3


def test_doctor_and_finalize_report_unparseable_raw(root):
    # A file that doesn't parse is a finding, not a traceback.
    path, runner = root
    _add(runner, "fine", "--title", "ok")
    (path / "raw/note/broken.md").write_text("---\nid: x\n")

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "unparseable-raw" in result.output
    assert "raw/note/broken.md" in result.output

    result = runner.invoke(main, ["finalize"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "raw/note/broken.md" in result.output


def test_unparseable_file_keeps_its_index_row(root):
    # Dropping the row of a file that failed to parse made the next append see
    # "no document" and overwrite the whole file. The row stays until the
    # file is repaired, so an append fails loudly instead.
    path, runner = root
    for line in ("entry one", "entry two"):
        _add(runner, line, "--append", "--connector", "activity",
             "--origin", "activity:d1", "--title", "d1")
    raw = path / "raw/activity/d1.md"
    raw.write_text(raw.read_text().replace("tags: []", "tags: [unclosed"))

    assert runner.invoke(main, ["reindex"]).exit_code == 1
    assert "d1" in runner.invoke(main, ["list"]).output

    result = runner.invoke(main, ["add", "-", "--append", "--connector", "activity",
                                  "--origin", "activity:d1", "--title", "d1"],
                           input="entry three")
    assert result.exit_code != 0
    assert "entry one" in raw.read_text() and "entry two" in raw.read_text()


@pytest.mark.parametrize("bad_field", ["connector: null", "title: [a, b]", "origin: ''"])
def test_invalid_field_values_are_unparseable(root, bad_field):
    path, runner = root
    _add(runner, "fine", "--title", "ok")
    key = bad_field.split(":")[0]
    fields = {"id": "abc", "connector": "note", "origin": "note:x", "title": "t"}
    fields[key] = None
    header = "\n".join(f"{k}: {v}" for k, v in fields.items() if v is not None)
    (path / "raw/note/bad.md").write_text(f"---\n{header}\n{bad_field}\n---\n\nbody\n")

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "raw/note/bad.md" in result.output


def test_unreadable_raw_path_is_a_finding(root):
    path, runner = root
    _add(runner, "fine", "--title", "ok")
    (path / "raw/note/link.md").symlink_to(path / "nowhere.md")

    for command in (["doctor"], ["reindex"]):
        result = runner.invoke(main, command)
        assert result.exit_code == 1, result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "raw/note/link.md" in result.output


def test_migrate_skips_unparseable_and_keeps_the_old_marker(root):
    # One bad file must not abort migrate halfway; the rest is rewritten, the
    # bad file named, and the marker stays v1 so a re-run finishes the job.
    path, runner = root
    (path / ".tars").write_text("version: 1\n")
    good = path / "raw/note/good.md"
    good.parent.mkdir(parents=True)
    good.write_text("---\nid: 000000000001\nconnector: note\norigin: note:g\n"
                    "title: good\ntags: []\nmeta: {}\n---\n\nConcepts: [[auth]]\n\nbody\n")
    bad = path / "raw/note/bad.md"
    bad.write_text("---\nid: 000000000002\n")

    result = runner.invoke(main, ["migrate"])
    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "raw/note/bad.md" in result.output
    assert store.vault_version(path) == 1
    assert "concepts:\n- auth" in good.read_text()

    bad.unlink()
    assert runner.invoke(main, ["migrate"]).exit_code == 0
    assert store.vault_version(path) == store.SCHEMA_VERSION


def test_only_the_exact_derived_line_is_stripped(root):
    # Shelved doc whose derived line was lost: the user's own first line,
    # which differs from the rendering of its concepts, is content.
    path, runner = root
    raw = path / "raw/note/s.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("---\nid: 000000000003\nconnector: note\norigin: note:s\ntitle: s\n"
                   "tags: []\nconcepts:\n- auth\nmeta: {}\n---\n\nConcepts: my own words\nbody\n")
    assert store.read_raw(raw).text == "Concepts: my own words\nbody"


def test_missing_concepts_key_does_not_switch_to_v1_parsing(root):
    # Only `migrate` reads v1 files (every other command refuses a v1 vault),
    # so a v2 file whose `concepts:` key was deleted by hand is still v2:
    # no concepts, and the user's own leading "Concepts:" line is kept.
    path, runner = root
    text = "Concepts: auth, billing - agenda\nreal body"
    _add(runner, text, "--title", "Agenda")
    raw = path / "raw/note/agenda.md"
    raw.write_text(raw.read_text().replace("concepts: []\n", ""))

    doc = store.read_raw(raw)
    assert doc.text == text
    assert doc.concepts == []
