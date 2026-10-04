"""An annotation's link to its target is a top-level `annotates: "[[stem]]"`
property, which Obsidian reads as a link (backlinks panel). tars itself
resolves the target by `meta.annotates_id`; both are written once by
`annotate` and carried through every later rewrite."""

import pytest
import yaml
from click.testing import CliRunner

from tars import store
from tars.cli import main

TARGET = "raw/jira/proj-123-migrate-auth-to-oidc.md"


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    target = runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:PROJ-123",
                                  "--title", "PROJ-123 Migrate auth to OIDC",
                                  "--concept", "auth"], input="Migrate auth.")
    return tmp_path, runner, target.output.split()[1]


def _frontmatter(path):
    return yaml.safe_load(path.read_text().split("\n---\n", 1)[0][4:])


def _annotation(path):
    return next(path.glob("raw/note/*.md"))


def test_annotation_carries_an_obsidian_link_and_the_target_id(root):
    path, runner, target_id = root
    runner.invoke(main, ["annotate", "PROJ-123", "the estimate ignores SSO"])

    fm = _frontmatter(_annotation(path))
    assert fm["annotates"] == "[[proj-123-migrate-auth-to-oidc]]"  # a quoted YAML string
    assert fm["meta"] == {"annotates_id": target_id}
    assert '"[[proj-123-migrate-auth-to-oidc]]"' in _annotation(path).read_text()


@pytest.mark.parametrize("rewrite", [
    ["tag", "{note}", "--concept", "sso"],
    ["untag", "{note}", "--concept", "auth"],
    ["normalize"],
    ["reindex"],
])
def test_every_rewrite_keeps_the_link(root, rewrite):
    path, runner, _ = root
    (path / "vocab.yml").write_text("SSO:\n  variants: [sso]\n")
    note = runner.invoke(main, ["annotate", "PROJ-123", "the sso estimate"]).output.split()[1]
    result = runner.invoke(main, [a.format(note=note) for a in rewrite])
    assert result.exit_code == 0, result.output
    assert _frontmatter(_annotation(path))["annotates"] == "[[proj-123-migrate-auth-to-oidc]]"


def test_other_documents_frontmatter_is_unchanged(root):
    path, _, _ = root
    assert "annotates" not in _frontmatter(path / TARGET)


def test_show_and_search_still_find_annotations(root):
    _, runner, _ = root
    runner.invoke(main, ["annotate", "PROJ-123", "the kerberos fallback"])
    assert "kerberos" in runner.invoke(main, ["show", "PROJ-123", "--annotations"]).output
    assert "↳ on [[proj-123-migrate-auth-to-oidc]]" in runner.invoke(
        main, ["search", "kerberos"]).output


def test_rm_says_the_obsidian_link_breaks(root):
    path, runner, _ = root
    note = runner.invoke(main, ["annotate", "PROJ-123", "keep me"]).output.split()[1]
    result = runner.invoke(main, ["rm", "jira:PROJ-123", "--yes"])
    assert f"annotation {note} still points at it" in result.output
    assert "[[proj-123-migrate-auth-to-oidc]] link in Obsidian is now unresolved" in result.output


def test_a_non_string_annotates_property_is_unparseable(root):
    # Obsidian's property editor can turn a text property into a list; that is
    # a malformed raw file, reported like any other — not silently dropped.
    path, runner, _ = root
    runner.invoke(main, ["annotate", "PROJ-123", "x"])
    note = _annotation(path)
    note.write_text(note.read_text().replace(
        "annotates: '[[proj-123-migrate-auth-to-oidc]]'",
        "annotates:\n- '[[proj-123-migrate-auth-to-oidc]]'").replace(
        'annotates: "[[proj-123-migrate-auth-to-oidc]]"',
        'annotates:\n- "[[proj-123-migrate-auth-to-oidc]]"'))
    with pytest.raises(store.UnparseableRaw, match="annotates"):
        store.read_raw(note)
