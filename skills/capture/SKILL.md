---
name: capture
description: Save something into TARS — an external source (URL, file, pasted excerpt), the user's own words (a thought, note-to-self, running note), or an agent-authored synthesis of session work. Trigger on "save this", "capture this", "add to tars/my brain", a bare URL/file drop meant for keeping, "note this down", "note to self", "jot this", "save the work to tars", "record what we did". Routes by whose words it is; verbatim except for agent-authored work records.
---

# Capture into TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Everything lands through `tars add`; the CLI owns storage, dedup, and
provenance. Route by **whose words** it is — that decides the connector, and
who authored it is load-bearing (never store your synthesis as the user's
words, or their words as yours):

| What it is | Command |
|---|---|
| External source — a URL, a file | `tars add "<url>"` / `tars add "<path>"` |
| External text pasted from elsewhere (an excerpt, someone else's message) | pipe to `tars add - --title "<short title>"`, with `--origin "<source-url-or-ref>#<short-slug>"` when it has one — the URL keeps provenance, the slug keeps two excerpts of one source apart |
| The user's own words — a thought, note-to-self, decision, running note | pipe **verbatim** to `tars add - --title "..."` (lands under the `note` connector); a living note they keep adding to → pin `--origin "note:<stable-slug>"`, and add to it later with `--append` |
| A synthesis *you* authored — a work-log or decision record, kept at the user's request | `tars add - --connector agent --origin "agent:<stable-slug>" --title "..."`; a later session extends it with `--append` |
| Files dropped in `inbox/` | `tars sweep`, then shelve what landed like any capture |

Route rules:

- **External sources: zero interpretation** — never summarize or edit content
  on the way in. If extraction fails (paywall, JS-only page), fetch it yourself
  (WebFetch / agent-browser) and pipe the text through `tars add -` with
  `--origin "<url>"`. Optional labels the user gives you: `--tag <tag>`.
- **User's words: verbatim.** Light shaping only (split a dictated run-on into
  paragraphs, keep an obvious list a list) — store what they said, don't reword
  or interpret.
- **Agent notes: a faithful record** of work actually done — what was done,
  decisions and their *why*, open follow-ups; don't pad or speculate. This is
  the one sanctioned agent-authored path, and it's user-initiated — never a
  silent summary of a doc you just synced (that would be write-time
  distillation). The stable slot grows across sessions
  (`agent:proj-1234-worklog`): add the new session's record with `--append`;
  omit `--origin` only for a frozen one-shot record.
- **Stable slots vs content-addressing**: with no `--origin`, pasted text
  content-addresses (`note:<hash>`/`agent:<hash>`) so re-adding identical text
  dedupes. A note the user keeps adding to wants a named slot — a slug they'd
  say out loud and reuse verbatim (`note:onboarding-questions`).
- **Adding to an existing note or worklog is always `--append`**
  (`tars add - --append --origin note:<slug>`): it keeps the title and tags,
  and adds the text under the lock. tars **refuses** to replace the user's or
  your own earlier words — a re-add with different text and no `--append`
  fails, by design. If `--append` reports no document at that origin, find
  the right slot (`tars list --connector note`) rather than adding
  `--create`, which is only for deliberately starting a new slot.
- Piped text needs `--title` (nothing to extract one from). For multi-line
  text, write a scratch file and pipe it — heredocs mangle special characters.

## Shelve to concepts (mandatory, no ceremony)

Every capture gets 1–4 concepts — pass `--concept <slug>` at add, or
`tars tag <doc_id> --concept <slug> ...` after. `ls wiki/concepts/` first and
**reuse aggressively**; mint a new concept only when nothing fits, as a
general speakable slug (`generic-service`, not
`translation-context-admin-api`). Then `tars hubs` once — it regenerates every
hub's `## Sources` (and skeletons for new concepts). Hub polish — titles,
descriptions, relevance clauses — is the **gardener's** job, not capture's.

## Report

One line: the add result (`added`/`updated`/`unchanged`), the origin slot if
named, and the concepts attached — flag newly minted ones so the taxonomy
growing stays visible.
