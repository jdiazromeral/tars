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


def test_finalize_clean_vault_skips_reindex_and_reports_clean(root):
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a",
                         "--concept", "atlas"], input="body")
    runner.invoke(main, ["hubs"])

    result = runner.invoke(main, ["finalize"])
    assert result.exit_code == 0, result.output
    assert "reindex: skipped (no drift)" in result.output
    assert "doctor:  clean" in result.output


def test_finalize_regenerates_hub_for_unhubbed_concept(root):
    # A freshly-tagged concept has no hub page until hubs runs; doctor flags it.
    # finalize should create the hub and leave doctor clean.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a",
                         "--concept", "brand-new"], input="body")
    assert runner.invoke(main, ["doctor"]).exit_code == 1

    result = runner.invoke(main, ["finalize"])
    assert result.exit_code == 0, result.output
    assert (path / "wiki/concepts/brand-new.md").exists()
    assert "doctor:  clean" in result.output


def test_finalize_reindexes_on_content_drift(root):
    # Hand-editing a raw body bypasses ingest and drifts the index; finalize
    # must detect the drift, reindex, and end clean.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "a"], input="body")
    raw = path / "raw/note/a.md"
    header, _, _ = raw.read_text().partition("---\n\n")
    raw.write_text(header + "---\n\nhand-edited content, bypassing ingest\n")
    assert runner.invoke(main, ["doctor"]).exit_code == 1

    result = runner.invoke(main, ["finalize"])
    assert result.exit_code == 0, result.output
    assert "drift cleared" in result.output
    assert "doctor:  clean" in result.output
