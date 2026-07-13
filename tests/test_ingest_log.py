import json

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


def test_log_records_lifecycle_and_skips_unchanged(root):
    path, runner = root
    args = ["add", "-", "--origin", "note:life", "--title", "Life"]
    first = runner.invoke(main, args, input="first")
    doc_id = first.output.split()[1]
    runner.invoke(main, args, input="first")     # unchanged -> must NOT log
    runner.invoke(main, args, input="second")    # updated
    runner.invoke(main, ["rm", doc_id, "--yes"])  # deleted

    lines = (path / "log/ingestions.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["action"] for e in events] == ["added", "updated", "deleted"]
    assert all(e["id"] == doc_id for e in events)
    assert all(e["ts"].endswith("Z") for e in events)


def test_log_command_shows_newest_first(root):
    _, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:a", "--title", "Alpha"], input="a")
    runner.invoke(main, ["add", "-", "--origin", "note:b", "--title", "Beta"], input="b")

    result = runner.invoke(main, ["log"])
    assert result.exit_code == 0, result.output
    lines = [line for line in result.output.splitlines() if line.strip()]
    assert "Beta" in lines[0]      # newest first
    assert "Alpha" in lines[1]

    limited = runner.invoke(main, ["log", "-n", "1", "--json"])
    payload = json.loads(limited.output)
    assert len(payload) == 1 and payload[0]["title"] == "Beta"


def test_log_empty_when_nothing_ingested(root):
    _, runner = root
    result = runner.invoke(main, ["log"])
    assert result.exit_code == 0
    assert "no ingestion events" in result.output


def test_reindex_does_not_pollute_log(root):
    # Rebuilding the cache is not an ingestion event: the log must be untouched.
    path, runner = root
    runner.invoke(main, ["add", "-", "--origin", "note:r", "--title", "Kept"], input="body")
    before = (path / "log/ingestions.jsonl").read_text()

    result = runner.invoke(main, ["reindex"])
    assert result.exit_code == 0, result.output
    assert (path / "log/ingestions.jsonl").read_text() == before
