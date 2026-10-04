"""Every command that acts on one document accepts the same reference: its id,
its origin, its file name (the wiki-link you see in Obsidian), or the source's
own key (PROJ-123). The match is exact, never fuzzy; a miss or a tie is an
error that says what to run next."""

import pytest
from click.testing import CliRunner

from tars.cli import main


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


def _ticket(runner):
    result = runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:PROJ-123",
                                  "--title", "PROJ-123 Migrate auth to OIDC"],
                           input="Migrate auth to OIDC.")
    assert result.exit_code == 0, result.output
    return result.output.split()[1]


@pytest.mark.parametrize("ref", [
    "{id}",
    "jira:PROJ-123",
    "proj-123-migrate-auth-to-oidc",
    "[[proj-123-migrate-auth-to-oidc]]",
    "[[proj-123-migrate-auth-to-oidc|the OIDC ticket]]",
    "raw/jira/proj-123-migrate-auth-to-oidc.md",
    "PROJ-123",
    "proj-123",
])
def test_every_form_of_reference_finds_the_document(root, ref):
    _, runner = root
    doc_id = _ticket(runner)
    result = runner.invoke(main, ["show", ref.format(id=doc_id), "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {doc_id}" in result.output


@pytest.mark.parametrize("command", [
    ["show", "{ref}"],
    ["tag", "{ref}", "--concept", "auth"],
    ["untag", "{ref}", "--concept", "auth"],
    ["promote", "{ref}", "--title", "Why OIDC"],
    ["rm", "{ref}", "--yes"],
])
def test_every_document_command_takes_a_reference(root, command):
    _, runner = root
    _ticket(runner)
    result = runner.invoke(main, [a.format(ref="PROJ-123") for a in command])
    assert result.exit_code == 0, result.output


def test_tag_reports_the_resolved_id(root):
    # An agent that passed a key gets the id back, so its next call is exact.
    _, runner = root
    doc_id = _ticket(runner)
    result = runner.invoke(main, ["tag", "PROJ-123", "--concept", "auth"])
    assert doc_id in result.output


def test_a_miss_says_what_to_run_next(root):
    _, runner = root
    _ticket(runner)
    result = runner.invoke(main, ["show", "PROJ-999"])
    assert result.exit_code != 0
    assert "no document matches 'PROJ-999'" in result.output
    assert "tars search" in result.output


def test_a_tie_lists_the_candidates_instead_of_guessing(root):
    _, runner = root
    a = runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:X-1",
                             "--title", "a"], input="a").output.split()[1]
    b = runner.invoke(main, ["add", "-", "--connector", "github", "--origin", "github:X-1",
                             "--title", "b"], input="b").output.split()[1]
    result = runner.invoke(main, ["show", "X-1"])
    assert result.exit_code != 0
    assert a in result.output and b in result.output
    assert "pass one of these ids" in result.output


def test_an_exact_id_wins_over_other_forms(root):
    # Tiers are tried in order (id, origin, file, source key) and stop at the
    # first match, so a precise reference is never made ambiguous by a loose one.
    _, runner = root
    doc_id = _ticket(runner)
    runner.invoke(main, ["add", "-", "--title", doc_id], input="a note titled like an id")
    result = runner.invoke(main, ["show", doc_id, "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {doc_id}" in result.output
