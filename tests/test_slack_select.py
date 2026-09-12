"""Slack sweep selection: the rules as code, so they are actually deterministic.

Every signal, the anti-signal's exact scope, and each refusal path — the
behaviour the sweep skill relies on but cannot itself guarantee.
"""

from __future__ import annotations

import pytest

from tars.connectors import slack


def cfg(**over):
    base = {**slack.DEFAULTS, "channels": ["C0AAA111"]}
    base.update(over)
    return base


def msg(ts="1.0", text="", **extra):
    return {"ts": ts, "text": text, **extra}


# --- config ------------------------------------------------------------------

def test_load_config_requires_a_non_empty_allowlist(tmp_path):
    (tmp_path / "connectors.yml").write_text("slack:\n  channels: []\n")
    with pytest.raises(RuntimeError, match="will not sweep your whole"):
        slack.load_config(tmp_path)


def test_load_config_missing_block_is_a_refusal_not_an_empty_sweep(tmp_path):
    (tmp_path / "connectors.yml").write_text("github:\n  orgs: [acme]\n")
    with pytest.raises(RuntimeError, match="needs a scope"):
        slack.load_config(tmp_path)


def test_load_config_applies_defaults(tmp_path):
    (tmp_path / "connectors.yml").write_text("slack:\n  channels: [C0AAA111]\n")
    loaded = slack.load_config(tmp_path)
    assert loaded["min_standalone_chars"] == 280
    assert loaded["min_reactions"] == 2
    assert loaded["include_group_dms"] is False
    assert loaded["channels"] == ["C0AAA111"]


def test_load_config_keeps_explicit_overrides(tmp_path):
    (tmp_path / "connectors.yml").write_text(
        "slack:\n  channels: [C0AAA111]\n  min_reactions: 5\n")
    assert slack.load_config(tmp_path)["min_reactions"] == 5


# --- who may be swept --------------------------------------------------------

def test_one_to_one_dms_are_never_swept():
    with pytest.raises(RuntimeError, match="never swept"):
        slack.validate_channel("C0AAA111", "im", cfg())


def test_group_dm_needs_the_flag_as_well_as_the_id():
    with pytest.raises(RuntimeError, match="include_group_dms"):
        slack.validate_channel("C0AAA111", "mpim", cfg())
    slack.validate_channel("C0AAA111", "mpim", cfg(include_group_dms=True))


def test_channel_outside_the_allowlist_is_refused():
    with pytest.raises(RuntimeError, match="not in the slack.channels allowlist"):
        slack.validate_channel("C0ZZZ999", "public_channel", cfg())


# --- signals -----------------------------------------------------------------

def test_thread_wins_regardless_of_length():
    """The substance of a thread is in its replies, so a terse parent still counts."""
    report = slack.select([msg(text="Propuesta", reply_count=15)], cfg())
    assert [(s.thread_ts, s.signal) for s in report.selected] == [("1.0", "replies")]


def test_attachment_admits_a_two_line_message():
    report = slack.select([msg(text="slides below", files=[{"id": "F1"}])], cfg())
    assert report.selected[0].signal == "attachment"


def test_reactions_count_reactors_not_distinct_emoji():
    """Two 👍 is 2 — the reading the threshold is calibrated against."""
    two_thumbs = msg(text="ok", reactions=[{"name": "+1", "count": 2}])
    assert slack.select([two_thumbs], cfg()).selected[0].signal == "reactions"
    one_thumb = msg(text="ok", reactions=[{"name": "+1", "count": 1}])
    assert slack.select([one_thumb], cfg()).selected == []


def test_pinned_is_a_signal():
    assert slack.select([msg(text="hi", pinned_to=["C0AAA111"])], cfg()).selected[0].signal == "pinned"


def test_length_is_the_last_resort():
    report = slack.select([msg(text="x" * 280)], cfg())
    assert report.selected[0].signal == "length"
    assert slack.select([msg(text="x" * 279)], cfg()).selected == []


def test_greetings_die_deterministically():
    report = slack.select([msg(text="Buenos días!"), msg(ts="2.0", text="jajaja")], cfg())
    assert report.selected == []
    assert report.skipped["no-signal"] == 2


def test_bots_and_subtypes_are_dropped():
    noise = [
        msg(text="deployed", bot_id="B1"),
        msg(ts="2.0", text="renamed the channel", subtype="channel_name"),
        msg(ts="3.0", text="joined", subtype="channel_join"),
    ]
    report = slack.select(noise, cfg())
    assert report.selected == []
    assert report.skipped["bot-or-subtype"] == 3


# --- the anti-signal ---------------------------------------------------------

REDUNDANT = ["github.com/acme/*/pull/*", "acme.atlassian.net/browse/*"]


def test_link_to_an_already_ingested_artifact_is_not_a_signal():
    pr = msg(text="approve please <https://github.com/acme/api/pull/12|api#12>")
    report = slack.select([pr], cfg(redundant_link_patterns=REDUNDANT))
    assert report.selected == []
    assert report.skipped["redundant-link-only"] == 1


def test_link_outside_the_ingested_shape_still_counts():
    """A gist / discussion / permalink is in no connector, so it stays a signal."""
    for url in ("https://github.com/acme/api/discussions/9",
                "https://gist.github.com/acme/abc123",
                "https://github.com/other-org/x/pull/1"):
        report = slack.select([msg(text=f"see <{url}>")], cfg(redundant_link_patterns=REDUNDANT))
        assert report.selected, url
        assert report.selected[0].signal == "link"


def test_host_matching_is_a_suffix_not_a_substring():
    """`evil-github.com` must never satisfy a `github.com` pattern."""
    report = slack.select([msg(text="see <https://evil-github.com/acme/api/pull/12>")],
                          cfg(redundant_link_patterns=REDUNDANT))
    assert report.selected[0].signal == "link"


def test_subdomain_matches_the_pattern_host():
    assert slack._is_redundant_link("https://acme.atlassian.net/browse/AB-1", REDUNDANT)


def test_anti_signal_nullifies_the_link_only_never_another_signal():
    """An attachment plus a redundant link still qualifies — on the attachment."""
    both = msg(text="fix in <https://github.com/acme/api/pull/12>", files=[{"id": "F1"}])
    report = slack.select([both], cfg(redundant_link_patterns=REDUNDANT))
    assert report.selected[0].signal == "attachment"


def test_bare_link_without_slack_wrapping_is_ignored_by_the_link_signal():
    """Slack always wraps real links as <url|label>; unwrapped text is just text."""
    assert slack.select([msg(text="http://example.com")], cfg()).selected == []


# --- run cap -----------------------------------------------------------------

def test_run_cap_truncates_and_flags_so_the_watermark_is_withheld():
    many = [msg(ts=f"{i}.0", text="x" * 300) for i in range(5)]
    report = slack.select(many, cfg(max_threads_per_run=2))
    assert len(report.selected) == 2
    assert report.truncated is True
    assert report.skipped["over-run-cap"] == 3


def test_no_cap_configured_selects_everything():
    many = [msg(ts=f"{i}.0", text="x" * 300) for i in range(5)]
    report = slack.select(many, cfg(max_threads_per_run=0))
    assert len(report.selected) == 5
    assert report.truncated is False


# --- refresh -----------------------------------------------------------------

def test_due_for_refresh_returns_threads_older_than_the_watermark():
    ingested = [
        {"origin": "slack:C0AAA111/100.5", "captured_at": "2026-07-01T00:00:00Z"},
        {"origin": "slack:C0AAA111/200.5", "captured_at": "2026-07-20T00:00:00Z"},
    ]
    assert slack.due_for_refresh(ingested, "2026-07-10T00:00:00Z") == ["100.5"]


def test_due_for_refresh_without_a_watermark_returns_all():
    ingested = [{"origin": "slack:C0AAA111/100.5", "captured_at": "2026-07-01T00:00:00Z"}]
    assert slack.due_for_refresh(ingested, None) == ["100.5"]
