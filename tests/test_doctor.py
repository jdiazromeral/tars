import json

import pytest
from click.testing import CliRunner

from tars import doctor
from tars.cli import main


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


def test_clean_vault_reports_no_findings(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a",
                         "--concept", "atlas"], input="body")
    runner.invoke(main, ["hubs"])

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output


def test_dangling_link_in_task_is_flagged(root):
    path, runner = root
    (path / "tasks/2026-07-08-check.md").write_text(
        "do the thing\n\nSource: [[nonexistent|Ghost]]\n"
    )
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "dangling-link" in result.output
    assert "tasks/2026-07-08-check.md" in result.output
    assert "nonexistent" in result.output


def test_task_orphaned_by_rm_is_flagged(root):
    # The exact scenario the roadmap calls out: `tars rm` deletes a source
    # doc, and the task that cited it is left pointing at nothing.
    path, runner = root
    add = runner.invoke(main, ["add", "-", "--origin", "note:secret", "--title", "Oops"],
                        input="accidentally pasted secret")
    doc_id = add.output.split()[1]
    (path / "tasks/2026-07-08-check.md").write_text("do the thing\n\nSource: [[oops|Oops]]\n")

    runner.invoke(main, ["rm", doc_id, "--yes"])
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "dangling-link" in result.output
    assert "[[oops]]" in result.output


def test_unhubbed_concept_is_flagged_until_hubs_runs(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a",
                         "--concept", "brand-new"], input="body")

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "unhubbed-concept" in result.output
    assert "brand-new" in result.output

    runner.invoke(main, ["hubs"])
    assert runner.invoke(main, ["doctor"]).exit_code == 0


def test_ambiguous_stem_across_layers_is_flagged(root):
    # A capture whose title slugs to an existing concept would shadow the hub
    # in Obsidian's flat [[link]] namespace. Ingest now suffixes it, so the
    # only way to get a clash is to create one by hand.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a",
                         "--concept", "design-patterns"], input="body")
    runner.invoke(main, ["hubs"])
    assert runner.invoke(main, ["doctor"]).exit_code == 0

    (path / "raw/web").mkdir(parents=True, exist_ok=True)
    (path / "raw/web/design-patterns.md").write_text(
        "---\nid: abc123\nconnector: web\norigin: https://example.com/dp\n"
        "title: Design Patterns\naliases: []\ncaptured_at: 2026-07-01T00:00:00Z\n"
        "tags: []\nconcepts: []\nmeta: {}\n---\n\nshadows the hub\n"
    )
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "ambiguous-stem" in result.output
    assert "[[design-patterns]]" in result.output
    assert "raw/web/design-patterns.md" in result.output
    assert "wiki/concepts/design-patterns.md" in result.output


def test_ingest_will_not_shadow_a_concept_hub(root):
    # The preventive half: same slug, but through the CLI. The capture gets an
    # id suffix so the hub keeps the plain name and doctor stays clean.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:seed", "--title", "seed",
                         "--concept", "design-patterns"], input="body")
    runner.invoke(main, ["hubs"])

    add = runner.invoke(main, ["add", "-", "--origin", "note:dp",
                               "--title", "Design Patterns"], input="the catalog page")
    assert add.exit_code == 0, add.output
    doc_id = add.output.split()[1]

    assert not (path / "raw/note/design-patterns.md").exists()
    assert (path / f"raw/note/design-patterns-{doc_id[:6]}.md").exists()
    assert runner.invoke(main, ["doctor"]).exit_code == 0


def test_reingest_keeps_its_own_unsuffixed_name(root):
    # Guard the ownership short-circuit: a doc that already owns the plain
    # name must not drift to a suffixed one when its content is updated.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "Alpha"], input="v1")
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "Alpha"], input="v2")

    assert "v2" in (path / "raw/note/alpha.md").read_text()
    assert sorted(p.name for p in path.glob("raw/note/alpha*.md")) == ["alpha.md"]


def test_db_drift_when_raw_file_missing_from_index(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a"], input="body")
    (path / "raw/note/a.md").unlink()

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "db-drift" in result.output
    assert "raw file missing" in result.output


def test_db_drift_when_raw_file_not_yet_indexed(root):
    path, runner = root
    conn_dir = path / "raw/note"
    conn_dir.mkdir(parents=True, exist_ok=True)
    (conn_dir / "unindexed.md").write_text(
        "---\nid: abc123\nconnector: note\norigin: note:manual\ntitle: manual\n"
        "aliases: []\ncaptured_at: 2026-07-01T00:00:00Z\ntags: []\nconcepts: []\nmeta: {}\n"
        "---\n\nhand-dropped raw file, never ingested\n"
    )
    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "db-drift" in result.output
    assert "not in index" in result.output


def test_db_drift_when_content_hash_stale(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a"], input="body")
    raw = path / "raw/note/a.md"
    header, _, _ = raw.read_text().partition("---\n\n")
    raw.write_text(header + "---\n\nhand-edited content, bypassing ingest\n")

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 1
    assert "db-drift" in result.output
    assert "content hash stale" in result.output


def test_json_output(root):
    path, runner = root
    (path / "tasks/t.md").write_text("Source: [[ghost]]\n")
    result = runner.invoke(main, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload[0]["check"] == "dangling-link"
    assert payload[0]["path"] == "tasks/t.md"


def test_raw_body_brackets_do_not_false_positive(root):
    # raw/ is captured third-party content — literal "[[...]]" in a source
    # document (e.g. wiki-style markup pasted from elsewhere) is not a link
    # the vault promises to keep valid, so it must not be scanned.
    _, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a"],
                  input="see [[SomeExternalWikiPage]] for details")
    assert runner.invoke(main, ["doctor"]).exit_code == 0


def test_doctor_module_run_matches_cli(root):
    from tars import db as database

    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a",
                         "--concept", "atlas"], input="body")
    findings = doctor.run(path, database.connect(path))
    assert findings == [doctor.Finding(
        "unhubbed-concept", "wiki/concepts/atlas.md",
        "concept 'atlas' has shelved docs but no hub page — run `tars hubs`",
    )]
