---
name: track
description: Track what you worked on — append an entry to today's activity record (ticket + optional duration + what you did), and review the day or the week. Trigger on "I worked on X", "track this", "log my time on PROJ-123", "I spent the morning on X", "what did I work on today", "weekly track", "where did my time go".
---

# Track activity into TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Two modes, same record:

- **Log** — "I worked on DESEO-1234", "spent the morning on the cache thing".
- **Review** — "what did I work on today", "weekly track".

## Why this exists (and what it is *not*)

Every other stream in TARS is *evidence left behind*: a ticket that moved, a PR,
a meeting Granola recorded. Work that leaves no trace — reading code, debugging
something that ended in "not reproducible", thinking a problem through — is
invisible to every connector, and it is most of a day. This skill is the
deliberate record of that, and nothing else. It does **not** replace the daily
pass (`end-of-day`) or the weekly digest; it supplies the band they were missing.

It is also not `tasks/`. A task is a commitment with an owned lifecycle
(`open → done`, one file forever, status changes belong to the user). Activity
is an **event**: immutable, append-many, no status. It lives in `raw/`, under
its own `activity` connector — no CLI code behind it, the generic
`tars add - --connector --origin` pipe carries the whole thing.

## The record

One document per day, appended to all day long:

| | |
|---|---|
| connector | `activity` |
| origin | `activity:<YYYY-MM-DD>` |
| title | `<YYYY-MM-DD> Activity` → `raw/activity/<YYYY-MM-DD>-activity.md` |
| concept | `activity-log` (exactly one — see below) |
| tag | `activity` |

**The date is in the identity, deliberately.** `tars list --since` filters on
`captured_at` — when a document *landed* — and every append rewrites the day
record, so its `captured_at` ends up being whenever you last touched it. The
filename is the only stable statement of which day the work happened. This
mirrors the `granola` convention (`<YYYY-MM-DD> <title>`), which is what makes a
day or a week readable as a filename glob across both streams:

```sh
ls raw/activity/2026-08-03-*.md raw/granola/2026-08-03-*.md    # a day
```

**One concept, on purpose.** Shelve under `activity-log` only — never under the
project concepts the entries mention. `tars hubs` regenerates each hub's
`## Sources` from its shelved documents, so shelving day records under project
concepts would bury every real source under a year of time rows. Relatedness
comes from the `[[wiki-link]]` to the ticket *inside the entry*: the ticket's raw
document accumulates a backlink from every day you touched it, which is exactly
the "what have I been putting time into" view — and full-text search still
reaches the entry bodies.

Entry format — a fixed three-slot line so the review modes read cleanly:

```markdown
# 2026-08-03 Activity

## Entries

- 1.5h · [[deseo-1234-cdn-cache-invalidation|DESEO-1234]] — traced the stale-purge path; root cause looks like the edge TTL
- — · [[deseo-1301-partner-onboarding|DESEO-1301]] — reviewed the API contract with the partner team
- 1h · unticketed — interviewing for the platform role
```

`—` in the duration slot means unknown or declined; `unticketed` in the ticket
slot means there is no issue behind it. The description is **the user's own
words**, lightly shaped — this is their record, not your summary of it.

## Log mode

1. **Fix the day.** Default to **today in local time** (`date +%Y-%m-%d`).
   Never derive it from a UTC timestamp: `captured_at` is UTC, so an evening
   entry would land in tomorrow's record. Honor an explicit day when the user
   gives one ("yesterday", "on Monday"). One case to confirm rather than guess:
   an entry logged after local midnight when *yesterday's* record exists and
   today's does not — that is usually still yesterday's work.

2. **Resolve the ticket.** The point of the ledger is that it points at managed
   work, so every entry tries to land on a real issue:

   - **A key was given** (`DESEO-1234`) — check it is ingested
     (`tars list --connector jira --json`). If it is not, pull it through the
     `sync-jira` skill's **concrete-issue** mode (which correctly leaves the
     watermark alone), then link its raw stem. **Nothing will catch this
     later**: `tars doctor` deliberately excludes `raw/` from its dangling-link
     check (captured third-party text can contain literal `[[...]]` that isn't a
     wiki-link), so an entry pointing at a document that was never ingested is a
     silently dead link. Verify here, or don't write a link — if the ticket
     can't be ingested (no access, MCP down), record the bare key as plain text
     and say so in the report.
   - **Prose, no key** ("the cache invalidation thing") — `tars search` the
     corpus and offer the matching ticket. Usually it exists and the user simply
     didn't say the number.
   - **Nothing matches** — ask (step 3).

3. **Ask for what's missing — once, and skippably.** When either slot is empty,
   ask in a **single** prompt, not two:

   - *No ticket resolved* → offer to create the Jira issue, proposing project,
     type and summary from the user's own words. Creating it is an outward write
     to a real system of record: use `createJiraIssue` via the Atlassian Rovo MCP
     (load the tool with ToolSearch if deferred), **only** on explicit
     confirmation, never silently. Once created, ingest it via `sync-jira`
     concrete mode so the link resolves.
   - *No duration given* → ask how long.

   Both are **declinable**, and a decline sticks for the rest of the session —
   "no ticket" records the entry as `unticketed`, "skip time" records `—`. This
   prompt exists to catch the entries worth catching, not to interrogate every
   line: real work is legitimately unticketed sometimes (interviews, an
   incident, a 1:1, admin), and the weekly review reports that count instead of
   the skill refusing the entry.

4. **Append to the day's record.** Find it — `ls raw/activity/<date>-*.md`,
   falling back to `tars list --connector activity --json` — and if it exists,
   read the file, **drop the frontmatter block and a leading `Concepts:` line**
   (that line is a derived rendering; re-feeding it would duplicate it), append
   the new entry under `## Entries`, and pipe the whole body back:

   ```sh
   tars add - --connector activity --origin "activity:<YYYY-MM-DD>" \
     --title "<YYYY-MM-DD> Activity" --tag activity --concept activity-log \
     < /path/to/scratch.md
   ```

   Write the body to a scratch file and redirect it — heredocs mangle special
   characters. Same origin every time, so the day is one document that upserts;
   re-adding an unchanged body is a no-op.

   Run `tars hubs` only when `tars add` reports **`added`** (a new day record
   means a new row on the `activity-log` hub, and the hub skeleton on the very
   first ever entry). On `updated` there is nothing for it to change.

5. **Report** one line: the day, the entry as stored, whether the ticket was
   resolved / created / left unticketed, and the add status.

## Review mode

Read-only, date-scoped, no watermark — re-run it as often as you like and it
answers the same. Read the **filename dates**, not `captured_at`.

- **Day** ("what did I work on today"): the day's activity record, plus that
  day's meetings (`raw/granola/<date>-*.md` — meetings are time spent too), plus
  `tars list --since <date> --json` for corroborating ticket and PR movement.
  If the day's Granola files aren't there yet, offer `sync-granola` first — the
  date-prefixed filenames make the gap visible without a fetch.
- **Week** ("weekly track"): the same read over the calendar week —
  `date -v-mon +%Y-%m-%d` gives this week's Monday on macOS (today, if today is
  Monday) — or an explicit window the user names.

Output, in chat, never a file:

- **By ticket** — one line per issue worked, its days and durations, linked as
  `[[<stem>|<KEY>]]`. This is the spine of the view: it answers "what have I been
  putting time into" directly.
- **Meetings** — a separate band, listed not summed. Meetings are time, but they
  are not entries; adding the two together double-counts.
- **Unticketed** — the count and the lines. Seeing this every Friday is what
  makes the tickets get created.
- **Gaps** — days in the window with no activity record at all, named. A silent
  gap reads as "no work"; an explicit one reads as "not logged".

Do not compute a total-hours figure. Durations live in prose (there is no
`--meta` passthrough on `tars add`, so nothing is summable in SQL), coverage is
the point, and a confident wrong total is worse than none.

## Notes

- **Local-only by default.** Durations are recorded in the entry and go nowhere
  else — this is a track for yourself. Pushing a real worklog to Jira
  (`addWorklogToJiraIssue`) is a deliberate opt-in, not something this skill does
  on its own.
- **Relationship to the other passes.** `end-of-day` reads the day's activity
  record as one of its bands and adds planning on top; the weekly `digest` is
  delta-scoped by watermark and organized by concept, and cites activity records
  as sources like any other document. This skill is calendar-scoped and organized
  by ticket — a different axis, which is why it does not touch the digest cursor.
- **Judgment stays where it lives.** Ticket ingestion is `sync-jira`'s job,
  commitments are the `tasks` skill's, hub polish is the `gardener`'s. This skill
  writes activity entries and reads them back.
