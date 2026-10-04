"""Sync windows never lose items: a gap is a silent loss, an overlap is free
(re-ingesting unchanged content is a no-op). So watermarks are handed to each
source in a form it reads correctly, erring early, and a capped sweep always
makes progress from the oldest end."""

import json

import pytest
from click.testing import CliRunner

from tars import syncstate
from tars.cli import main
from tars.connectors import slack


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    runner = CliRunner()
    result = runner.invoke(main, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path, runner


# --- watermark formats ---------------------------------------------------------

@pytest.mark.parametrize("fmt, expected", [
    ("iso", "2026-10-04T08:30:00Z"),
    ("epoch", "1791102600"),       # Gmail after:<epoch>, Slack oldest=<epoch>
    ("jql", "2026-10-03"),         # a day early: JQL reads dates in the Jira user's zone
])
def test_cursor_prints_the_watermark_in_each_sources_form(root, fmt, expected):
    _, runner = root
    runner.invoke(main, ["cursor", "gmail", "--set", "2026-10-04T08:30:00Z"])
    result = runner.invoke(main, ["cursor", "gmail", "--as", fmt])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == expected


def test_an_empty_cursor_prints_nothing_in_any_form(root):
    _, runner = root
    for fmt in ("iso", "epoch", "jql"):
        result = runner.invoke(main, ["cursor", "jira", "--as", fmt])
        assert result.exit_code == 0 and result.output == ""


def test_as_is_for_reading_only(root):
    _, runner = root
    result = runner.invoke(main, ["cursor", "jira", "--begin", "--as", "epoch"])
    assert result.exit_code != 0


# --- slack: a capped sweep progresses from the oldest end ---------------------

def _cfg(**over):
    return {**slack.DEFAULTS, "channels": ["C0AAA111"], **over}


def _threads(n, newest_first=True):
    msgs = [{"ts": f"{1_791_000_000 + i * 60}.000100", "text": "x", "reply_count": 2}
            for i in range(n)]
    return msgs[::-1] if newest_first else msgs


def test_the_cap_keeps_the_oldest_threads_whatever_the_input_order():
    report = slack.select(_threads(5), _cfg(max_threads_per_run=2))
    assert [s.thread_ts for s in report.selected] == ["1791000000.000100", "1791000060.000100"]
    assert report.truncated


def test_a_truncated_sweep_says_where_to_resume():
    # The watermark moves to the last selected thread's exact ts; Slack's
    # oldest= is exclusive, so the next run starts strictly after it. (Flooring
    # to the second would re-select it forever under a cap of 1.)
    report = slack.select(_threads(5), _cfg(max_threads_per_run=2))
    assert report.resume_from == "2026-10-03T04:01:00.000100Z"
    assert slack.select(_threads(5), _cfg()).resume_from is None


@pytest.mark.parametrize("cap", [1, 3])
def test_repeated_capped_runs_reach_every_thread(cap):
    # The bug: newest-first input + "don't commit when truncated" re-selected
    # the same newest threads forever and never reached the oldest.
    everything = _threads(7)
    seen, watermark = set(), None
    for _ in range(10):
        window = [m for m in everything  # oldest= is exclusive, as Slack's is
                  if watermark is None or float(m["ts"]) > float(watermark)]
        report = slack.select(window, _cfg(max_threads_per_run=cap))
        seen.update(s.thread_ts for s in report.selected)
        if not report.truncated:
            break
        watermark = syncstate.as_form(report.resume_from, "epoch")
    else:
        pytest.fail("the sweep never finished")
    assert seen == {m["ts"] for m in everything}


def test_slack_select_cli_reports_resume_from(root):
    path, runner = root
    (path / "connectors.yml").write_text(
        "slack:\n  channels: [C0AAA111]\n  max_threads_per_run: 1\n")
    result = runner.invoke(main, ["slack", "select", "--channel", "C0AAA111",
                                  "--channel-type", "public_channel"],
                           input=json.dumps(_threads(3)))
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["truncated"] is True and out["resume_from"] == "2026-10-03T04:00:00.000100Z"


def test_a_slack_resume_point_keeps_its_microseconds_as_epoch(root):
    _, runner = root
    runner.invoke(main, ["cursor", "slack/C0AAA111", "--set", "2026-10-03T04:01:00.000100Z"])
    result = runner.invoke(main, ["cursor", "slack/C0AAA111", "--as", "epoch"])
    assert result.output.strip() == "1791000060.000100"


# --- review of #15 ---------------------------------------------------------------

def test_set_drops_a_stale_pending_stamp(root):
    # A manual --set supersedes any in-flight sweep: a later --commit must not
    # promote an old sweep-start over it.
    _, runner = root
    runner.invoke(main, ["cursor", "gmail", "--begin"])
    runner.invoke(main, ["cursor", "gmail", "--set", "2026-09-01T00:00:00Z"])
    assert runner.invoke(main, ["cursor", "gmail", "--commit"]).exit_code != 0
    assert runner.invoke(main, ["cursor", "gmail"]).output.strip() == "2026-09-01T00:00:00Z"


def test_clear_empties_a_cursor(root):
    _, runner = root
    runner.invoke(main, ["cursor", "slack/C0AAA111/resume", "--set", "2026-09-01T00:00:00Z"])
    assert runner.invoke(main, ["cursor", "slack/C0AAA111/resume", "--clear"]).exit_code == 0
    assert runner.invoke(main, ["cursor", "slack/C0AAA111/resume"]).output == ""


def test_a_timestamp_without_a_zone_is_utc_not_local(root, monkeypatch):
    # Read as local time, New York would land 4h late: a gap, not an overlap.
    import time
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    _, runner = root
    runner.invoke(main, ["cursor", "gmail", "--set", "2026-10-04T08:30:00"])
    out = runner.invoke(main, ["cursor", "gmail", "--as", "epoch"]).output.strip()
    monkeypatch.undo()
    time.tzset()
    assert out == "1791102600"


def test_a_non_iso_cursor_reads_back_and_converts_cleanly(root):
    _, runner = root
    runner.invoke(main, ["cursor", "legacy", "--set", "yesterday"])
    assert runner.invoke(main, ["cursor", "legacy"]).output.strip() == "yesterday"
    result = runner.invoke(main, ["cursor", "legacy", "--as", "epoch"])
    assert result.exit_code == 1 and "not an ISO timestamp" in result.output


@pytest.mark.parametrize("fmt, expected", [("iso", "2026-10-03T08:30:00Z"),
                                           ("epoch", "1791016200")])
def test_lookback_reads_the_window_early(root, fmt, expected):
    # Granola lists meetings by start time: a meeting in progress at sync time
    # must be listed again next run, so its window starts a day early.
    _, runner = root
    runner.invoke(main, ["cursor", "granola", "--set", "2026-10-04T08:30:00Z"])
    result = runner.invoke(main, ["cursor", "granola", "--lookback", "24", "--as", fmt])
    assert result.output.strip() == expected


def test_select_survives_an_odd_ts():
    report = slack.select([{"ts": "abc", "text": "x", "reply_count": 1},
                           {"ts": "1791000000.000100", "text": "y", "reply_count": 1}], _cfg())
    assert [s.thread_ts for s in report.selected][-1] == "1791000000.000100"
