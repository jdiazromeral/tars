---
name: sync-github
description: Sync GitHub pull requests into TARS. Trigger on "sync github", "pull my PRs into tars", "ingest my pull requests". The fetch is pure code — `tars sync github`, scoped by connectors.yml (orgs/repos/authors) — this skill adds the judgment after: concept shelving, people backfill, hubs, report.
---

# Sync GitHub → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Unlike sync-granola/sync-jira, the transport here is deterministic code: the
`github` **code connector** fetches PRs via the authenticated `gh` CLI and
owns storage, idempotence, and the cursor. You run it and then do only what
needs judgment.

## 1. Run the sync

```sh
tars sync github
```

- Scope comes from `connectors.yml` at the vault root (`github.orgs` /
  `github.repos` / `github.authors` / `github.reviewers` — the last adds PRs
  the handle is asked to review, both `review-requested:` and `reviewed-by:`,
  so a thread keeps updating after the review is submitted); the connector
  **refuses to run unscoped** — if it errors asking for scope, show the user
  the config stanza and ask which orgs/repos to watch. Never work around the
  refusal.
- The watermark self-manages: persisted only on clean completion, so a
  failed run re-scans safely. No `tars cursor` calls needed.

## 2. Shelve what landed (judgment)

The sync output already names every added/updated doc (`status  doc_id  title`
per PR) — shelve straight from it, no listing needed
(`tars list --connector github --since "<sync start>" --json` is the fallback
if the output scrolled away). For each: `tars tag <doc_id> --concept <slug> ...`
with 1–4 concepts judged from the PR title — reuse aggressively
(`ls wiki/concepts/`; a webapp PR about login flows likely shelves under
[[authentication]], a search PR under [[search-ranking]]). Then `tars finalize` once (regenerate hubs, clear index drift,
re-check invariants); skeleton-hub polish is the gardener's job.

## 3. People backfill (the identity map earning its columns)

PR authors and reviewers arrive as GitHub handles. For each handle that
matches an existing `wiki/people/` page whose `github:` frontmatter field is
blank, fill it — that mapping is exactly what the person pages exist for.
Create a NEW person page only for a 1:1 counterpart, a commitment owner, or
someone recurring across ~3+ captures — never bulk-create from reviewer lists.

## 4. Report

Counts (the connector prints added/updated/unchanged per author), concepts
created vs reused, people fields backfilled, and anything skipped or errored
— name it explicitly so nothing is silently dropped. Call out **state
transitions**: PRs whose `meta.state` flipped to merged/closed this sync
(closing bumps `updated`, so the terminal snapshot is captured — after which
a dead PR drops out of every future sweep on its own; no cleanup needed).

## Notes

- **Evaluations mode**: to gather evidence for the team, temporarily add
  their handles to `github.authors` in `connectors.yml`, sync, and their PRs
  land as documents linked (via shelving + people pages) to each person.
  Remove the handles after the sweep if you don't want ongoing tracking.
- The PR document holds metadata + description + the full review discussion,
  NOT the diff — code's source of truth is the repository itself.
