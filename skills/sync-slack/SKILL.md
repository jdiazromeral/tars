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
  include_group_dms: false     # mpim sweep — off by default, opt in per vault
  window_days: 30              # first-run backfill per channel (no watermark yet)
  # Standalone (unthreaded) messages need ONE of the B3 signals; these tune it.
  min_standalone_chars: 280    # weakest fallback signal — length alone
  min_reactions: 2             # distinct reactions that count as endorsed;
                               # raise for chatty channels (2 is social reflex)
  redundant_domains:           # a link here is an ANTI-signal: the artifact is
    - github.com               # already ingested whole by another connector,
    - atlassian.net            # so the Slack stub only competes with it

```

If the block is missing or `channels` is empty, stop and tell the user to add
an allowlist (offer to resolve channel IDs from names via
`slack_search_channels`). Group DMs (`mpim`) are swept only when
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

## B3. Discover threads in the window

Per channel, page `slack_read_channel` with `oldest=<watermark>` until
exhausted, and select by **structural rules only** (mechanical, config-tunable
— not semantic judgment):

- **Threads** — any message with `reply_count ≥ 1`: capture whole (parent +
  all replies via `slack_read_thread`), even if the parent predates the
  watermark (a reply inside the window makes the thread current — the upsert
  refreshes it).
- **Standalone messages** — no replies: capture as a thread-of-one when **any
  one** of these signals is present. Each is a mark a *human* left on the
  message, read straight off the Slack payload — never an opinion about the
  topic:
  - **an attachment** — a file or image (someone shipped an artifact). The
    strongest signal in practice: it is the only one that catches a two-word
    message carrying a strategy deck or an eval screenshot;
  - **≥ `min_reactions` distinct reactions** — the channel endorsed it. Raise
    this per vault for chatty channels, where two 👍 is social reflex rather
    than endorsement;
  - **pinned** — an explicit importance marker;
  - **a link to a surface no other connector covers.** A link is only a
    signal when it points at something the corpus cannot already get in
    better shape. A link to a `redundant_domains` entry is an **anti-signal**:
    it says the real artifact lives in a connector that ingests it whole (a PR
    with its full review discussion, an issue with its comments), so a
    "review this please" stub adds no knowledge and competes with the good
    document in FTS. Links to docs, specs, dashboards and wiki pages still
    count;
  - **`len(text)` ≥ `min_standalone_chars`** — the weakest signal, and a
    fallback only. Length is a poor proxy for importance (a courtesy sentence
    can carry a strategy deck; a long message can be a rant), so it is last,
    not first.

  There is deliberately **no broadcast-mention signal**. It was proposed and
  cut on evidence: dry-run over a real squad channel, every single
  `@here`/`@channel` hit was ritual logistics ("@here daily?"), 0 for 4.

  No signal at all: skip. Greetings and one-liners die here, deterministically.
- **Always drop**: bot/app messages, join/leave/topic/rename events
  (subtype-tagged — mechanical to detect).

The signals are deliberately **permissive**, because the costs are asymmetric:
in a capture-raw corpus a false positive costs bytes that FTS will simply never
match, while a false negative is content permanently absent from the brain. So
when a signal is ambiguous, it admits.

These rules are plumbing, the same species as gmail's category filter — a
message qualifies on what is *attached to* it, never on what it is *about*. If
a channel needs different thresholds, that belongs in `connectors.yml` or the
vault's `AGENTS.md` house rules — never in ad-hoc per-run judgment. And the
residual gap is deliberate: a short, unadorned, genuinely important message
("the vendor confirmed it ships Monday") has no structural signal at all and
will be missed. Mode A is the answer to that — the sweep is a safety net, not a
replacement for deliberate capture.

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
