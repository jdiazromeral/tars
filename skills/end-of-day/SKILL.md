---
name: end-of-day
description: End-of-day review — sync today's work in, then show what you did today (by concept) alongside what's open for tomorrow. A read-only planning pass, NOT the digest. Trigger on "end of day", "eod", "what did I do today", "wrap up my day", "plan my tomorrow", "daily review".
---

# End-of-day review

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — cadence, source allowlists, tone, and privacy carve-outs there
> override this skill's defaults on conflict.

A daily "what did I do, what's next" pass, for closing out a day and planning
the next. It is **not** the digest and must never behave like one:

- **No `digests/` artifact, no digest watermark.** The digest is the *weekly*
  rollup with a committed two-phase cursor (`cursor digest --begin/--commit`);
  advancing it from here would fragment the weekly view and skip documents.
  This pass writes nothing to `digests/` and touches no digest cursor.
- **Date-scoped, not delta-scoped — so it takes no watermark of its own.**
  "What did I do today" must read the same whether you run it at 18:00 or again
  at 20:00; a watermark would make the second run show "nothing new." Re-running
  is safe and idempotent by design.
- **The only write is optional, additive task extraction** (step 4, via the
  `tasks` skill) — it never flips status or deletes. Everything else is reads.

## Why sync comes first (the load-bearing mechanic)

`tars list --since` filters on **`captured_at`** — *when a document landed in
TARS* (stored in UTC) — **not the work's own event date**, which lives
unindexed inside the raw body. So today's Jira movements, PRs, and meetings are
invisible to this pass until a sync pulls them in. **Sync, then review**, or
"today" comes back empty.

Corollary: because the filter is capture-time, `--since <today>` equals "what I
did today" only when you sync roughly daily. Sync after a quiet stretch and a
two-week-old meeting you *just* ingested shows up as "today." For a daily habit
that's exactly right; just know the proxy.

## Steps

1. **Sync today's work in** (default: yes). Delegate to the `sync-all` skill
   with the **digest step skipped** — this pass replaces the digest, it doesn't
   run it. Narrow on request ("just jira and github today"), or skip entirely if
   the user already synced this session ("I just synced, only review"). Slack is
   excluded there as always; pull threads by hand if one mattered today.

2. **Fix the day boundary.** Simplest is a bare `--since <YYYY-MM-DD>` (today's
   date) — but note `captured_at` is UTC, so a bare date cuts at **00:00 UTC**,
   which is *not* local midnight (e.g. 02:00 in CEST). That's fine for a daytime
   review; it only drops work captured in the small hours of local morning. For
   an exact **local-midnight** cutoff, compute it in UTC — zero H/M/S in local
   time, then format the resulting instant as UTC:

   ```sh
   date -u -r "$(date -v0H -v0M -v0S +%s)" +%Y-%m-%dT%H:%M:%SZ   # today 00:00 local, in UTC (BSD/macOS)
   ```

   Pass whichever you pick to `--since`. Honor an explicit window the user gives
   ("since lunch", "last 24h", "since yesterday") instead.

3. **Retrospect — what landed today.** `tars list --since "<boundary>" --json`
   (ids, titles, connectors — no bodies), and read each hit at the depth it
   deserves, exactly as the `digest` skill prescribes:
   - **Connector backfills (github, jira sweeps)** — the titles are the review;
     collapse to one line with a count, break out only an item that *changed
     something* (a decision in a PR thread, a ticket that flipped state).
   - **Meetings (granola)** — `tars show <id> --head 60` for frontmatter + Notes;
     `tars show <id> --grep "next steps|próximos pasos" -C 6` to mine commitments.
   - **Small captures (note, agent, slack, web, file)** — a full `tars show` is
     usually fine; they're short.
   - `tars log --json` distinguishes added vs updated when a line needs it.

4. **Extract today's commitments (offer, don't force).** If today's captures
   hold concrete new commitments (a meeting "next step", an explicit promise),
   offer to run the `tasks` skill over just those sources. It is idempotent and
   additive — dedupe against `tasks/` first, create one file per commitment,
   never flip status. This is what makes tomorrow's plan actionable; skip it if
   nothing durable surfaced (daily standups rarely yield real tasks).

5. **Forward — what's open for tomorrow.** Read `tasks/TASKS.md` (regenerate it
   first if step 4 added anything): lead with `## ⚠ Overdue`, then your
   due-soon `## Mine`. Add loose ends spotted in today's captures that aren't
   tasks yet — a PR still awaiting review, an unanswered thread, a decision left
   hanging.

6. **Show a scratch summary in chat** — not a file:
   - **Done today** — grouped by concept (the vault's spine), each line ending
     in its `[[<file-stem>|<title>]]` source link; a backfill collapses to one
     counted line.
   - **Open for tomorrow** — overdue first, then due-soon and today's loose
     ends, each linking its task/source.
   - Name anything skipped (empty, unreadable) and, if `## ⚠ Overdue` is
     non-empty, ask which are done / moved / need a new date and apply the
     answer.

## Notes

- **This is the daily counterpart to the weekly digest, not a replacement.**
  Daily = this read-only planning pass. Weekly = `/tars:digest`, the persisted
  by-concept artifact with the committed watermark. If you want today's review
  *persisted* and folded into the week, run `/tars:digest` instead — it appends
  a dated section to the current `digests/<YYYY>-W<week>.md`, so a daily digest
  habit pre-assembles the weekly view. This skill deliberately trades that trail
  for a zero-clutter, re-runnable pass.
- **Judgment stays where it lives.** Concept shelving and people wiring happen
  inside each `sync-<connector>` skill (via `sync-all` in step 1); task
  discipline lives in the `tasks` skill. This skill only sequences and reads.
- **Idempotent and interrupt-safe.** No watermark to advance, additive-only
  writes — run it as many times a day as you like.
