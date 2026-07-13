---
name: sync-gmail
description: Sync Gmail threads into TARS. Trigger on "sync gmail", "pull my inbox", "ingest that email thread about X", "mark this thread for deletion". Default scope is inbox threads (excluding promotions/social) since the watermark; also fetches a concrete thread list or ad-hoc search query on demand, and can flag a thread `to_be_deleted` without ever deleting anything. Fetches via the Gmail MCP (Anthropic-hosted Claude.ai connector, OAuth already brokered) and stores via the tars CLI so provenance, dedup, and the cursor stay deterministic.
---

# Sync Gmail → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

The agent is only the transport here. All storage decisions belong to the CLI:
never summarize, filter, or reformat thread content beyond the template below,
and never write into `raw/` directly. Load the Gmail MCP tools via ToolSearch
if deferred (`mcp__claude_ai_Gmail__*`).

## Modes

Pick the mode from what the user asked; they differ only in the search scope
and in **whether the watermark moves**.

- **Incremental (default)** — "sync gmail" / "pull my inbox". Scope is
  `in:inbox -category:promotions -category:social` via `search_threads`, sent
  or received since the watermark. This mode **brackets the sweep** with
  `tars cursor gmail --begin` … `--commit` (two-phase advance) and never
  reaches Trash or Spam — those are separate mailbox locations from
  `in:inbox`, not inbox categories, so excluding promotions/social does not
  need to touch them.
- **Concrete threads** — "ingest that thread with X about Y", "sync threads
  <id1> <id2>", or a named ad-hoc `search_threads` query the user gives you
  (e.g. "pull everything from legal@vendor.com", even if that reaches Trash
  or Spam because the user explicitly asked). Fetch exactly those threads.
  Calls **neither** `--begin` nor `--commit`.
- **Mark for deletion** — "mark this thread for deletion", "flag the vendor
  email as to-delete". Resolve the thread (already-ingested doc, or fetch it
  fresh if not yet in TARS), then apply the `to_be_deleted` label via
  `label_thread`. This is a **label-only** action — it never calls a delete
  tool and never removes the raw doc. Calls **neither** `--begin` nor
  `--commit` (it isn't a sweep).

The rule is enforced by the CLI, not by your memory: only `--begin`/`--commit`
move the watermark, and ad-hoc pulls or label actions never call them. A
thread pulled by a concrete search may be older than the watermark or outside
the default scope, so letting it advance the cursor would silently narrow
future incremental sweeps — the two-phase advance makes that impossible.

## Steps

1. **Read the watermark** (incremental only): `tars cursor gmail` — an ISO
   timestamp, empty on first run (ask the user how far back to go, default 30
   days). **Before fetching**, stamp the sweep start: `tars cursor gmail
   --begin` (the CLI records `now()` in a pending slot).

2. **Discover threads** with `search_threads` — discovery-only, it does not
   return full message bodies per its own tool description, so treat its
   output as an id list to fan out over, not as content. Paginate until
   exhausted — do not silently cap.
   - Incremental: query `in:inbox -category:promotions -category:social`
     plus `after:<watermark>`.
   - Concrete threads: the named thread ids, or the user's given
     `search_threads` query verbatim (may include Trash/Spam/any label since
     the user named it explicitly).

3. **Fetch full content** with `get_thread`, `messageFormat: FULL_CONTENT` —
   the only call that returns complete message bodies, and what gets
   assembled into the doc below. `get_message` (single-message-by-id) is out
   of scope for this connector's core flow; don't use it here. **Use each
   message's `plaintextBody` field, never `htmlBody`** — `htmlBody` carries
   the full mail-merge markup (inline CSS, tracking pixels, MSO conditionals)
   and routinely runs 90-100KB+ on a real newsletter or marketing thread,
   which exceeds tool output limits; `plaintextBody` is the same content at a
   fraction of the size and is what belongs in the template below.

4. **Process one thread fully before starting the next** — fetch, assemble,
   and `tars add` each thread as a single self-contained step. Do not collect
   thread ids, titles, and bodies into separate parallel lists to zip
   together by index afterward: a shell's array indexing convention (bash is
   0-indexed, zsh is 1-indexed) can silently shift one list relative to
   another, pairing a real title/origin with the wrong thread's body while
   every individual value still looks valid. Assemble one markdown document
   per thread — complete, raw, no editorializing, all messages in
   chronological order:

   ```markdown
   # <subject>

   - thread_id: <threadId>
   - participants: <names/emails, comma-separated>
   - labels: <comma-separated Gmail labels>
   - date: <first message ISO> – <last message ISO>

   ## Thread

   ### <sender> — <ISO timestamp>

   <message body, verbatim>
   ```

   Then pipe it in, attaching 1–4 concepts you can already judge from the
   subject/body (idempotent — re-syncing an unchanged thread is a no-op):

   ```sh
   tars add - --connector gmail --origin "gmail:<threadId>" \
     --title "<subject>" --tag gmail \
     --concept <slug> [--concept <slug>...]
   ```

   The origin is the thread id (stable, source-native) — never a direct
   `raw/` write.

   **Concepts**: `ls wiki/concepts/` first, reuse aggressively; mint new ones
   only when nothing fits (general, speakable slugs). Concepts merge on
   re-ingest — a re-sync adds shelving, never removes it. After the sweep run
   `tars finalize` once (regenerate hubs, clear index drift, re-check
   invariants); skeleton-hub polish is the gardener's job.

   **People**: in the participants line, wiki-link anyone with a
   `wiki/people/` page (`[[<slug>|<Name>]]`); backfill the existing `emails`
   field on their page with the address you see — never invent a new
   identity key. Create a NEW page only for a 1:1 counterpart, a commitment
   owner, or someone recurring across ~3+ captures (per AGENTS.md's
   recurrence threshold) — never bulk-create from a CC list.

5. **Label `synced`** — only after a thread's ingest succeeds (a
   failed/partial ingest must not get the label). Resolve the label id via
   `list_labels`, `create_label` if it doesn't exist yet, then apply it with
   `label_thread`. This is separate from and always after the `tars add`
   call above. **`create_label`/`label_thread` can fail with an
   insufficient-scope error** if the Gmail MCP connection wasn't authorized
   with label write access — this is a connection-level limitation, not a
   sync failure. If it happens, don't retry, don't block the sweep, and
   don't fall back to a different mutating tool: skip labeling for this run,
   continue the ingest, and name it plainly in the report (step 7) so the
   user knows to re-authorize the connector if they want `synced`/
   `to_be_deleted` to work.

6. **Commit the watermark — incremental mode only** — once every thread has
   been ingested cleanly: `tars cursor gmail --commit` (promotes the pending
   sweep-start stamp to the live watermark). Do this only if the sweep
   finished without errors — if any thread failed, leave it uncommitted so
   the next run re-scans from the old watermark. Skip this step entirely for
   concrete-thread pulls and mark-for-deletion actions (they never called
   `--begin`).

7. **Report**: counts of added / updated / unchanged, the mode used, the new
   watermark (or "unchanged — ad-hoc pull"), concepts created vs reused,
   people pages touched, any threads skipped because the MCP errored, and
   any labels applied (`synced`, `to_be_deleted`) or skipped due to
   insufficient label-write scope — name them explicitly so nothing is
   silently dropped.

## Notes

- This connector is skill-mediated (like `sync-jira`/`sync-slack`): there is
  no `tars sync gmail` — the MCP is the only channel and the CLI owns
  storage via `tars add` + `tars cursor`.
- The full Gmail MCP tool set is: `search_threads`, `get_thread`,
  `get_message`, `list_labels`, `create_label`, `label_thread`,
  `label_message`, `unlabel_thread`, `unlabel_message`,
  `apply_sensitive_thread_label`, `apply_sensitive_message_label`,
  `create_draft`, `list_drafts`. This skill only ever calls `search_threads`,
  `get_thread`, `list_labels`, `create_label`, and `label_thread` (for
  `synced` and `to_be_deleted`) — every other tool is out of scope for this
  flow.
- **Never call `create_draft`** — this connector only reads and labels, it
  never composes or sends anything.
- **Never call `apply_sensitive_thread_label` or `apply_sensitive_message_label`** — those move a thread to Trash/Spam (a real delete/spam action); this skill's `to_be_deleted` is a plain user label applied via `label_thread`, and never triggers an actual delete.
- `to_be_deleted` is advisory only: it marks intent for a human to act on
  later (e.g. from the Gmail UI) — this skill never removes the raw doc, and
  the label action is fully separate from the ingest logic above.
