# TARS

**TARS Answers from Raw Sources** — a recursive acronym, GNU-style, and the
whole philosophy in four words: a local-first second brain that captures
everything raw, indexes it, and spends the intelligence at **query time**
instead of write time.

> Also answers to *TARS Ain't Rocket Science* (humor setting: 75%).

## Design

Three layers, in order of trust:

| Layer | What | Where | In git |
|---|---|---|---|
| Inbox | Zero-ceremony landing zone — any tool that writes a text file captures; `tars sweep` drains it into raw/ | `inbox/` | yes (transient) |
| Raw archive | Complete, immutable captures with provenance frontmatter, filenames from human titles | `raw/<connector>/<title-slug>.md` | yes |
| Wiki | The curated human layer: concept hubs (what things are), people pages (identity map across tools), promoted notes | `wiki/concepts/`, `wiki/people/`, `wiki/notes/` | yes |
| Tasks | One record per commitment (status/owner/due + provenance links) plus a regenerable open-items index | `tasks/`, `tasks/TASKS.md` | yes |
| Index | SQLite catalog + FTS5 full-text index (an `embedding` column is reserved so vectors can slot in later) | `tars.db` | no — rebuild with `tars reindex` |
| Derived views | Regenerable rollups organized by concept | `digests/` | yes |

There is deliberately **no mandatory summarization pipeline**. Raw captures
stay complete, so a fact that seems irrelevant today can still be found the
day it matters. Synthesis happens when you ask a question; only durable
insights (decisions, rationale, concepts) get promoted into `wiki/notes/`.

Plumbing (fetch, normalize, dedup, chunk, index, sync) is deterministic code
in the `tars` CLI. The model's judgment is reserved for the seams that need
it: extracting from messy sources, and answering questions with citations.

## Install (system-wide)

Three pieces make TARS available everywhere: the **CLI on your PATH**, the
**vault location in `TARS_HOME`**, and the **skills as a Claude Code plugin**.
Set up once, use from any directory — shell or agent session.

### 1. Bootstrap (once, from this repo)

```sh
uv sync
uv tool install --editable .   # installs `tars` into ~/.local/bin — on PATH system-wide
tars init ~/tesseract          # the vault: inbox/, raw/, wiki/, tasks/, digests/, tars.db
(cd ~/tesseract && git init)   # the vault is its own local-only repo (see Privacy)
```

Put the vault **outside this repository**, under **any name and path you
like** — `~/tesseract`, `~/brain`, `~/vaults/work`… nothing in the code knows
the name; the vault is identified by its `.tars` marker and found via
`TARS_HOME`. Keeping it out of the repo means the tool stays purely shareable
code and your data never depends on a `.gitignore` entry to stay private. It's
also relocatable/renamable at any time: move the directory, update
`TARS_HOME`, done (identity lives in the files, not their location).

`--editable` means a `git pull` in this repo updates the installed CLI — no
reinstall needed (a vault *format* bump will tell you to run `tars migrate`).

### 2. Shell, from anywhere

The CLI finds the vault via the `TARS_HOME` environment variable (or by
walking up to a `.tars` marker). Export it in your shell profile:

```sh
# ~/.zshrc
export TARS_HOME="$HOME/tesseract"
alias brain='tars add -'       # optional <5s capture: echo "idea" | brain
```

New shell, then verify from any directory: `tars status`.

### 3. Claude Code, from anywhere (the `tars:` skills)

The agent skills live in `skills/` and ship as a plugin, namespaced
`tars:<skill>` — `tars:capture`, `tars:ask`, `tars:digest`, `tars:end-of-day`,
`tars:tasks`, `tars:promote`, `tars:gardener`, and the `tars:sync-*` connectors
(`ls skills/` is the authoritative list). Installed,
they load in **every** session regardless of directory. Two equivalent ways:

**Interactive** — inside any Claude Code session:

```
/plugin marketplace add <path-to-this-repo | owner/repo>
/plugin install tars@tars
```

**Declarative** — merge into `~/.claude/settings.json` (survives reinstalls,
easy to check into dotfiles):

```json
{
  "env": { "TARS_HOME": "/absolute/path/to/your/vault" },
  "extraKnownMarketplaces": {
    "tars": { "source": { "source": "directory", "path": "/absolute/path/to/this/repo" } }
  },
  "enabledPlugins": { "tars@tars": true }
}
```

The `env` block matters even if your shell profile exports `TARS_HOME`: it
guarantees agent sessions resolve the vault no matter how they were launched
(GUI, headless, cron). **Restart your session** — plugins load at startup —
then from any repo: *"save this"*, *"sync granola"*, *"what do we know
about…"* all hit the same vault.

### Try it

```sh
tars add https://example.com/article
tars add ~/Downloads/paper.pdf
echo "decision: we keep snowflake exports weekly" | tars add -
tars search "snowflake exports" -v   # -v includes each hit's matching chunk
tars show <doc_id> --head 20         # frontmatter + opening; --grep for slices
tars status
```

### Verify it's wired up

From **any** directory — a work repo, your home dir, anywhere — three checks
prove the system-wide plumbing resolves the one vault:

```sh
echo $TARS_HOME    # the vault path (e.g. ~/tesseract) — must be set for shell *and* agent sessions
which tars         # the CLI on PATH (e.g. ~/.local/bin/tars)
tars status        # docs per connector + sync state — proves the vault actually resolves
```

In a Claude Code session, `/plugin` lists `tars@tars` as enabled and the
`tars:` skills fire on the trigger phrases below. If they don't, restart the
session (plugins load at startup) and confirm `enabledPlugins` in
`~/.claude/settings.json`. Once all three pass, TARS is live everywhere — the
repo you happen to be in never matters.

## How to use it

TARS has two interfaces: the **CLI** (deterministic plumbing) and the
**`tars:` skills** (judgment). Day to day you just talk to the agent — "save
this", "what do we know about…", "sync granola" — and the skills call the CLI
for you.

### The daily loop

**1. Capture constantly, with zero ceremony.** Anything worth keeping goes in
the moment you see it — don't decide *why* it matters yet, that's the whole
point of storing raw:

```sh
tars add "https://..."                         # article you want to keep
tars add ~/Downloads/architecture-review.pdf   # a doc someone shared
pbpaste | tars add - --title "slack: why we killed the invitations user"
```

Or in an agent session, just say *"save this"* / paste a URL — the one
`capture` skill runs the same commands, routing by **whose words** it is. If a
page is paywalled or JS-only, the agent fetches it its own way and pipes the
text through `tars add -` with the URL as `--origin`, so provenance survives.

The same skill covers your *own* words — say *"note this down"* and it stores
them verbatim under the `note` connector (pin a stable `note:<slug>` slot and
it becomes a living note you keep adding to) — and syntheses the *agent* just
produced: say *"save the work to tars"* and it authors a work-log under a
separate `agent` connector, so agent-written synthesis never masquerades as a
verbatim capture or as your own words. Agent notes take a stable, mutable slot
too, so a living work-log refreshes in place instead of duplicating. (Both can
later be `promote`d into `wiki/notes/` if a durable insight crystallizes.)

For a fast shell path, alias it: `alias brain='tars add -'` → `echo "idea" | brain`.
And for capture with **no terminal at all** there's `inbox/`: any tool that can
write a text file into that folder is a capture path — an iOS Shortcut via a
folder sync you trust (Syncthing is the no-cloud option), a folder action, a
`cat >>` from a script. `tars sweep` (run it, or let an agent session do it)
drains every drop into `raw/note/` content-addressed, so double-drops never
duplicate; the agent shelves them under concepts afterwards like any capture.

**2. Ask, don't browse.** The corpus is not meant to be read; it's meant to be
queried. In a session: *"what do we know about the snowflake export cadence?"*,
*"why did we remove the invitations user?"*. The `ask` skill searches cheap-first
(`tars search -v --json` returns each hit's best-matching chunk, usually enough
to answer from), escalates per document only when needed (`tars show --grep` /
`--head`; the full document is the last resort, not the default), and answers
**with citations** — doc ids + origins — saying plainly when the corpus doesn't
know. From the shell, `tars search "..."` works too; it's just retrieval
without the synthesis.

Search is accent-insensitive across the mixed Spanish/English corpus
(`reunion` finds *reunión*). When a question only lands after rephrasing,
the agent logs the miss to `retrieval-misses.md` — the file is the evidence
that decides if/when a semantic-search layer earns its complexity.

**3. Promote rarely, deliberately.** When an answer surfaces something durable
— a decision, a rationale, a concept — say *"promote that"*. The agent runs
`tars promote <doc_id> --title "..."`, distills a few sentences into the
created note, and links related notes with `[[wiki-links]]`. `wiki/notes/` is the
only curated surface: keep it small enough that every note earns its place.

### Vault house rules (`AGENTS.md`)

Drop an `AGENTS.md` at your vault root (`$TARS_HOME/AGENTS.md`) to steer the
skills per vault — they read it at the start of a run and let it override their
defaults. Because the vault *is* the context boundary, a home vault and a work
vault behave differently with no machine detection: same skills, different house
rules. Good things to put there: which newsletter senders to digest, the tone
and layout of digests, privacy carve-outs (what never to capture), default
cadence. Keep it short and in prose — it's instructions for a model, not a
config schema.

```markdown
# TARS house rules
profile: work

- Terse digests, decisions-first.
- Never capture personal finance / family threads.
- Newsletter allowlist: alphasignal, pragmatic-engineer, bytebytego.
```

(Not to be confused with this repo's own `AGENTS.md`, which tells agents how to
work on the TARS *codebase*; the vault file steers the skills operating on your
*data*.)

### While you're in another codebase

The `tars:` skills are a global plugin and `tars` is on your PATH, so a session
opened in `webapp`, `partners`, or anywhere hits the same vault `TARS_HOME`
points at — **one brain across every codebase you touch**. The repo you're
standing in never decides where TARS stores anything.

The daily loop above works unchanged here; two moves earn their keep most while
coding:

- **Recall before you re-derive.** Facing an unfamiliar subsystem or a
  half-remembered decision, ask first — *"what do we know about the
  generic-service auth flow?"*. `ask` answers from your PRs, tickets,
  meetings and notes with citations (or says plainly it doesn't know), which
  usually beats redoing the code archaeology.
- **Bank the work when a chunk lands.** *"save the work to tars"* → `capture`
  keeps an agent-authored work-log under a stable slot, so the next session's
  *"save the work"* updates the same log in place instead of duplicating — a
  running record of what you did, queryable later.

Capturing as you go routes by **whose words** it is — all through the one
`capture` skill: an external doc/RFC → *"save this"*; your own realization →
*"note this down"* (the `note` connector); a synthesis the agent authored →
*"save the work"* (the `agent` connector). All land in `raw/`, shelved under
concepts and searchable by `ask`.

### The vault: concepts are the center of gravity

Open the vault directory (the one `TARS_HOME` points at) as an Obsidian
vault. **Concepts** (`wiki/concepts/<slug>.md`)
are human-named hubs — the generic service, a team, a project — each a
short description plus curated `## Notes` and `## Sources` lists. Every
capture is shelved under 1–4 concepts at ingest (agents create concepts
freely; the `gardener` skill periodically merges/renames/prunes with your
approval), so the graph clusters around things you'd actually name, not
around documents.

Raw files are named by their human title
(`raw/granola/2026-07-06-generic-alignment.md`), so every wiki-link and
graph node is readable. Open a concept and see everything it touches; open a
meeting and the backlinks panel shows the concepts it's shelved under and
every digest entry, task, and note that came out of it; open a note and
`Source:` walks you back to the verbatim capture.

### Housekeeping

- `tars status` — corpus overview (docs per connector, notes, sync state).
- `tars hubs` — regenerate every concept page's `## Sources` from shelving
  data. Hub membership is a derived view, not something anyone hand-maintains.
- `tars doctor [--json]` — checks the vault satisfies its own invariants:
  dangling `[[wiki-links]]` (including a task left citing a doc `tars rm`
  deleted), two files sharing a basename across layers (links resolve
  arbitrarily and the graph grows a duplicate node), concepts with shelved
  docs but no hub page, and DB↔raw drift.
  Read-only — reports the fix (`tars reindex` / `tars hubs`), never mutates;
  exits non-zero when it finds issues, so it's scriptable.
- `tars finalize` — the one-step finisher for any sync or edit batch:
  `reindex` (only when it detects drift) → `hubs` → `doctor`. Every connector
  sync closes with it; run it by hand after any change and it re-checks
  invariants, exiting non-zero if something's still off.
- `tars rm <doc_id>` — the redaction path: deletes a capture everywhere (raw
  file, sidecar, index) and reports any wiki-links still pointing at it.
- `tars backup [dir] [--keep N]` — writes a full git bundle of the vault to
  `dir` (or `$TARS_BACKUP_DIR` when omitted; `--keep` prunes to the newest N);
  copy bundles to an encrypted disk. The
  vault never gets a remote, but it must survive a dead laptop. Commit first —
  bundles capture committed history only. Automate it with launchd (macOS):

  ```sh
  # ~/Library/LaunchAgents/com.tars.backup.plist runs this daily at 09:30
  tars backup ~/backups/tars-vault --keep 14
  ```
- `tars migrate` — upgrade an older vault format in place after a tool update
  (the CLI refuses to write mixed formats and will tell you when it's needed).
- The vault is the directory holding the `.tars` marker (e.g. `~/tesseract` —
  open *that* as your Obsidian vault). It lives **outside this repo** and is
  **its own git repo**: commit `raw/` + `wiki/` + `tasks/` there as they grow
  (conventional commits, **no remote — ever**). The DB is gitignored inside
  the vault; `tars reindex` rebuilds it anytime.

## Connectors

Built in: `web` (URLs, incl. PDF links), `file` (pdf/md/txt/html), and `note`
(stdin — the user's own words). Agent-authored notes get their own `agent`
connector, so synthesis stays provenance-separate from verbatim capture — the
`tars:capture` skill routes among all of these by whose words it is. Synced
connectors come in two styles:

- **Code connectors** (for sources reachable with a token or local file):
  register in `src/tars/connectors/` and run via `tars sync <name>` with
  incremental cursors — see the module docstring for the origin contract.
  - `github` — pull requests (metadata + description + full review
    discussion, never the diff) via the authenticated `gh` CLI. **Scope is
    mandatory** and lives in `connectors.yml` at the vault root — it refuses
    to sweep your whole account:

    ```yaml
    github:
      orgs: [acme]     # sweep only these owners
      repos: []           # optional extra owner/repo entries
      authors: []         # whose PRs; empty = the authenticated user
      reviewers: []       # also capture PRs these handles are asked to
                          # review (review-requested: + reviewed-by:)
      window_days: 30     # first-run backfill window
      ticket_prefixes: [] # keep only PRs whose title carries one of these
                          # ticket prefixes (e.g. [PROJ]); [] = keep all.
                          # Applies to reviewed PRs too, so cross-team review
                          # noise drops without losing your own squad's work.
    ```

    Run `tars sync github` (or say "sync github" — the `tars:sync-github`
    skill adds shelving and people backfill on top).
- **Skill-mediated connectors** (when the only channel is an MCP the agent
  holds, e.g. Granola — its local files are encrypted): a skill fetches via
  MCP and pipes each item through `tars add - --connector <name> --origin
  "<name>:<id>"`, with the watermark kept in `tars cursor <name>`. Storage,
  provenance, and idempotence stay in the CLI either way. So far:
  - `sync-granola` — meetings via the Granola MCP ("sync granola").
  - `sync-jira` — issues via the Atlassian Rovo MCP ("sync jira"). Default scope
    is issues assigned to you since the watermark; also pulls concrete keys
    ("ingest PROJ-123") or an epic's children ("sync everything under
    PROJ-100"), which are one-off and leave the watermark alone.
  - `sync-slack` — **threads, never channel sweeps**, via the Slack MCP:
    paste a permalink and say "save this thread". Origin
    `slack:<channel>/<ts>`, so re-capturing a thread refreshes the snapshot
    as replies arrive instead of duplicating. Slack is a firehose;
    deliberate, thread-level capture keeps the corpus high-signal. There is
    no CLI code behind it — the generic `tars add - --connector --origin`
    pipe carries the whole thing.
  - `sync-gmail` — inbox threads via the Gmail MCP ("sync gmail"). Default
    scope is `in:inbox -category:promotions -category:social` since the
    watermark; also pulls a concrete thread list or an ad-hoc search query
    ("ingest that thread with X"), which are one-off and leave the watermark
    alone. Can flag a thread `to_be_deleted` on request — label-only, never
    an actual delete.

**Sync everything in one pass** with the `tars:sync-all` skill ("sync all",
"catch up my brain"): it runs each sweeping connector's own flow in sequence
(gmail, jira, granola, github — Slack stays deliberate), then `tars finalize`
once and an optional digest (the `tars:digest` skill — digests are
agent-authored, there is no CLI command for them). Per-connector judgment is unchanged; the
skill only adds order and a single clean finish.

## Privacy

Two guarantees, and they are **not** the same — don't confuse them:

- **At rest, the corpus stays on your machine.** The vault is a separate,
  local-only git repo *outside this repository* — no git remote, no cloud sync,
  no third-party indexing service. This tool repo tracks no corpus content and
  can be shared. Durability without a remote: `tars backup` bundles to a disk
  you trust.
- **In use, the corpus crosses a trust boundary.** TARS is an LLM agent: every
  `ask`, `digest`, `capture`, and `promote` sends the relevant vault content to
  the model provider (Anthropic), and the `sync-*` connectors route through
  their MCP providers — some Anthropic-hosted (Gmail), some third-party
  (Atlassian Rovo, Slack). "Private" here means **not published or stored
  off-machine** — not **never seen by a third party**. Only ingest what you're
  permitted to send to a hosted LLM.
