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
    ["rm", "{ref}"],  # a loose ref is confirmed at the prompt (rm --yes wants an exact one)
])
def test_every_document_command_takes_a_reference(root, command):
    _, runner = root
    _ticket(runner)
    result = runner.invoke(main, [a.format(ref="PROJ-123") for a in command], input="y\n")
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


# --- review of #13: loose forms never pick a document silently ---

def test_a_key_that_is_also_a_file_name_is_a_tie_not_a_guess(root):
    # A note titled "PROJ-123" and the ticket PROJ-123: rm --yes on the
    # wrong one deletes without asking, so the tie must be an error.
    _, runner = root
    ticket = _ticket(runner)
    note = runner.invoke(main, ["add", "-", "--title", "PROJ-123"],
                         input="my note").output.split()[1]
    result = runner.invoke(main, ["rm", "PROJ-123"], input="y\n")
    assert result.exit_code != 0
    assert ticket in result.output and note in result.output


def test_a_github_key_is_not_cut_at_the_hash(root):
    _, runner = root
    pr = runner.invoke(main, ["add", "-", "--connector", "github",
                              "--origin", "github:javi/tars#13", "--title", "PR 13"],
                       input="pr").output.split()[1]
    runner.invoke(main, ["add", "-", "--title", "tars"], input="a note named tars")
    result = runner.invoke(main, ["show", "javi/tars#13", "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {pr}" in result.output


def test_a_url_matches_its_canonical_origin(root):
    _, runner = root
    page = runner.invoke(main, ["add", "-", "--connector", "web",
                                "--origin", "https://example.com/blog/my-post",
                                "--title", "My post"], input="page").output.split()[1]
    result = runner.invoke(main, ["show", "https://Example.com/blog/my-post?utm_source=x#intro",
                                  "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {page}" in result.output


@pytest.mark.parametrize("ref", ["[[My Plan]]", "[[my plan]]", "[[My Plan\\|label]]"])
def test_file_names_match_case_insensitively_and_with_escaped_pipes(root, ref):
    path, runner = root
    doc = runner.invoke(main, ["add", "-", "--title", "plan"], input="the plan").output.split()[1]
    (path / "raw/note/plan.md").rename(path / "raw/note/My Plan.md")
    runner.invoke(main, ["reindex"])
    result = runner.invoke(main, ["show", ref, "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {doc}" in result.output


# --- second review of #13 ---

def test_a_path_or_url_is_never_cut_down_to_a_file_name(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--title", "README"], input="my readme notes")
    for ref in ("https://github.com/x/y/blob/main/README.md", "raw/jira/readme.md"):
        result = runner.invoke(main, ["show", ref])
        assert result.exit_code != 0, (ref, result.output)
        assert "no document matches" in result.output


def test_a_raw_path_is_an_exact_reference(root):
    _, runner = root
    doc = runner.invoke(main, ["add", "-", "--title", "README"], input="x").output.split()[1]
    result = runner.invoke(main, ["rm", "raw/note/readme.md", "--yes"])
    assert result.exit_code == 0, result.output
    assert doc in result.output


def test_rm_yes_refuses_a_loose_reference(root):
    # The ticket was never synced; "PROJ-123" uniquely matches the user's own
    # note by name. Deleting without a prompt on a guess is what --yes forbids.
    path, runner = root
    runner.invoke(main, ["add", "-", "--title", "PROJ-123"], input="my note")
    result = runner.invoke(main, ["rm", "PROJ-123", "--yes"])
    assert result.exit_code != 0
    assert "exact reference" in result.output
    assert (path / "raw/note/proj-123.md").exists()


def test_rm_without_yes_names_what_a_loose_reference_resolved_to(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--title", "PROJ-123"], input="my note")
    result = runner.invoke(main, ["rm", "PROJ-123"], input="n\n")
    assert "[note]" in result.output and "PROJ-123" in result.output
    assert (path / "raw/note/proj-123.md").exists()


@pytest.mark.parametrize("ref", ["[[reunión-semanal#Acuerdos]]", "[[reunión-semanal#^abc123]]",
                                 "[[REUNIÓN-SEMANAL]]", "Reunión-Semanal"])
def test_heading_links_and_non_ascii_case(root, ref):
    _, runner = root
    doc = runner.invoke(main, ["add", "-", "--title", "Reunión semanal"],
                        input="acta").output.split()[1]
    result = runner.invoke(main, ["show", ref, "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {doc}" in result.output


# --- third review of #13 ---

def test_rm_yes_on_a_tied_exact_reference_lists_candidates(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--connector", "web", "--origin", "https://ex.com/a",
                         "--title", "page"], input="page")
    runner.invoke(main, ["add", "-", "--origin", "https://ex.com/a", "--title", "excerpt"],
                  input="excerpt")
    result = runner.invoke(main, ["rm", "https://ex.com/a", "--yes"])
    assert result.exit_code == 1, result.output  # a clean error, not a traceback
    assert "matches 2 documents" in result.output


@pytest.mark.parametrize("form", ["absolute", "no-suffix"])
def test_the_path_show_prints_is_a_reference(root, form):
    path, runner = root
    doc_id = _ticket(runner)
    printed = runner.invoke(main, ["show", doc_id, "--path"]).output.strip()
    ref = printed if form == "absolute" else "raw/jira/proj-123-migrate-auth-to-oidc"
    result = runner.invoke(main, ["rm", ref, "--yes"])
    assert result.exit_code == 0, result.output
    assert doc_id in result.output


def test_non_ascii_capitals_fold_on_both_sides(root):
    _, runner = root
    doc = runner.invoke(main, ["add", "-", "--connector", "granola", "--origin",
                               "granola:ÓRDENES-2026", "--title", "x"],
                        input="acta").output.split()[1]
    result = runner.invoke(main, ["show", "órdenes-2026", "--head", "2"])
    assert result.exit_code == 0, result.output
    assert f"id: {doc}" in result.output
