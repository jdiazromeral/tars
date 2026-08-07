---
name: sync-slack
description: Capture Slack into TARS. Mode A (default) on "save this slack thread", "capture this conversation", or a pasted slack.com/archives link — deliberate, thread-level. Mode B on "sweep slack channels", "sync slack channels", "catch up my slack" — an incremental sweep of the connectors.yml channel allowlist with per-channel watermarks and deterministic structural rules (no model decides what to keep). 1:1 DMs are never swept in either mode.
---

# Capture Slack → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Two modes, one storage path:

- **Mode A — Thread capture (default).** The user names a thread in the moment
  — "save this thread" — and it is captured verbatim. Deliberate, human-chosen.
- **Mode B — Channel sweep.** An incremental sweep of a **connectors.yml
  allowlist** of channels. The allowlist is the judgment — a human chose those
  channels in config — and everything inside it is captured under deterministic
  structural rules. **No model decides per-item what to keep**: like the gmail
  connector's `-category:promotions` filter, scope is mechanical plumbing, and
  the intelligence stays at query time.

Skill-mediated like Granola/Jira/Gmail: the Slack MCP is the only transport
(there is no `tars sync slack` — no local token), the CLI owns storage and
idempotence via `tars add` + `tars cursor`. Load MCP tools via ToolSearch if
deferred.

Both modes produce the **same document** with the **same origin**
(`slack:<CHANNEL_ID>/<thread_ts>`), so a thread captured by hand and later
swept — or vice versa — upserts instead of duplicating.

---

# Mode A — Thread capture (default)

## A1. Resolve the thread

From a permalink `https://<ws>.slack.com/archives/<CHANNEL_ID>/p<digits>`:
the ts is the digits with a dot before the last 6 (`p1751970123456789` →
`1751970123.456789`). If that message is itself a reply, the MCP response
carries the parent `thread_ts` — capture the WHOLE thread from the parent.
Without a link ("that conversation with X about Y"), find it via
`slack_search_public_and_private` (`is:thread` helps), confirm with the user
if ambiguous.

## A2. Fetch and assemble

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

Attachments: <filename> (<mimetype>, <size>) — file_id <FILE_ID>
```

- Subject: a short phrase from the parent message — judgment, but naming,
  not summarizing. Title: `slack <#channel> <YYYY-MM-DD> <subject>`.
- **Attachments: record the metadata, always.** The `Attachments:` line goes on
  any message that has one (omit it entirely when there are none). Their
  *content* is **not** ingested yet — see the ROADMAP item — so this line is
  the whole record that an artifact existed, and without it a two-line message
  carrying a strategy deck captures as a bare courtesy sentence, which reads
  like full coverage of something the corpus never took. Naming the file makes
  it searchable and the omission auditable.
- **People**: wiki-link authors with a `wiki/people/` page (`[[<slug>|<Name>]]`);
  backfill blank `slack:` identity fields with handles you see. Create a NEW
  page only for a 1:1 counterpart, a commitment owner, or someone recurring
  across ~3+ captures — never bulk-create.
- Keep emoji reactions only when they carry decision weight (a ✅ vote tally);
  drop join/leave noise and bot messages.

## A3. Store

```sh
tars add - --connector slack --origin "slack:<CHANNEL_ID>/<thread_ts>" \
  --title "<title>" --tag slack \
  --concept <slug> [--concept <slug>...]
```

The origin is channel+parent-ts: stable, source-native. Threads are
**mutable** (replies keep arriving) — re-capturing the same link refreshes
the snapshot; unchanged is a no-op, grown is an upsert. Shelve 1–4 concepts
(`ls wiki/concepts/` first, reuse aggressively, general speakable slugs),
then `tars finalize`; skeleton-hub polish is the gardener's job.

## A4. Report

The one-line add result, concepts attached (flag new ones), people
linked/backfilled, and the message count captured — so a partial fetch is
visible, never silent.

---

# Mode B — Channel sweep (scoped, incremental)

Same fetch/assemble/store path as Mode A — only the *selection* of threads is
automated, and only inside a hard allowlist. A sweep with no allowlist is an
error, never a whole-workspace pull.

## B1. Scope — hard requirement

Read `connectors.yml` at the vault root (`$TARS_HOME/connectors.yml`). The
`slack` block is **required** for Mode B; refuse to sweep without it — never
infer "all my channels":

```yaml
slack:
  channels:                    # allowlist of channel IDs (required, non-empty)
    - C0ABCDE1234              #   e.g. #my-squad-channel
    - C0FGHIJ5678
  include_group_dms: false     # mpim sweep — deliberate privacy speed bump:
                               # a group DM needs BOTH this flag and its ID above
  window_days: 30              # first-run backfill per channel (no watermark yet)
  refresh_days: 14             # how far back B3's refresh pass re-reads threads
  max_threads_per_run: 40      # cap per run; 0 = uncapped
  min_standalone_chars: 280    # weakest fallback signal — length alone
  min_reactions: 2             # total reactors (sum of counts), not distinct
                               # emoji; raise for chatty channels
  redundant_link_patterns:     # a link matching one of these is an ANTI-signal:
    - github.com/acme/*/pull/* # that exact artifact is already ingested whole by
    - acme.atlassian.net/browse/*  # another connector, so the stub only competes
```

The schema, its defaults and this refusal are **code**, not prose:
`connectors/slack.py:load_config()` raises when `channels` is missing or empty,
exactly as github's does. Don't hand-parse the file — let the CLI fail. If it
does, tell the user to add an allowlist (offer to resolve channel IDs from
names via `slack_search_channels`). Group DMs (`mpim`) are swept only when
`include_group_dms: true` AND their IDs are in `channels`. **1:1 DMs (`im`)
are never swept** — those are Mode A, by direct request only: a DM is someone's
half-private conversation.

## B2. Per-channel two-phase watermark

Slack messages are append-only, so the cursor is simply the sweep-start
timestamp per channel, stored under a **namespaced key** — no CLI changes,
`sync_state` keys are free-form:

```sh
tars cursor "slack/<CHANNEL_ID>"            # read (empty on first run)
tars cursor "slack/<CHANNEL_ID>" --begin    # stamp pending BEFORE fetching
tars cursor "slack/<CHANNEL_ID>" --commit   # promote ONLY after clean ingest
```

Bracket **each channel independently**: a channel that errors leaves its own
cursor uncommitted (next sweep re-scans it) without holding back the others.
First run on a channel (empty cursor): backfill `window_days`. Mode A captures
never touch these cursors.

## B3. Discover threads — the CLI selects, you don't

Per channel, page `slack_read_channel` with `oldest=<watermark>` until
exhausted, then hand the raw history to the CLI and capture what it returns:

```sh
echo '<conversations.history JSON>' \
  | tars slack select --channel <CHANNEL_ID> --channel-type <public_channel|private_channel|mpim>
```

It answers `{selected: [{thread_ts, signal, reply_count, reaction_total}],
skipped: {reason: n}, truncated: bool}`.

**Do not re-derive the rules here.** They live in
`connectors/slack.py:select()` and are covered by `tests/test_slack_select.py`
— a deterministic rule executed by a model reading English is only as
deterministic as the model's mood, and per AGENTS.md plumbing is code. For
review, the rules it applies are: threads (`reply_count ≥ 1`) always; a
standalone message on any one human-left mark — attachment, `min_reactions`
reactors, pinned, a link *not* matching `redundant_link_patterns`, or length
last; bots and channel-bookkeeping subtypes dropped. Signals are permissive
because the costs are asymmetric (a false positive costs bytes FTS never
matches; a false negative is permanently absent), and the anti-signal nullifies
**only** the link signal — an attachment plus a link to an ingested PR still
qualifies, on the attachment.

If `truncated` is true the run hit `max_threads_per_run`: capture what came
back and **do not commit the watermark** (B2) — the next run resumes from the
same point instead of skipping what was cut.

### Refresh already-ingested threads (mutable sources go stale)

`conversations.history` returns top-level messages at their **original `ts`**;
replies live in `conversations.replies` and never reappear in history. So a
thread whose parent predates the watermark is *not in the window* no matter how
much activity it gains — without this pass the sweep would freeze a thread on
first capture and the vault would hold a misleading snapshot that looks
complete. Same "refresh by id" duty `sync-jira` carries:

1. `tars list --connector slack --json` → keep entries whose origin starts
   `slack:<CHANNEL_ID>/`.
2. Re-fetch each with `slack_read_thread` (the CLI's
   `slack.due_for_refresh()` is the helper: it drops anything already captured
   at/after the watermark) and re-store it — unchanged is a no-op, grown is an
   upsert.
3. Bound the cost with `refresh_days`: only threads captured that recently are
   revisited, since older ones rarely grow.

**Stated limit:** a reply to a thread older than `refresh_days` reaches the
vault only through Mode A ("refresh the slack thread about X"). Likewise a
short, unadorned but genuinely important message ("the vendor confirmed it
ships Monday") carries no structural signal and will be missed. The sweep is a
safety net, not a replacement for deliberate capture.

## B4. Capture

Each selected thread goes through **A2 + A3 verbatim** — same template, same
origin `slack:<CHANNEL_ID>/<thread_ts>`, same people discipline, same concept
shelving (judgment stays where it always was: after the fetch, on raw
content). Re-sweeps refresh grown threads and no-op on unchanged ones.

Process channels sequentially and threads within a channel one at a time
(fetch → assemble → add) — never batch-zip parallel lists.

A sweep is the corpus's first *bulk* capture path, so the flat filename
namespace matters here more than anywhere (per AGENTS.md: Obsidian resolves
`[[stem]]` by basename, so the collision check spans the whole vault). Keep
thread subjects distinctive rather than generic — the CLI disambiguates with an
id suffix and `tars doctor` flags any `ambiguous-stem`, but a readable stem
beats a suffixed one.

## B5. Finalize and report

After all channels: `tars finalize` once. Then report per channel: threads
added / updated / unchanged, standalones captured **with the signal that
admitted each** (attachment / reactions / pinned / link / length)
and how many were skipped for having none, the new watermark or "uncommitted —
errored", people linked/backfilled, concepts created vs reused. Naming the
admitting signal is what makes the thresholds tunable from evidence instead of
taste. Name every skipped or failed item class explicitly — silent truncation
reads as coverage.

---

## Notes

- **The allowlist is the relevance filter.** Mode B deliberately has no
  model-side keep/drop step: adding a channel to `connectors.yml` is the
  human judgment, everything within the structural rules is kept raw, and
  synthesis happens at query time. If a channel turns out too noisy, the fix
  is removing it from the allowlist (and `tars rm` for regrets), not a
  smarter gate.
- **Signal yield is a property of the channel, not of the rules** — measured,
  not assumed. Dry-running the same rules over two real channels: an
  announcement-style channel yielded 9 documents in 30 days, nearly all
  durable; a squad coordination channel yielded ~27, of which 2 were durable,
  ~14 were stubs pointing at PRs/issues another connector already had, and the
  rest was banter that happened to clear a bar. **Coordination channels are
  poor sweep targets** precisely because their durable content is a
  by-product that lives in GitHub and Jira — which are swept properly. Sweep
  where decisions are *announced*, not where work is *coordinated*; and when a
  channel disappoints, that is the allowlist doing its job, not a reason for a
  cleverer filter.
- **sync-all**: the sweep is NOT part of the default `sync-all` set — run it
  with "sweep slack channels" (or "sync all including slack" explicitly).
  Deliberate Mode A capture remains the default Slack posture.
- Private channels the user belongs to may be allowlisted like public ones —
  the MCP reads what the user can read. The `im` prohibition is absolute.
