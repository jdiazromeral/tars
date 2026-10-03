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


class Crash(Exception):
    pass


def _events(path):
    lines = (path / "log/ingestions.jsonl").read_text().splitlines()
    return [json.loads(line)["action"] for line in lines]


def test_log_survives_a_crash_mid_ingest(root, monkeypatch):
    # The log is the one piece of state reindex can't rebuild, so an event is
    # written before the vault changes: a crash may leave an event for a write
    # that never landed, but never a landed write with no event.
    path, runner = root
    from tars import store

    def crash(*args, **kwargs):
        raise Crash
    monkeypatch.setattr(store, "write_raw", crash)
    result = runner.invoke(main, ["add", "-", "--title", "Doomed"], input="doomed")
    assert isinstance(result.exception, Crash)
    assert _events(path) == ["added"]


def test_log_survives_a_crash_mid_remove(root, monkeypatch):
    path, runner = root
    added = runner.invoke(main, ["add", "-", "--title", "Kept"], input="kept")
    doc_id = added.output.split()[1]
    from tars import store

    def crash(*args, **kwargs):
        raise Crash
    monkeypatch.setattr(store, "raw_files", crash)
    result = runner.invoke(main, ["rm", doc_id, "--yes"])
    assert isinstance(result.exception, Crash)
    assert _events(path) == ["added", "deleted"]
