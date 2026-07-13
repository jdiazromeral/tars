---
name: sync-granola
description: Sync Granola meetings into TARS. Trigger on "sync granola", "pull my meetings into tars", "ingest recent meetings". Fetches via the Granola MCP (local files are encrypted; MCP is the only channel), stores via the tars CLI so provenance, dedup, and the cursor stay deterministic.
---

# Sync Granola → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

The agent is only the transport here. All storage decisions belong to the CLI:
never summarize, filter, or reformat meeting content beyond the template below,
and never write into raw/ directly.

1. **Read the watermark**: `tars cursor granola` — an ISO timestamp, empty on
   first run. On first run, ask the user how far back to go (default: 30 days).
2. **List meetings** since the watermark with the Granola MCP tools (load them
   via ToolSearch if deferred: `list_meetings` / `get_meetings`). Paginate
   until exhausted — do not silently cap.
3. **For each meeting**, fetch notes and transcript (`get_meeting_transcript`),
   assemble one markdown document — complete, raw, no editorializing:

   ```markdown
   # <title>

   - date: <start ISO>
   - attendees: <names/emails, comma-separated>
   - granola_id: <meeting_id>

   ## Notes

   <Granola's notes/summary panel, verbatim>

   ## Transcript

   <full transcript, verbatim>
   ```

   Then pipe it in, attaching 1–4 concepts you can already judge from the
   notes/summary (idempotent — re-syncing an unchanged meeting is a no-op):

   ```sh
   tars add - --connector granola --origin "granola:<meeting_id>" \
     --title "<YYYY-MM-DD> <title>" --tag meeting \
     --concept <slug> [--concept <slug>...]
   ```

   **Concepts**: `ls wiki/concepts/` first, reuse aggressively; mint new ones
   only when nothing fits (general, speakable slugs). Concepts merge on
   re-ingest — a re-sync adds shelving, never removes it. After the sweep run
   `tars finalize` once (regenerate hubs, clear index drift, re-check
   invariants); skeleton-hub polish is the gardener's job.

   **People**: wiki-link attendees who already have a `wiki/people/` page
   (`[[<slug>|<Name>]]`); backfill blank identity fields (emails, handles) you
   can see. Create a NEW page only for a 1:1 counterpart, a commitment owner,
   or someone recurring across ~3+ captures — never bulk-create from a big
   attendee list.

4. **Vocab watch** — the corrections ledger is agent-authored, human-approved:
   while reading each transcript, watch for suspected speech-to-text
   mishearings — tokens that read like near-misses of names the vault already
   knows (people pages, concept slugs, `vocab.yml` canonicals, product
   terms and ticket prefixes). For each suspect, propose a `vocab.yml` entry:
   the canonical form, the misheard variant(s), and evidence (which meeting,
   how many times). **Wait for the user's approval**, then append the entry
   scoped to STT — new-format, `connectors: [granola]` — run `tars normalize`,
   and report what changed. Never correct raw text any other way, and never
   add an entry unapproved. Skip generic words that could appear legitimately
   (that's exactly what connector scoping protects, but don't lean on it).
5. **Advance the watermark** to the newest meeting's start time:
   `tars cursor granola --set "<ISO timestamp>"`.
6. **Report**: counts of added / updated / unchanged, the new watermark,
   concepts created vs reused, vocab entries proposed/added, and any meetings
   skipped because the transcript was empty or the MCP errored — name them
   explicitly so nothing is silently dropped.
