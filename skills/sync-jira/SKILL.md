---
name: sync-jira
description: Sync Jira issues into TARS. Trigger on "sync jira", "pull my jira tickets", "ingest PROJ-123", "sync everything under <epic>". Default scope is issues assigned to me since the watermark; also fetches concrete issue keys or an epic's children on demand. Fetches via the Atlassian Rovo MCP and stores via the tars CLI so provenance, dedup, and the cursor stay deterministic.
---

# Sync Jira → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

The agent is only the transport here. All storage decisions belong to the CLI:
never summarize, filter, or reformat issue content beyond the template below,
and never write into `raw/` directly.

## Modes

Pick the mode from what the user asked; they differ only in the JQL and in
**whether the watermark moves**.

- **Incremental (default)** — "sync jira" / "pull my tickets". Scope is
  `assignee = currentUser()`, updated since the watermark. This mode **brackets
  the sweep** with `tars cursor jira --begin` … `--commit` (two-phase advance).
- **Concrete issues** — "ingest PROJ-123", "sync PROJ-123 and PROJ-124".
  Fetch exactly those keys. Calls **neither** `--begin` nor `--commit`.
- **Under an epic** — "sync everything under PROJ-100". Fetch the epic and its
  children. Calls **neither** `--begin` nor `--commit`.
- **Refresh** — "review the current status of PROJ-713", "refresh my Atlas
  tickets", "are these still up to date?". Jira issues are *mutable*: an ingested
  doc is a point-in-time snapshot that goes stale as comments, status, and state
  change. Re-fetch the named key(s) — or every ingested issue via
  `tars list --connector jira --json` — and run them back through the same
  fetch+ingest path. Idempotent: unchanged is a no-op, a changed ticket upserts
  (its `captured_at` frontmatter bumps to now, so the snapshot date is visible).
  Calls **neither** `--begin` nor `--commit`. If a refresh 404s (deleted or
  access lost), **flag it — never delete the raw doc** (status changes belong to
  the user).

The rule is enforced by the CLI, not by your memory: only `--begin`/`--commit`
move the watermark, and ad-hoc pulls never call them. A ticket under an epic may
be assigned to someone else and older than the watermark, so letting it advance
the cursor would silently skip your own future work — the two-phase advance
makes that impossible.

The incremental sweep only *discovers* changes for issues assigned to me; issues
pulled ad-hoc (a concrete key, an epic's children) are outside that scope and
will not be refreshed by it — use the Refresh mode to bring them current.

## Steps

1. **Resolve the site.** The MCP needs a `cloudId` — get it from
   `getAccessibleAtlassianResources` (load MCP tools via ToolSearch if
   deferred). If more than one site is accessible, ask which. Keep the site URL
   for building `browse/<KEY>` links.

2. **Build the JQL** for the chosen mode (always `ORDER BY updated ASC` so a
   mid-run failure leaves a safe resumable watermark):

   - Incremental: first read the current bound with `tars cursor jira` (empty on
     first run — then ask how far back to go, default 30 days). Then, **before
     fetching**, stamp the sweep start: `tars cursor jira --begin` (the CLI
     records `now()` in a pending slot). JQL:
     `assignee = currentUser() AND updated >= "<watermark>" ORDER BY updated ASC`.
   - Concrete issues: `key in (PROJ-123, PROJ-124) ORDER BY updated ASC`.
   - Under an epic: `parent = "<EPIC-KEY>" ORDER BY updated ASC`. If that returns
     nothing on a classic company-managed project, retry with
     `"Epic Link" = "<EPIC-KEY>"`. Ingest the epic issue itself too.

3. **Fetch** with `searchJiraIssuesUsingJql`, paginating until exhausted — do
   not silently cap. For each hit, call `getJiraIssue` for the full description,
   comments, and fields (a search row is not enough).

4. **For each issue**, assemble one markdown document — complete, raw, no
   editorializing. Render Jira's rich text (ADF) as the MCP returns it; keep
   comments in chronological order:

   ```markdown
   # <KEY> <summary>

   - key: <KEY>
   - type: <issue type>
   - status: <status>
   - assignee: <display name or Unassigned>
   - reporter: <display name>
   - priority: <priority>
   - parent: <PARENT-KEY> <parent summary>   (omit if none)
   - labels: <comma-separated>               (omit if none)
   - created: <ISO>
   - updated: <ISO>
   - url: <site>/browse/<KEY>

   ## Description

   <description, verbatim>

   ## Comments

   ### <author> — <ISO>

   <comment body, verbatim>
   ```

   Then pipe it in, attaching 1–4 concepts you can already judge from the
   summary/description (idempotent — re-syncing an unchanged issue is a no-op):

   ```sh
   tars add - --connector jira --origin "jira:<KEY>" \
     --title "<KEY> <summary>" --tag jira \
     --concept <slug> [--concept <slug>...]
   ```

   The origin is the issue key (stable, human-meaningful); the title carries the
   key so the raw filename and `[[wiki-links]]` read well.

   **Concepts**: `ls wiki/concepts/` first, reuse aggressively; mint new ones
   only when nothing fits (general, speakable slugs). Concepts merge on
   re-ingest — a re-sync adds shelving, never removes it. After the sweep run
   `tars finalize` once (regenerate hubs, clear index drift, re-check
   invariants); skeleton-hub polish is the gardener's job.

   **People**: in the assembled assignee/reporter lines, wiki-link anyone with
   a `wiki/people/` page (`[[<slug>|<Name>]]`); backfill a blank `jira:`
   identity field with the handle/accountId you see. Create a NEW page only
   for a 1:1 counterpart, a commitment owner, or someone recurring across ~3+
   captures — never bulk-create from assignees.

5. **Commit the watermark — incremental mode only** — once every issue has been
   ingested cleanly: `tars cursor jira --commit` (promotes the pending sweep-start
   stamp to the live watermark). Do this only if the sweep finished without
   errors — if any page or issue failed, leave it uncommitted so the next run
   re-scans from the old watermark. Skip this step entirely for concrete-issue
   and epic pulls (they never called `--begin`).

6. **Report**: counts of added / updated / unchanged, the mode used, the new
   watermark (or "unchanged — ad-hoc pull"), concepts created vs reused, people
   pages touched, and any issues skipped because the MCP errored — name them
   explicitly so nothing is silently dropped.

## Notes

- This connector is skill-mediated (like `sync-granola`): there is no
  `tars sync jira` — the MCP is the only channel and the CLI owns storage via
  `tars add` + `tars cursor`.
- Ingested Jira issues become linkable targets for `tasks/` records and digests:
  a task that references a ticket can wiki-link its raw stem
  (`[[proj-123-...|PROJ-123]]`). Task extraction itself stays the `tasks`
  skill's job — this skill only ingests and shelves.
