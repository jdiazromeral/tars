---
name: gardener
description: Review and tidy the TARS wiki taxonomy — merge near-duplicate concepts, rename bad slugs, prune dead hubs, repair missing links, and tend people pages (duplicate identities, unresolved handles, dead pages). Trigger on "garden the concepts/taxonomy/wiki", "clean up concepts", or periodically after heavy ingestion. Agents create concepts and people freely at capture time; this skill is the counterweight.
---

# Garden the wiki taxonomy

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Concepts are created freely during capture/sync, so entropy accumulates by
design. Gardening is a propose-then-apply loop — never restructure without
showing the plan first.

## 1. Survey

- `ls wiki/concepts/` and read every hub page (they're small).
- For each concept, count references: grep for `[[<slug>]]` across `raw/`,
  `wiki/notes/`, `digests/`, `tasks/`, and other concept pages.

## 2. Diagnose — look for:

- **Near-duplicates / synonyms**: `generic` vs `generic-service`,
  singular/plural, ES/EN variants → merge candidates.
- **Too broad**: a hub linked from almost everything (e.g. `seo`) — propose
  splitting or demoting; a concept that doesn't discriminate isn't shelving.
- **Too narrow / dead**: one source, no growth in weeks → propose merging
  into its parent or deleting.
- **Drift**: hub description no longer matches what's shelved under it.
- **Broken hygiene**: dangling `[[links]]` in notes, digests, and tasks;
  hub pages missing a description or `## Notes` curation. (Hub `## Sources`
  drift is no longer a diagnosis — `tars hubs` regenerates it; just run it.)
- **Skeleton hubs**: pages `tars hubs` created that still carry the placeholder
  title or no description — capture deliberately defers this polish here. Give
  each a proper name, 1–2 lines of *what this is and why it matters*, and
  optionally append `— <why relevant>` clauses to key `## Sources` entries
  (regeneration preserves them while the doc stays shelved).
- **People (`wiki/people/`)**: two pages for one human (name variants,
  personal vs work email) → merge; identity frontmatter with unresolved
  fields that recent sources could fill (a GitHub handle seen on a PR, a
  team change); pages that never grew past creation → propose pruning;
  recurring plain-text names in attendee lines that have crossed the
  page-worthiness threshold (~3+ captures) → propose creating.

## 3. Propose

Present a numbered plan (merge X into Y, rename A→B, delete C, repair D…)
with reference counts as evidence. Wait for the user's approval — they may
approve a subset.

## 4. Apply (approved items only)

- Renames/merges: reshelve documents through the CLI — `tars tag <doc_id>
  --concept <new>` then `tars untag <doc_id> --concept <old>` (concepts live
  in raw frontmatter; tag/untag are the only writers, never hand-edits).
  Update remaining `[[old-slug]]` → `[[new-slug]]` occurrences in
  `wiki/notes/`, `digests/`, and `tasks/` by hand.
- Run `tars hubs` — it regenerates every hub's `## Sources` from the new
  shelving. Move `## Notes` entries and the description onto the surviving
  hub yourself (those are curated, not derived); delete the absorbed page.
- Finish with a summary: what changed, final concept count, anything left
  unresolved.
