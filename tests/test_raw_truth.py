"""Raw is truth: reading the archive must never change it.

Rebuilding the index, checking invariants, or parsing a file are reads. None
of them may drop a line, rewrite a file, or let one bad file take the rest of
the index down with it.
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
