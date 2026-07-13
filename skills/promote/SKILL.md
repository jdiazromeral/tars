---
name: promote
description: Promote a durable insight from a raw capture into the curated notes/ layer. Trigger when the user approves keeping an insight ("promote that", "save that as a note", "add that to the wiki") or accepts your offer after an answer.
---

# Promote an insight

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Promotion is deliberate and rare — notes/ stays small and high-signal
(decisions, rationale, concepts). Never bulk-promote.

1. Confirm the exact insight wording with the user if you haven't already.
2. `tars promote <doc_id> --title "<title>"` — creates the note
   skeleton with provenance frontmatter and prints its path.
3. Edit the created file: replace the placeholder with the distilled insight
   (a few sentences to a few paragraphs, not a dump of the source). Link the
   concepts it belongs to (`[[<concept-slug>]]`) and related notes — check
   `wiki/concepts/` and `wiki/notes/` for candidates. Keep the generated
   `Source: [[…]]` line — it's the Obsidian backlink to the raw capture.
4. Add the note under `## Notes` on each linked concept's hub page.
5. Show the user the final note content.
