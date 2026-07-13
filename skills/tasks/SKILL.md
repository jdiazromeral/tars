---
name: tasks
description: Extract commitments from captured sources into task records under tasks/, and regenerate tasks/TASKS.md. Trigger on "extract tasks", "what did I commit to", "turn that meeting into tasks", or after ingesting sources rich in action items. Idempotent and additive — it creates task files and rebuilds the index; it never flips status or deletes.
---

# Extract tasks from the corpus

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — per-vault rules there (source allowlists, tone, privacy, output
> layout) override this skill's defaults on conflict.

Tasks are first-class **records**, one file per commitment — not lines in a
list. This skill turns action items buried in captures into that layer. It is
connector-agnostic: run it over any recent captures (meetings, threads, docs),
not just Granola.

## Scope discipline

A task is a concrete, owned commitment with a clear action — "crear la épica
de Generic Q3 en Jira", "revisar el roadmap antes del viernes". NOT:
vague aspirations, discussion points, or standing responsibilities. Prefer
under-extraction: a wrong or noise task erodes trust in the whole layer. Daily
standups rarely yield durable tasks; alignment / 1:1 / planning meetings do.

Capture two owners:
- **Mine** — the user committed to it (`owner: me`).
- **Waiting on** — someone else owns a commitment that affects the user
  (`owner: <person-slug>`).

## 1. Gather candidates

Identify the source(s). If the user named a meeting/doc, `tars show <doc_id>`;
otherwise `tars search` or read the relevant `raw/` files. Pull every explicit
commitment or "next step", with its owner and any due date. Meetings usually
end with a "Próximos pasos / Next steps" block — mine that first.

## 2. Dedupe before writing (mandatory)

For each candidate, grep `tasks/` for the same source link AND a matching
action; if found, skip (do not create a second file, do not edit status).
Re-running this skill over the same source must be a no-op.

## 3. Write one file per new commitment

Path: `tasks/<created-date>-<short-slug>.md` (e.g. `2026-07-06-generic-q3-epic.md`).

```markdown
---
status: open
owner: me            # or a person slug, e.g. hubot
due: 2026-07-10      # ISO date, or null
created: 2026-07-06  # date of the source that generated it
---

<the action as one imperative sentence>

Source: [[<raw-file-stem>|<source title>]]
Concepts: [[<concept-slug>]] [[<concept-slug>]]
Owner: [[<person-slug>|<Name>]]   # only for "waiting on" tasks owned by others
```

- `owner: me` tasks omit the `Owner:` line.
- If the owner is someone with a `wiki/people/` page, link them; owning a
  commitment makes someone page-worthy, so create the person page if they
  don't have one yet.
- Link the concept(s) the task belongs to so it joins the graph.

## 4. Regenerate the index

Rewrite `tasks/TASKS.md` from all `status: open` task files, grouped:

```markdown
# Open tasks

_Regenerated from tasks/*.md — do not edit by hand._

## ⚠ Overdue

- [ ] <action> — <Owner name if not me> — due <due> — [[<task-file-stem>]] · [[<source-stem>|src]]

## Mine

- [ ] <action> — due <due or "—"> — [[<task-file-stem>]] · [[<source-stem>|src]]

## Waiting on

- [ ] <action> — <Owner name> — due <due or "—"> — [[<task-file-stem>]] · [[<source-stem>|src]]
```

`## ⚠ Overdue` holds every open task (any owner) whose `due` is before today —
those move *out* of their home group so the index leads with what's rotting;
omit the section when nothing is overdue. Sort every group by due date (nulls
last). Include only open tasks.

## 5. Boundaries

- **Never** change a task's `status` or delete a task file on your own — that
  is the user's call (they say so in chat or edit the file). You only *create*
  records and *regenerate* the index.
- **Do nudge**: after regenerating, if anything sits in `## ⚠ Overdue`, list
  those tasks to the user and ask which are done, moved, or need a new date —
  then apply whatever they answer (that explicit answer is the user's call
  being made).
- Report: how many tasks created (Mine / Waiting on), how many candidates
  skipped as duplicates, and any people pages created.
