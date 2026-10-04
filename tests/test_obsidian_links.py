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


def test_a_non_string_annotates_property_is_unparseable(root):
    # Obsidian's property editor can turn a text property into a list; that is
    # a malformed raw file, reported like any other — not silently dropped.
    path, runner, _ = root
    runner.invoke(main, ["annotate", "PROJ-123", "x"])
    note = _annotation(path)
    note.write_text(note.read_text().replace(
        'annotates: "[[proj-123-migrate-auth-to-oidc]]"',
        'annotates:\n- "[[proj-123-migrate-auth-to-oidc]]"'))
    with pytest.raises(store.UnparseableRaw, match="annotates"):
        store.read_raw(note)


def test_the_target_id_is_quoted_so_no_yaml_reads_it_as_a_number(root):
    # Ids are hex; an all-digit one (036199599289) is an integer to YAML 1.2
    # readers such as Obsidian, which would drop the leading zero on re-save.
    path, runner, target_id = root
    runner.invoke(main, ["annotate", "PROJ-123", "x"])
    note = _annotation(path)
    runner.invoke(main, ["tag", "raw/note/" + note.name, "--concept", "sso"])  # a rewrite
    assert f'annotates_id: "{target_id}"' in note.read_text()


# --- review of #14 ---

def _origin(note):
    return store.read_raw(note).origin


def test_append_to_an_annotation_keeps_the_link(root):
    path, runner, _ = root
    runner.invoke(main, ["annotate", "PROJ-123", "my note"])
    note = _annotation(path)
    result = runner.invoke(main, ["add", "-", "--append", "--origin", _origin(note)], input="more")
    assert result.exit_code == 0, result.output
    assert _frontmatter(note)["annotates"] == "[[proj-123-migrate-auth-to-oidc]]"


def test_annotating_again_restores_a_lost_link(root):
    path, runner, _ = root
    runner.invoke(main, ["annotate", "PROJ-123", "my note"])
    note = _annotation(path)
    note.write_text(note.read_text().replace(
        'annotates: "[[proj-123-migrate-auth-to-oidc]]"\n', ""))
    result = runner.invoke(main, ["annotate", "PROJ-123", "my note"])
    assert result.output.startswith("updated"), result.output
    assert _frontmatter(note)["annotates"] == "[[proj-123-migrate-auth-to-oidc]]"


def test_rm_drops_the_link_so_the_targets_title_leaves_the_vault(root):
    # The link's stem is the target's file name, made from its title: a secret
    # there must go with the target, not live on in the kept annotations.
    path, runner, _ = root
    runner.invoke(main, ["add", "-", "--connector", "jira", "--origin", "jira:P-1",
                         "--title", "P-1 token sk-SECRET123"], input="x")
    note_id = runner.invoke(main, ["annotate", "P-1", "rotate it"]).output.split()[1]
    result = runner.invoke(main, ["rm", "jira:P-1", "--yes"])

    note = next(p for p in path.glob("raw/note/*.md") if store.read_raw(p).id == note_id)
    assert "secret123" not in note.read_text().lower()
    assert "annotates" not in _frontmatter(note)
    assert store.read_raw(note).text == "rotate it"  # the user's words are kept
    assert f"annotation {note_id} kept" in result.output
    assert "still referenced in" not in result.output  # reported once, not twice


def test_a_legacy_annotates_key_is_read_as_annotates_id(root):
    # #13 shipped meta.annotates; such a note must still count as an annotation.
    path, runner, target_id = root
    runner.invoke(main, ["annotate", "PROJ-123", "old style"])
    note = _annotation(path)
    note.write_text(note.read_text()
                    .replace("  annotates_id:", "  annotates:")
                    .replace('annotates: "[[proj-123-migrate-auth-to-oidc]]"\n', ""))
    runner.invoke(main, ["reindex"])
    listed = runner.invoke(main, ["show", "PROJ-123", "--annotations"]).output
    assert "old style" in listed


def test_the_link_names_the_file_the_target_really_lives_in(root):
    # A stale index row (a hand rename, not yet reindexed) must not produce a
    # link that is dangling from the moment it is written.
    path, runner, _ = root
    (path / TARGET).rename(path / "raw/jira/renamed.md")
    runner.invoke(main, ["annotate", "PROJ-123", "x"])
    assert _frontmatter(_annotation(path))["annotates"] == "[[renamed]]"


def test_the_quoting_representer_stays_local_to_tars():
    assert store._DoubleQuoted not in yaml.SafeDumper.yaml_representers
