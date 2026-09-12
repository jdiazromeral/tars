"""Slack connector: the *selection* half of the channel sweep, as code.

Slack has no local token — the MCP the agent holds is the only transport — so
unlike `github` this connector cannot own its fetch. What it *can* own is
everything that needs no judgment: reading the scope out of `connectors.yml`
and deciding which messages in a window are worth capturing. Per AGENTS.md
("plumbing is code, judgment is the model") that belongs here rather than in
skill prose, because a deterministic rule executed by an LLM reading English
is only as deterministic as the model's mood.

The split, therefore:

    MCP (skill)        fetch channel history / thread replies
    slack.select()     decide which threads to capture      <-- this module
    MCP (skill)        fetch the selected threads in full
    skill              assemble, shelve under concepts, wire people
    tars add           store, dedup, index

Selection is **structural only**: a message qualifies on what is *attached to*
it, never on what it is *about*. Threads (anything with a reply) are always
in. A standalone message qualifies when it carries one of the marks a human
left on it — an attachment, enough reactions, a pin, a link to a surface no
other connector already covers, or, weakest and last, sheer length.

Signals are permissive on purpose: the costs are asymmetric. A false positive
costs bytes that FTS will never match; a false negative is content permanently
absent from the corpus. The one exception is `redundant_link_patterns`, which
*excludes*, so it is deliberately narrow — see `_is_redundant_link`.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

CONFIG_FILE = "connectors.yml"

#: Subtypes that are channel bookkeeping, never content.
DROP_SUBTYPES = frozenset({
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "channel_unarchive",
    "group_join", "group_leave", "group_topic", "group_purpose", "group_name",
    "bot_message", "thread_broadcast_bot",
})

DEFAULTS = {
    "include_group_dms": False,
    "window_days": 30,
    "refresh_days": 14,
    "max_threads_per_run": 40,
    "min_standalone_chars": 280,
    "min_reactions": 2,
    "redundant_link_patterns": [],
}

#: Slack wraps links as `<url|label>` or `<url>`.
_SLACK_LINK = re.compile(r"<(https?://[^|>\s]+)(?:\|[^>]*)?>")


def load_config(root: Path) -> dict:
    """Read and validate the `slack:` block, applying defaults.

    Mirrors `github.load_config`: the allowlist is a hard requirement, so a
    misconfigured vault fails loudly instead of quietly sweeping a workspace.
    """
    path = root / CONFIG_FILE
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = (data or {}).get("slack") or {}
    channels = cfg.get("channels") or []
    if not channels:
        raise RuntimeError(
            f"slack connector needs a scope: set slack.channels to a non-empty "
            f"list of channel IDs in {path} — it will not sweep your whole "
            f"workspace."
        )
    if not all(isinstance(c, str) and c.strip() for c in channels):
        raise RuntimeError(f"slack.channels in {path} must be a list of channel ID strings")
    merged = {**DEFAULTS, **cfg}
    merged["channels"] = [c.strip() for c in channels]
    return merged


def validate_channel(channel_id: str, channel_type: str, cfg: dict) -> None:
    """Raise unless this conversation may be swept.

    1:1 DMs are never swept in any configuration — a DM is someone's
    half-private conversation, and the other party never opted in. Group DMs
    need both the flag *and* an explicit ID in the allowlist; the flag is a
    deliberate privacy speed bump, not a substitute for naming the channel.
    """
    if channel_id not in cfg["channels"]:
        raise RuntimeError(f"{channel_id} is not in the slack.channels allowlist")
    if channel_type == "im":
        raise RuntimeError(
            f"{channel_id} is a 1:1 DM — never swept. Capture those deliberately "
            f"with Mode A instead."
        )
    if channel_type == "mpim" and not cfg["include_group_dms"]:
        raise RuntimeError(
            f"{channel_id} is a group DM and slack.include_group_dms is false"
        )


def _links(text: str) -> list[str]:
    return _SLACK_LINK.findall(text or "")


def _is_redundant_link(url: str, patterns: list[str]) -> bool:
    """True when `url` names an artifact another connector ingests *whole*.

    Deliberately narrow, because this is the one rule that *excludes* and the
    rest of the design leans the other way. Patterns match `host/path`, with
    the host compared as a **suffix** (so `acme.atlassian.net` matches
    `*.atlassian.net` but `evil-github.com` never matches `github.com`), and
    the path by glob. A pattern therefore points at a shape that is genuinely
    in the corpus (`github.com/acme/*/pull/*`) rather than a whole domain —
    a gist, a discussion, a code permalink or an out-of-scope repo is not
    ingested anywhere, so it stays a perfectly good link signal.
    """
    parts = urlsplit(url)
    host, path = parts.netloc.lower().split(":")[0], parts.path.rstrip("/")
    for pattern in patterns:
        pat_host, _, pat_path = pattern.strip().lower().partition("/")
        if not (host == pat_host or host.endswith("." + pat_host)):
            continue
        if not pat_path or fnmatch.fnmatch(path.lstrip("/"), pat_path):
            return True
    return False


@dataclass
class Selected:
    """One thread to capture, plus the signal that admitted it."""
    thread_ts: str
    signal: str
    reply_count: int = 0
    reaction_total: int = 0


@dataclass
class SelectionReport:
    selected: list[Selected] = field(default_factory=list)
    #: reason -> count, so a sweep can report what it dropped and why.
    skipped: dict[str, int] = field(default_factory=dict)
    #: True when `max_threads_per_run` cut the list short: the caller must NOT
    #: commit its watermark, so the next run resumes instead of skipping.
    truncated: bool = False

    def _skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _reaction_total(message: dict) -> int:
    """Total reactors, not distinct emoji.

    Pinned to `sum(count)` on purpose: "two 👍" is the case the threshold is
    calibrated against, and under a distinct-emoji reading that would be 1 and
    could never qualify. One reading, so two agents cannot disagree.
    """
    return sum(int(r.get("count", 0)) for r in message.get("reactions") or [])


def select(messages: list[dict], cfg: dict) -> SelectionReport:
    """Choose which messages in a fetched window become captured threads.

    Pure: no I/O, no MCP, no clock. `messages` is `conversations.history` as
    the MCP returns it (newest-first or oldest-first, order is irrelevant).
    """
    report = SelectionReport()
    cap = cfg.get("max_threads_per_run") or 0
    patterns = cfg.get("redundant_link_patterns") or []

    for message in messages:
        ts = message.get("ts")
        if not ts:
            report._skip("no-ts")
            continue
        if message.get("subtype") in DROP_SUBTYPES or message.get("bot_id"):
            report._skip("bot-or-subtype")
            continue

        reactions = _reaction_total(message)
        replies = int(message.get("reply_count") or 0)

        # A thread is Slack's own record of a conversation: always in, and the
        # length bar never applies — the substance of a thread lives in its
        # replies, so a two-word parent with 15 answers still matters.
        if replies >= 1:
            signal = "replies"
        elif message.get("files"):
            signal = "attachment"
        elif reactions >= cfg["min_reactions"]:
            signal = "reactions"
        elif message.get("pinned_to"):
            signal = "pinned"
        elif any(not _is_redundant_link(u, patterns) for u in _links(message.get("text", ""))):
            signal = "link"
        elif len(message.get("text") or "") >= cfg["min_standalone_chars"]:
            signal = "length"
        else:
            # Either no signal at all, or the only link pointed at an artifact
            # another connector already holds in full.
            report._skip("redundant-link-only" if _links(message.get("text", "")) else "no-signal")
            continue

        if cap and len(report.selected) >= cap:
            report.truncated = True
            report._skip("over-run-cap")
            continue

        report.selected.append(
            Selected(thread_ts=ts, signal=signal,
                     reply_count=replies, reaction_total=reactions)
        )

    return report


def due_for_refresh(ingested: list[dict], watermark: str | None) -> list[str]:
    """Thread ts values worth re-fetching for late replies.

    `conversations.history` returns top-level messages at their original `ts`,
    so a thread whose parent predates the watermark never reappears no matter
    how many replies it gains — the sweep alone would freeze it forever. The
    fix is the same "refresh by id" pass the jira connector uses: re-read the
    threads already ingested for this channel and let the upsert do the rest.

    `ingested` is `tars list --connector slack --json`, filtered to the
    channel; each entry needs `origin` (`slack:<channel>/<thread_ts>`) and
    `captured_at`. Entries captured at/after `watermark` are already current
    from this sweep, so only older ones are returned.
    """
    out = []
    for doc in ingested:
        origin = doc.get("origin") or ""
        if "/" not in origin:
            continue
        if watermark and (doc.get("captured_at") or "") >= watermark:
            continue
        out.append(origin.rsplit("/", 1)[1])
    return out
