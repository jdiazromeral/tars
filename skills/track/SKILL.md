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
its own `activity` connector — no connector code behind it, the generic
`tars add - --connector --origin --append` pipe carries the whole thing.

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
mirrors the `granola` convention (`<YYYY-MM-DD> <title>`, dated by the meeting's
start time), which is what makes a day or a week readable as a filename glob
across both streams:

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
   the entry is logged in the small hours (before 04:00 local), yesterday's
   record exists and today's does not — that is usually still yesterday's work,
   so ask "is this still yesterday's work?". At any other hour, today is today.

2. **Resolve the ticket.** The point of the ledger is that entries land on a
   real issue when there is one:

   - **A key was given** (`DESEO-1234`) — find the ingested issue by its origin
     `jira:<KEY>`: `tars list --connector jira --json`, filtered to that origin
     (the CLI has no exact-origin query). If it is not there, pull it through the
     `sync-jira` skill's **concrete-issue** mode (which correctly leaves the
     watermark alone) and look it up the same way. Then get the link stem from
     `tars show <id> --path` (the file's basename, without `.md`) — **never guess
     the stem from the title**: collision suffixes and renamed tickets make the
     title a bad predictor. **Nothing will catch a wrong stem later**: `tars
     doctor` deliberately excludes `raw/` from its dangling-link check (captured
     third-party text can contain literal `[[...]]` that isn't a wiki-link), so
     an entry pointing at a document that was never ingested is a silently dead
     link. Verify here, or don't write a link — if the ticket can't be ingested
     (no access, MCP down), record the bare key as plain text and say so in the
     report.
   - **Prose, no key** ("the cache invalidation thing") — `tars search` the
     corpus and offer the matching ticket. Usually it exists and the user simply
     didn't say the number.
   - **Nothing matches** — ask (step 3).

3. **Ask for what's missing — once, and skippably.** When either slot is empty,
   ask in a **single** prompt, not two:

   - *No ticket resolved* → ask for the key, or accept `unticketed`.
   - *No duration given* → ask how long.

   Both are **declinable**, and a decline applies to **that one entry only** —
   not the rest of the session. "No ticket" records the entry as `unticketed`,
   "skip time" records `—`. This prompt exists to catch the entries worth
   catching, not to interrogate every line: real work is legitimately unticketed
   sometimes (interviews, an incident, a 1:1, admin), and the weekly review
   reports that count instead of the skill refusing the entry.

4. **Append to the day's record** with `tars add - --append`. Same origin every
   time, so the day is one document; the append happens under the DB lock, so
   there is no model rewrite of prior entries and no lost writes across
   concurrent sessions.

   - **First entry of the day** (no `raw/activity/<date>-*.md` yet): the body is
     the header — `# <YYYY-MM-DD> Activity`, a blank line, `## Entries`, a blank
     line — plus the entry.
   - **Later entries**: the body is just the single entry line; `--append` lands
     it as the next line under `## Entries`.

   ```sh
   tars add - --append --connector activity --origin "activity:<YYYY-MM-DD>" \
     --title "<YYYY-MM-DD> Activity" --tag activity --concept activity-log \
     < /path/to/scratch.md
   ```

   Write the body to a scratch file and redirect it — heredocs mangle special
   characters.

   Always run `tars hubs` afterwards (a new day record is a new row on the
   `activity-log` hub, and the hub skeleton on the very first ever entry).

5. **Report** one line: the day, the entry as stored, whether the ticket was
   resolved / left unticketed, and the add status.

## Review mode

Read-only, date-scoped, no watermark — re-run it as often as you like and it
answers the same. Read the **filename dates**, not `captured_at`.

- **Day** ("what did I work on today"): the day's activity record, plus that
  day's meetings (`raw/granola/<date>-*.md` — meetings are time spent too), plus
  `tars list --since <date> --json` for corroborating ticket and PR movement,
  bounded to that day: drop results whose `captured_at` is on or after the next
  day (for a past day the open-ended `--since` would otherwise pull in
  everything since). A bare date compares against UTC `captured_at`, so the edges
  are fuzzy by the local UTC offset; use `end-of-day`'s local-midnight cutoff
  when that matters. If the day's Granola files aren't there yet, offer
  `sync-granola` first — the date-prefixed filenames make the gap visible without
  a fetch. Granola's filename date comes from the meeting's start time and may be
  UTC-shifted near midnight, so the glob is a close proxy for meetings, exact for
  activity.
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
- **Gaps** — past weekdays in the window with no activity record at all, named
  (no weekends, no days still to come). A silent
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
