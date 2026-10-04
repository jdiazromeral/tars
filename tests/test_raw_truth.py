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
    path, runner = root
    for name in ("alpha", "bravo", "charlie"):
        _add(runner, f"{name} body", "--title", name)
    (path / "raw/note/bravo.md").write_text("")

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 1
    assert "raw/note/bravo.md" in result.output
    conn = db_mod.connect(path)
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2


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
