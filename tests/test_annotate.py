"""`tars annotate`: your words about a captured document live in a note of
their own that points at it. The target is never touched, so a re-sync can't
lose the annotation and the annotation can't corrupt the source."""

import json

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


def _ticket(runner, text="Migrate auth to OIDC. Estimate 3 sprints."):
    result = runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:PROJ-123",
                                  "--title", "PROJ-123 Migrate auth to OIDC",
                                  "--concept", "auth"], input=text)
    assert result.exit_code == 0, result.output
    return result.output.split()[1]


def _annotate(runner, *args, stdin=None):
    return runner.invoke(main, ["annotate", *args], input=stdin)


def test_annotation_is_a_note_that_points_at_its_target(root):
    path, runner = root
    target = _ticket(runner)
    before = (path / "raw/jira/proj-123-migrate-auth-to-oidc.md").read_text()

    result = _annotate(runner, "PROJ-123", "the estimate ignores the SSO migration")
    assert result.exit_code == 0, result.output
    assert result.output.startswith("added")
    assert "on [[proj-123-migrate-auth-to-oidc]]" in result.output

    note_id = result.output.split()[1]
    note = store.read_raw(next(path.glob("raw/note/*.md")))
    assert note.id == note_id
    assert note.connector == "note"
    assert note.text == "the estimate ignores the SSO migration"
    assert note.meta["annotates"] == target
    assert note.concepts == ["auth"]  # inherited, so it lands in the same hubs
    # the target is untouched, byte for byte
    assert (path / "raw/jira/proj-123-migrate-auth-to-oidc.md").read_text() == before


def test_annotation_takes_stdin_and_extra_concepts(root):
    path, runner = root
    _ticket(runner)
    result = _annotate(runner, "jira:PROJ-123", "-", "--concept", "sso",
                       stdin="line one\nline two\n")
    assert result.exit_code == 0, result.output
    note = store.read_raw(next(path.glob("raw/note/*.md")))
    assert note.text == "line one\nline two"
    assert note.concepts == ["auth", "sso"]


def test_the_same_annotation_twice_is_unchanged(root):
    # An agent retrying the command must not duplicate the note.
    path, runner = root
    _ticket(runner)
    _annotate(runner, "PROJ-123", "same words")
    result = _annotate(runner, "PROJ-123", "same words")
    assert result.output.startswith("unchanged"), result.output
    assert len(list(path.glob("raw/note/*.md"))) == 1


def test_the_same_words_on_two_targets_are_two_annotations(root):
    path, runner = root
    _ticket(runner)
    runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:PROJ-124",
                         "--title", "PROJ-124 Other"], input="other")
    _annotate(runner, "PROJ-123", "needs a design review")
    _annotate(runner, "PROJ-124", "needs a design review")
    assert len(list(path.glob("raw/note/*.md"))) == 2


def test_a_resync_of_the_target_keeps_its_annotations(root):
    path, runner = root
    _ticket(runner)
    _annotate(runner, "PROJ-123", "my take")
    _ticket(runner, text="Migrate auth to OIDC. Estimate 5 sprints.")  # upstream changed

    result = runner.invoke(main, ["show", "PROJ-123"])
    assert "Estimate 5 sprints" in result.output
    assert "my take" in result.output


def test_show_lists_annotations_after_the_document(root):
    _, runner = root
    _ticket(runner)
    note_id = _annotate(runner, "PROJ-123", "first thought\nmore detail").output.split()[1]

    for args in (["show", "PROJ-123"], ["show", "PROJ-123", "--head", "3"]):
        out = runner.invoke(main, args).output
        assert "annotations (1)" in out, out
        assert note_id in out and "first thought" in out
        assert "more detail" not in out  # one line each; `tars show <id>` for the rest
    assert "annotations (" not in runner.invoke(main, ["show", "PROJ-123", "--path"]).output


def test_search_marks_an_annotation_hit_with_its_target(root):
    _, runner = root
    _ticket(runner)
    _annotate(runner, "PROJ-123", "the kerberos fallback is unowned")

    out = runner.invoke(main, ["search", "kerberos"]).output
    assert "↳ on [[proj-123-migrate-auth-to-oidc]]" in out
    hits = json.loads(runner.invoke(main, ["search", "kerberos", "--json"]).output)
    assert hits[0]["annotates"] == "proj-123-migrate-auth-to-oidc"


def test_rm_warns_about_annotations_and_keeps_them(root):
    path, runner = root
    _ticket(runner)
    note_id = _annotate(runner, "PROJ-123", "keep me").output.split()[1]

    result = runner.invoke(main, ["rm", "PROJ-123", "--yes"])
    assert result.exit_code == 0, result.output
    assert f"annotation {note_id} still points at it" in result.output
    assert len(list(path.glob("raw/note/*.md"))) == 1


def test_an_annotation_outlives_its_target_without_nagging(root):
    # rm warns once, at the moment it matters; afterwards the note is simply
    # the user's words — still searchable, and doctor has nothing to say.
    _, runner = root
    _ticket(runner)
    _annotate(runner, "PROJ-123", "the kerberos fallback is unowned")
    runner.invoke(main, ["rm", "PROJ-123", "--yes"])
    runner.invoke(main, ["hubs"])

    doctor = runner.invoke(main, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    out = runner.invoke(main, ["search", "kerberos"]).output
    assert "kerberos" in out and "↳ on" not in out


def test_annotating_nothing_is_an_error(root):
    _, runner = root
    _ticket(runner)
    assert _annotate(runner, "PROJ-123", "-", stdin="  \n").exit_code != 0
    assert "no document matches" in _annotate(runner, "PROJ-999", "x").output
