---
name: ask
description: Answer a question from the TARS corpus with citations. Trigger on "what do we know about...", "why did we...", "search my brain/tars for...", or any question about past decisions, meetings, tickets, or captured sources.
---

# Ask TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Cheap first, escalate only when needed — a full document is the *last* resort,
never the default (one meeting capture can be ~30k tokens).

1. **Hub check**: if a `wiki/concepts/` page matches the topic, its `## Notes`
   and `## Sources` lists are a curated map of the best material.
2. **Search with chunks**: `tars search "<query>" -v --json` — each hit carries
   its best-matching chunk verbatim (~400 tokens), which often already holds
   the answer. Try 2–3 phrasings if the first is thin (different vocabulary,
   entity names, ticket keys); `--connector` narrows.
3. **Escalate per document, not per question** — only when a hit's chunk isn't
   enough:
   - `tars show <doc_id> --grep "<term>" -C 5` — the lines around every match
     (numbered, so you can re-aim with a wider `-C`).
   - `tars show <doc_id> --head 40` — frontmatter + opening (a meeting's Notes
     panel sits at the top).
   - Full `tars show <doc_id>` only when the answer genuinely needs the whole
     document.
   Also grep `wiki/notes/` — promoted insights outrank raw captures when they
   conflict.
4. **Log vocabulary misses.** If the first phrasing found nothing useful and a
   reformulation did — the idea was in the corpus under different words — append
   one line to `retrieval-misses.md` at the vault root:
   `- <YYYY-MM-DD> · asked: "<original phrasing>" · found via: "<phrasing that worked>"`.
   Don't log corpus gaps (the answer truly isn't there) or typos — only
   vocabulary mismatches. This file is the trigger instrument for semantic
   search: it gets built when the data says lexical search fails too often,
   not before.
5. Synthesize the answer **only from what the corpus supports**, citing each
   claim with its doc id and origin (URL / path / ticket). If the corpus
   doesn't contain the answer, say so plainly — never fill gaps silently.
6. If the answer surfaced a durable insight (a decision, a rationale, a
   concept worth keeping), offer to promote it — see the promote skill.
