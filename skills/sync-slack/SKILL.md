---
name: sync-slack
description: Capture Slack threads into TARS. Trigger on "save this slack thread", "capture this conversation", a pasted slack.com/archives link meant for keeping, or "refresh the slack thread about X". Thread-level and deliberate by design — TARS does not sweep channels; you capture the discussions that matter, verbatim, via the Slack MCP, stored through the tars CLI.
---

# Capture Slack → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Slack is a firehose; the corpus is not. The unit of capture is the **thread**
(a lone message is a thread of one), chosen by the user in the moment —
"save this thread" — never a channel sweep. Skill-mediated like Granola/Jira:
the Slack MCP is the transport, the CLI owns storage and idempotence.

## 1. Resolve the thread

From a permalink `https://<ws>.slack.com/archives/<CHANNEL_ID>/p<digits>`:
the ts is the digits with a dot before the last 6 (`p1751970123456789` →
`1751970123.456789`). If that message is itself a reply, the MCP response
carries the parent `thread_ts` — capture the WHOLE thread from the parent.
Without a link ("that conversation with X about Y"), find it via
`slack_search_public_and_private` (`is:thread` helps), confirm with the user
if ambiguous. Load MCP tools via ToolSearch if deferred.

## 2. Fetch and assemble

`slack_read_thread` (paginate until exhausted), then one markdown document —
complete, verbatim, chronological:

```markdown
# <#channel>: <short thread subject>

- channel: #<name> (<CHANNEL_ID>)
- thread_ts: <ts>
- permalink: <url>
- participants: <names, comma-separated>

## Thread

### <author> — <ISO timestamp>

<message text verbatim>
```

- Subject: a short phrase from the parent message — judgment, but naming,
  not summarizing. Title: `slack <#channel> <YYYY-MM-DD> <subject>`.
- **People**: wiki-link authors with a `wiki/people/` page (`[[<slug>|<Name>]]`);
  backfill blank `slack:` identity fields with handles you see. Create a NEW
  page only for a 1:1 counterpart, a commitment owner, or someone recurring
  across ~3+ captures — never bulk-create.
- Keep emoji reactions only when they carry decision weight (a ✅ vote tally);
  drop join/leave noise and bot messages.

## 3. Store

```sh
tars add - --connector slack --origin "slack:<CHANNEL_ID>/<thread_ts>" \
  --title "<title>" --tag slack \
  --concept <slug> [--concept <slug>...]
```

The origin is channel+parent-ts: stable, source-native. Threads are
**mutable** (replies keep arriving) — re-capturing the same link refreshes
the snapshot; unchanged is a no-op, grown is an upsert. Shelve 1–4 concepts
(`ls wiki/concepts/` first, reuse aggressively, general speakable slugs),
then `tars finalize` (regenerate hubs, clear any index drift, re-check
invariants); skeleton-hub polish is the gardener's job.

## 4. Report

The one-line add result, concepts attached (flag new ones), people
linked/backfilled, and the message count captured — so a partial fetch is
visible, never silent.

## Notes

- **No channel watch, deliberately.** A `connectors.yml`-scoped incremental
  sweep (like github's) is designed but deferred: the trigger is catching
  yourself capturing threads from the same channel weekly. Until then,
  deliberate capture keeps signal high and the corpus yours.
- DMs and private channels: the MCP reads what the user can read; capture
  only what they explicitly asked to keep — a DM is someone's half-private
  conversation, so never sweep, only capture on direct request.
