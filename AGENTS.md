# TARS — agent instructions

TARS is a local-first second brain: an immutable raw archive + a rebuildable
SQLite/FTS5 index + a curated wiki (concepts, people, notes) + task records.

## Layout

The vault is the directory holding the `.tars` root marker. Its name and
location are **arbitrary** — it lives *outside* this repo, wherever the user
put it, under whatever name they chose (Javi's is `~/tesseract`; nothing in
the code knows or cares). **Every path in this file and in the skills is
relative to that vault root**, so operate from inside it: `cd "$TARS_HOME"`
before any vault file op (the bare `ls`/`grep`/read/write on `raw/`, `wiki/`,
`tasks/`, `digests/` the skills describe). `TARS_HOME` is set in the user's
`~/.claude/settings.json` env block (and shell profile); the `tars` CLI also
finds the root by walking up to the `.tars` marker, so it works from anywhere.
Keeping paths root-relative — never hardcoding a vault name — is deliberate:
the vault is relocatable and renamable by moving the directory and updating
`TARS_HOME`, nothing else.

```
inbox/                            zero-ceremony drop zone — `tars sweep` drains it
raw/<connector>/<title-slug>.md   machine archive — complete, immutable, CLI-only
wiki/concepts/<slug>.md           what things are — the hubs everything shelves under
wiki/people/<slug>.md             who's involved — identity map across our tools
wiki/notes/<slug>.md              promoted insights (tars promote)
tasks/<created>-<slug>.md         one task per file — truth, owned lifecycle
tasks/TASKS.md                    regenerable index of open tasks
digests/<YYYY>-W<week>.md         regenerable rollups, organized by concept
log/ingestions.jsonl              append-only ingestion history (`tars log`)
AGENTS.md                         optional per-vault house rules — skills read + honor it
tars.db                           disposable index — tars reindex rebuilds it
```

## Architecture invariants

- **Truth is `raw/`, `wiki/`, and `tasks/`** — plain files, tracked in git.
  `tars.db` is a disposable cache. Never treat the DB as the only copy of
  anything.
- **Plumbing is code, judgment is the model.** Fetching, normalizing,
  chunking, indexing, dedup and sync live in the `tars` CLI (`src/tars/`).
  Agents do extraction from messy content, shelving, and query-time
  synthesis — always *through* the CLI for `raw/` and `tars.db`; the `wiki/`
  and `tasks/` layers are agent-edited markdown.
- **Ingestion is idempotent**, keyed by `(connector, origin)`. Re-adding
  unchanged content is a no-op; changed content is an upsert. New connectors
  must preserve this (see `src/tars/connectors/__init__.py`). The `origin` is
  always a **stable, source-native id** (`jira:<KEY>`, `granola:<id>`, a URL,
  `note:<content-hash>`, `agent:<slug>`, `file:<content-hash>`) — never a timestamp or a local
  path, so re-capture never mints a duplicate (the path lives in `meta`). Identity lives in `origin`; change-detection in
  `content_hash`; the sync cursor is only a fetch optimization.
- **Mutable sources go stale; refresh them by id.** A Jira issue keeps changing
  after ingest (comments, status, state). The incremental watermark only
  *discovers* changes for items in scope (e.g. assigned to me); an item pulled
  ad-hoc must be re-fetched by id to refresh its snapshot — re-ingest is a no-op
  when unchanged, an upsert when not. Enumerate what's ingested with
  `tars list --connector <name>`; a ticket that 404s on refresh is flagged,
  never deleted (status changes belong to the user).
- **Write-time distillation is forbidden as a pipeline step.** Raw captures
  are stored complete. Distillation happens at query time, and only durable
  insights get promoted into `wiki/notes/` — with the user's approval, never
  in bulk.
- **Raw is append-mostly, not literally immutable.** Three sanctioned,
  auditable mutation paths exist — vocab normalization (below), concept
  shelving via `tag`/`untag`, and `tars rm` (redaction) — all through the
  CLI. Anything else that changes a raw file by hand is a bug.
- **Concepts are shelving state, never content.** They live in raw
  frontmatter (`concepts:`); the body `Concepts: [[...]]` line is derived at
  write time and excluded from the content hash. On re-ingest concepts
  **merge** (a re-sync can add shelving, never remove it); only `tars untag`
  takes one away. Hub `## Sources` sections are a **regenerated view** — run
  `tars hubs` after tagging instead of hand-appending entries (descriptions,
  `## Notes`, and per-entry relevance clauses survive regeneration).
- **Raw files are named by human title** (slug, collision-suffixed with a
  short id; fixed at first ingest so links never break). The doc id lives in
  frontmatter and the DB as the idempotence key — filenames and
  `[[wiki-links]]` are for humans, ids are for machines.
- **Concepts (`wiki/concepts/`) are the vault's grouping**: hub pages (short
  description + `## Notes` + `## Sources`) that everything shelves under.
  Every capture gets 1–4 concepts (`--concept` on add, or `tars tag` after);
  `tars hubs` keeps the hub pages' `## Sources` in step. Agents create concepts freely —
  reuse aggressively, mint general speakable slugs — and the `gardener`
  skill is the counterweight. Concept assignment is shelving, not
  summarizing. Hubs may carry optional frontmatter marking *what kind* of hub
  they are — `kind: team|project|service|topic|roadmap` (plus `status`,
  `quarter` for time-boxed ones) — so concrete deliverables stay queryable
  (`kind: roadmap`) without leaving the flat namespace: the marker groups, not
  a subfolder.
- **People (`wiki/people/`) map humans across our ops.** A person page holds
  the identity map in frontmatter (`emails`, `github`, `slack`, `jira`,
  `role`, `team`) plus 1–2 lines of context; backlinks do the rest. Create a
  page only when the relationship is real: a 1:1 counterpart, an owner of a
  commitment in `tasks/`, or someone recurring across ~3+ captures — a
  22-person meeting does NOT mint 22 pages. Wherever a person with a page
  appears (attendee lines, task owners, PR authors), reference them as
  `[[<slug>|<Name>]]`; people below the threshold stay plain text until they
  cross it. The `gardener` skill also tends people: merges duplicate
  identities, prunes pages that never grew, flags unresolved handles.
- **Tasks are records, not lines.** One file per commitment under `tasks/`,
  frontmatter `status: open|done|dropped`, `owner` (person slug or `me`),
  `due`, `created`; body = the action sentence plus `Source:`, `Concepts:`
  and (for others' commitments) `Owner:` wiki-links. Extraction is
  idempotent — before creating one, grep `tasks/` for the same source link
  and action. Status changes belong to the user (asked in chat or edited by
  hand); agents create task files and regenerate the index, never flip
  status or delete on their own. `tasks/TASKS.md` is a regenerable index of
  open items grouped Mine / Waiting on — rebuild it whenever task files
  change.
- **Capture-tool noise is canonicalized via `vocab.yml`, never hand-edits.**
  Speech-to-text mishearings (Granola hears "Acme"/"AcmeCloud" as
  "Acne"/"AcneCloud") are noise, not content, so normalizing them makes raw
  *more* faithful. The corpus-owned `vocab.yml` (canonical → misheard variants)
  is applied inside `ingest.add` (so it survives re-sync) and by `tars
  normalize` over existing raw. This is the ONLY sanctioned way to correct raw
  text — never edit raw files by hand to fix a typo (a re-sync would overwrite
  it). Extend `vocab.yml`, then `tars normalize`. Two rules keep the ledger
  honest: (1) **entries are scoped** — STT corrections carry
  `connectors: [granola]` so a faithful web capture about *fiber optics* is
  never rewritten to fix a hearing problem (legacy flat entries = global;
  scope anything that is a real word); (2) **the agent authors, the user
  approves** — sync skills propose entries with evidence (see sync-granola's
  vocab watch), and nothing lands unapproved.
- **Retrieval misses are measured, not felt.** Lexical search (FTS5,
  accent-folded for the ES/EN corpus) is the only retrieval rung until data
  says otherwise: when an ask succeeds only after synonym reformulation, the
  agent logs it to `retrieval-misses.md` at the vault root (see the ask
  skill), and the digest counts them. A sustained ~20% miss rate is the
  trigger to build the semantic rung (`chunks.embedding` is reserved for it;
  use a multilingual model, bge-m3 class). Until then, don't.
- **Digests are regenerable views, never truth**, organized by concept;
  every claim links its source as `[[<raw-file-stem>|<title>]]` so backlinks
  connect concepts ↔ people ↔ raw ↔ notes ↔ digests ↔ tasks. Digests never
  edit `wiki/notes/`.
- **Per-vault house rules live in the vault, honored at runtime.** An optional
  `$TARS_HOME/AGENTS.md` holds vault-specific rules — source allowlists, digest
  tone, privacy carve-outs, output layout. Skills read it at the start of a run
  and let it override their defaults on conflict; this is how one machine's home
  vault and another's work vault behave differently with **no** code change or
  machine detection — the vault *is* the context boundary. Keep it short prose,
  not a config schema (it instructs a model). Distinct from **this** file: the
  repo's `AGENTS.md` instructs agents working on the *code*; the vault's
  `AGENTS.md` instructs the skills operating on the user's *data*.

## Privacy (hard rule)

This brain contains sensitive work data. The vault is split from the tool so the
files stay off any shared remote while the code circulates. Two distinct
properties — keep them straight:

- **At rest: local-only.** The vault is its own git repo with **no remote** —
  never add one (org or personal cloud), never sync it to a third-party
  indexing service. Off-machine durability is `tars backup <dir>` (a full git
  bundle) copied to an encrypted disk — commit the vault first; bundles only
  capture committed history.
- **In use: it crosses a trust boundary.** TARS is an LLM agent, so operating
  on the corpus sends its content to the model provider on every query, and the
  `sync-*` connectors route through their MCP providers (some Anthropic-hosted
  like Gmail, some third-party like Atlassian Rovo and Slack). "Private" means
  **not published or stored off-machine** — NOT unseen by a third party. Don't
  ingest data you're not permitted to send to a hosted LLM; the vault holds
  sensitive work data at your discretion, it is not a blanket "safe to dump
  anything" zone.
- **Redaction**: `tars rm <doc_id>` is the one sanctioned way to remove a
  capture (an accidental paste, a secret). It deletes the raw file, sidecar,
  and index row, and reports every wiki-link still pointing at the stem; run
  `tars hubs` after. Note: content already committed to vault git history
  needs a history rewrite too — flag that to the user, never do it silently.
- **This tool repo tracks no corpus content** — the vault lives outside it
  entirely (plus a defensive `tesseract/` gitignore entry from when it lived
  inside) — and may be shared. Before sharing, verify nothing leaked:
  `git log --all --name-only | grep -E '^(tesseract|raw|wiki|tasks|digests)/'`
  must return nothing.

## Development

- Python ≥3.11, managed with `uv`. Run things as `uv run tars ...`,
  tests as `uv run pytest`.
- The agent skills live in `skills/` and ship as a Claude Code **plugin**
  (`.claude-plugin/`), namespaced `tars:<skill>` (e.g. `tars:ask`,
  `tars:capture`) and available from any directory once installed — they
  resolve the vault through `TARS_HOME`, same as the CLI.
- Conventional Commits.
- CLI usage: `tars add <url|file|->`, `tars sweep`, `tars tag|untag <doc_id>
  --concept ...`, `tars hubs`, `tars search <query> [-v] [--json]` (`-v` adds
  each hit's best-matching chunk — usually enough to answer from),
  `tars show <doc_id> [--path | --head N | --grep <regex> [-C N]]` (token-frugal
  slices; a full show on a transcript can be ~30k tokens),
  `tars list [--connector ...] [--since <ISO>] [--json]`,
  `tars promote <doc_id> --title ...`, `tars rm <doc_id> [--yes]`,
  `tars cursor <connector> [--set ... | --begin | --commit]`,
  `tars sync <connector>`, `tars normalize`, `tars reindex`, `tars migrate`,
  `tars backup [dir] [--keep N]`, `tars doctor [--json]`, `tars status`.
- **`tars doctor`** checks vault invariants — dangling `[[wiki-links]]`
  (including a task left citing a doc `tars rm` deleted), concepts with
  shelved docs but no hub page, and DB↔raw drift. Read-only: it names the
  problem and the fix command (`tars reindex` / `tars hubs`), never mutates
  anything. Exits non-zero when it finds issues.
- At the start of a session (or when asked to capture), glance at `inbox/` —
  pending drops mean `tars sweep`, then shelve what landed (concepts + hubs)
  like any capture.
