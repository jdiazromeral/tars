---
name: digest
description: Generate a digest of everything that entered TARS since the last digest — summaries and key decisions by concept, open questions, and extracted commitments as task records under tasks/. Trigger on "digest", "weekly digest", "what came in", "catch me up on my brain".
---

# Digest TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Digests are **derived artifacts**: regenerable views over raw/, never truth.
Every claim links its source as an Obsidian wiki-link —
`[[<raw-file-stem>|<title>]]` (raw files are named by human title; `tars
search --json` returns the stem in the `file` field). Organize content by
**concept**, not by document — concepts are the vault's grouping. Never edit
notes/ from here.

1. **Scope**: `tars cursor digest` gives the last digest watermark (ISO
   timestamp; empty = ask the user for a window, default last 7 days). Then,
   **before reading anything**, stamp the sweep start: `tars cursor digest
   --begin` — two-phase advance, so a document captured while you work lands
   in the *next* digest instead of being skipped forever. The new set is
   `tars list --since "<watermark>" --json` (id, title, connector — no
   bodies); `tars log --json` distinguishes added vs updated when it matters.
2. **Read at the depth each document deserves** — never full-`show` by
   default:
   - **Connector backfills (github, jira sweeps)**: the titles are the digest —
     one summary line for the batch, per-doc lines only for items that changed
     something (a decision in a PR discussion, a ticket that flipped state).
   - **Meetings (granola)**: `tars show <id> --head 60` covers frontmatter +
     the Notes panel; `tars show <id> --grep "next steps|próximos pasos" -C 6`
     mines commitments. Read the transcript only when a claim needs the exact
     words.
   - **Small captures (note, agent, slack, web, file)**: full `tars show` is
     usually fine — they're short.
3. **Write `digests/<YYYY>-W<week>.md`** (append a dated section if it exists):
   - **What came in** — one line per document: `[[<file-stem>|<title>]]` + connector
     (a connector backfill collapses to one line with a count).
   - **By concept** — the heart of the digest: one `### [[<concept>]]`
     subsection per active concept, containing that concept's decisions
     (actually made, not discussed), movements, and open questions/tensions —
     each claim ending with its `[[<file-stem>|<title>]]` source link. Cover
     every concept touched this period; cross-cutting items go under the most
     load-bearing concept with links to the others.
   - **Unshelved** — anything that fits no concept; if it recurs, that's a
     signal to create one.
   - **Retrieval health** — count of new `retrieval-misses.md` entries this
     period (omit the section when zero). A sustained miss rate around ~20%
     of asks is the agreed trigger to consider a semantic-search rung — flag
     it when the trend points there.
   - **Promotion candidates** — at most 2–3 insights worth `tars promote`,
     phrased and ready for the user to approve, each linked.
4. **Extract commitments into `tasks/`** — one file per commitment, from
   meeting "next steps" and explicit commitments in any source. Before
   creating one, grep `tasks/` for the same `Source:` link and action
   (extraction is idempotent). File `tasks/<created-date>-<slug>.md`:

   ```markdown
   ---
   status: open
   owner: me            # or the person's slug
   due: 2026-07-10      # or —
   created: 2026-07-06
   ---

   <the action, one clear sentence>

   Source: [[<file-stem>|<title>]]
   Concepts: [[<concept-slug>]]
   Owner: [[<person-slug>|<Name>]]   # only for others' commitments
   ```

   Agents never flip `status` or delete task files — that's the user's.
   Then **regenerate `tasks/TASKS.md`** exactly as the tasks skill specifies:
   `## ⚠ Overdue` first (any open task past its due date, omitted when empty),
   then `## Mine` / `## Waiting on`, sorted by due date (nulls last).
5. **Commit the watermark**: `tars cursor digest --commit` — only if every
   document was covered and the digest written cleanly; on any failure leave it
   uncommitted so the next run re-scans from the old watermark.
6. **Show the user** the digest content and any new tasks, and mention
   documents skipped (empty, unreadable) by name. If `## ⚠ Overdue` is
   non-empty, list those tasks and ask which are done, moved, or need a new
   date — apply what the user answers.
