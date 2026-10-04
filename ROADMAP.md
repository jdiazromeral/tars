# TARS roadmap

The rule that has served this project well: **build on observed need, never on
anticipation**. Every deferred item below has an explicit trigger; if the
trigger hasn't fired, building it early is scope, not progress.

## Now

- ~~**GitHub connector**~~ — shipped, and as the first *code* connector
  (`tars sync github` via the authenticated `gh` CLI; mandatory org/repo
  scope in `connectors.yml`), not skill-mediated as first sketched: the
  fetch needed no judgment. The `tars:sync-github` skill layers shelving and
  `github:` people backfill on top.
- ~~**Raw index (`INDEX.md`)**~~ — shipped 2026-07-10, **removed the same day**.
  It was introduced to save agents tokens, and measured against the corpus it
  doesn't: the only winning access pattern (grep for a title-level lookup,
  ~1 KB) saves ~400 tokens over a plain `tars search` hit, misses anything
  that lives in content rather than titles, and a single accidental whole-file
  read (~10 k tokens at 270 docs, growing linearly) erases ~25 disciplined
  greps. No skill ever consumed it. Verdict per this file's own rule: a view
  nobody's flow needs is scope, not progress. If title-level lookup misses
  start accumulating in `retrieval-misses.md`, that's the trigger to revisit.
- **Ingestion log** — shipped 2026-07-10 and kept: **`log/ingestions.jsonl`**,
  append-only, one line per add/update/delete event (skip unchanged), written
  in `ingest.add` and rendered by `tars log`. Genuinely new state (frontmatter
  only keeps the latest `captured_at`), git-tracked like the rest of the
  vault's truth.
- ~~**Slack channel sweep**~~ — shipped as Mode B of `tars:sync-slack`,
  redesigned from the first (rejected) proposal: the write-time **LLM
  relevance gate is gone**. Scope is a `connectors.yml` channel allowlist
  (the human judgment, in config), selection inside it is deterministic
  structural rules only (threads with replies; standalone messages when they
  carry a human's mark — attachment, reactions, pin, a link to a surface no
  other connector covers, length last as a weak fallback; bot/join noise
  dropped by subtype) — a message qualifies on what is *attached to* it, never
  on what it is *about*, the same species of plumbing as gmail's
  `-category:promotions`, so the "no write-time judgment" thesis holds and
  everything captured is raw. Signals are permissive on purpose: a false
  positive costs bytes FTS never matches, a false negative is permanently
  absent. Two dry runs over real channels calibrated the set — they cut a
  proposed broadcast-mention signal (0 useful hits of 4) and turned links into
  anti-signals when they point at a connector-covered domain, since a "review
  this PR" stub only competes with the fully-ingested PR. They also showed
  signal yield is a property of the channel: sweep where decisions are
  announced, not where work is coordinated. One two-phase
  watermark per channel via namespaced cursor keys (`slack/<CHANNEL_ID>` —
  `sync_state` keys were already free-form). Selection itself is **code**
  (`connectors/slack.py`: a typed `load_config` plus a pure
  `select(messages, cfg)` behind `tars slack select`, covered by
  `tests/test_slack_select.py`) — by this file's own "plumbing is code" rule a
  rule with no judgment in it does not belong in skill prose, where an LLM
  reading English is as deterministic as its mood. Group DMs opt in; 1:1 DMs
  never swept; excluded from `sync-all` by default. Note for the armed
  **secret-filtering** trigger below: this is the first connector that pulls
  whole channels, so the doctor credential-grep check just got closer to
  firing.

  **Stated limitations of the sweep**, not separate roadmap items — a trigger
  this PR fired itself would be exactly the anticipation the rule above
  exists to prevent:
  - *Attachment content is not captured*, only its metadata. An attachment is
    one of the admitting signals, so a message can enter on the strength of an
    artifact the corpus never takes — a pointer with no target, which reads
    like coverage. Whoever closes this decides: identity (source-native file id
    vs the content-addressed `file` connector, so the same document arriving by
    two connectors dedupes); **uneven coverage**, since `extract.py` handles
    pdf/html/text but not images, the commonest chat attachment, leaving
    screenshots admitted-but-unreadable without OCR; thread↔file links, size
    caps, video; and `--meta` on `tars add`, absent today, which is why file
    ids sit in document prose. It also compounds secret filtering: whole
    conversations *plus their files* is how a stray credential or PII export
    reaches the vault.
  - *Replies to threads older than `refresh_days` are only reachable via Mode
    A* — `conversations.history` returns parents at their original `ts`, so the
    refresh pass has to be bounded somewhere.
- **Inbox transport**: `inbox/` + `tars sweep` exist; pick a no-cloud file
  transport for mobile capture (Syncthing/Möbius syncing only `inbox/`) — or
  consciously skip if desktop capture proves sufficient.
- **Backup automation**: adopt `contrib/com.tars.backup.plist` (daily bundle,
  `--keep 14`).

## Next

- **`evaluate` skill**: codify the team-evaluations flow (rubric ingest →
  per-person evidence sweep → cited synthesis) *after* the first manual cycle
  reveals where the friction is. Not before.
- **More connectors on demand** (Sentry, Datadog, Drive, email): each only when
  a real question needs its data. A connector nobody queries is maintenance
  debt.

From Karpathy's LLM Wiki gist and rohitg00's v2 extension (both captured under
[[tars]], 2026-07-10) — TARS already implements the core pattern (raw/wiki/
schema, ingest/query/lint, log, crystallization via the `agent` connector);
the three ideas worth keeping, each behind its trigger:

- **Secret filtering on ingest**: Slack threads and PR bodies land in `raw/`
  verbatim; nothing strips tokens/keys/PII. Cheapest first step is a `doctor`
  check that greps the corpus for credential patterns — measure whether the
  problem exists before building an ingest filter. Trigger: the doctor check
  finds a real hit, or a connector starts pulling from channels where secrets
  plausibly appear.
- **Supersession-lite**: no confidence scores or decay machinery — just a
  convention for marking a claim/note as superseded by a newer doc (a
  `Superseded-by: [[...]]` line, honored by the ask skill). Trigger: the first
  time an `ask` answer cites a decision the corpus already knows is stale.
- **Hybrid search (BM25 + vectors)**: FTS5 misses paraphrases; v2 puts the
  threshold at a few hundred pages and Karpathy's gist points at `qmd` as an
  off-the-shelf hybrid CLI to evaluate. Trigger: title/phrasing misses
  accumulating in `retrieval-misses.md` — the same evidence file that gates
  revisiting the removed raw index.

## Code health — findings from the 2026-10-04 risk review

Four parallel reviewers, one per risk (data integrity, identity, untrusted
input, skills vs CLI), each finding reproduced on a temp vault and re-run
before it landed here. Grouped into themes, ordered by what they can cost.
Design direction agreed with the user: **tars only ever adds** — reads never
change raw, your words are never replaced, and comments on a source are
annotations (new notes that point at it), never edits to it.

- [x] **A. A leading `Concepts:` line was eaten** on the next read-modify-write
  of an unshelved doc (3 of 4 reviewers). Fixed: stripped only when the doc
  is shelved.
- [x] **B. `reindex` rewrote raw and wasn't atomic** — re-normalized text,
  dropped unknown frontmatter keys, committed the DELETE first, and one bad
  file crashed it (and doctor/finalize) midway. Fixed: read-only, one
  transaction, unparseable files skipped and reported (`unparseable-raw`).
- [ ] **C. `capture` overwrites living notes, agent worklogs and excerpts** —
  the skill never mentions `--append`; same-slot re-adds replace silently.
- [ ] **D. Sync windows that lose items** — `sync-granola` advances with
  `--set` even after skipping meetings; the Slack cap keeps the newest
  threads so a backlog never reaches the oldest. Plausible: ISO watermarks
  passed unconverted to JQL / Gmail `after:` / Slack `oldest=`.
- [ ] **E. Re-add drops metadata** — `--append` without `--title` nulls the
  title and wipes tags; a title-only change reports `unchanged`; `--append`
  to a missing slot silently creates a doc.
- [ ] **F. `tag`/`untag`/`normalize` read outside the lock** — a concurrent
  `--append` (track) between their read and write is lost (2 reviewers).
- [ ] **G. Hostile titles** — a newline in a title injects headings/links into
  hubs (growing every `tars hubs`, doctor clean); `promote` writes invalid
  YAML for `: ` titles and can mint a stem that collides with a raw doc.
- [ ] **H. What `rm` doesn't erase** — free pages of tars.db, the log (title,
  origin), hubs, vault git history, backups. `rm` only mentions links.
- [ ] **I. Input edge cases** — `sweep` lossily decodes non-UTF-8 and deletes
  the original; CRLF text gets a hash that never matches (2 reviewers); a
  Slack DM is swept when `--channel-type` is omitted; NFC/NFD Hangul titles
  share one file on APFS; `normalize` rewrites inside URLs.
- [ ] **J. Skills promising what they don't do** — `end-of-day` calls itself
  read-only but runs `sync-all` (which labels Gmail); `gardener` leaves raw
  person links dangling and never finalizes; `digest`/`tasks` disagree on
  the task format; no skill says captured text is data, not instructions.
- [ ] **Usability: commands only accept a doc id.** `show`/`tag`/`untag`/`rm`/
  `promote` (and the planned `annotate`) should take an id, an origin
  (`jira:PROJ-123`) or a file stem / `[[stem]]`, exact match only.
- Low priority, noted: no fsync before rename; hubs written non-atomically;
  one pending-cursor slot shared by overlapping sweeps; URL variants (`/a` vs
  `/a/`, `:443`, query order) mint duplicates; a lost tars.db plus a
  re-titled re-add creates two files with one id, which doctor can't see.

## Code health — findings from the 2026-07-08 full review

Ordered by priority; tick them off as they land. These are observed defects,
not anticipation, so they don't need triggers. A second review pass on
2026-07-10 added the still-open items below and refreshed two counts.

### Bugs

- [x] **Validate `TARS_HOME` before trusting it.** `find_root`
  (`store.py:54`) returns the env path unconditionally. Pointed at a wrong
  existing directory, the missing marker reads as v1 and the error tells you
  to run `tars migrate` — which stamps a `.tars` marker and mints a `tars.db`
  in the wrong directory. Fix: when `TARS_HOME` is set, require the `.tars`
  marker and fail loudly if absent.
- [x] **Make the concurrency story true or stop claiming it.** `db.py:104`
  says concurrent sessions "queue, not crash", but `ingest.add` does a
  read-merge-write of concepts across statements (`ingest.py:59` → `:71`) —
  two sessions tagging the same doc can silently drop a tag. Wrap the
  read-modify-write in `BEGIN IMMEDIATE` (or drop the comment). Fixed
  alongside a second bug the regression test surfaced: `store.write_raw`'s
  plain `write_text()` let a concurrent reader observe a half-written file
  (an actual crash) — now written via temp file + `os.replace`.
- [x] **Test `extract.py` — currently zero coverage on the flakiest module.**
  Only code touching network/trafilatura/pypdf; a dependency bump breaking
  extraction is the most probable future failure and no test would catch it.
  `from_pdf_bytes` and `_from_html` are testable with small fixtures, no
  network. `tests/test_extract.py` covers both with hand-rolled PDF/HTML
  fixtures: text extraction, title resolution (metadata + regex fallback),
  and the empty-content `ExtractionError` paths. `from_url`/`from_file`
  stay untested — they're thin dispatch over these two plus real network
  I/O, not worth mocking `httpx`.
- [x] **The ingestion log isn't crash-consistent with what it logs**
  (observed 2026-07-10). The append ran *after* `db.commit()`, so a crash in
  between silently dropped an event from `log/ingestions.jsonl`, the one record
  `reindex` can't rebuild. Resolved by logging *first*, in both `ingest.add`
  (inside the write lock) and `ingest.remove`. No transaction spans the
  filesystem and SQLite, so a gap remains; it is now the harmless one: an event
  for a write that never landed, never a write with no event. The only readers
  (`digest`, `end-of-day`) look events up for documents already in the vault,
  so a stray event is ignored.

### Design debts

- [x] **`tars doctor` — the missing verification instrument.** The
  architecture bets on agents following prose (sync-jira is 151 lines of
  instructions); nothing checks the vault satisfies its own invariants.
  Doctor should flag: dangling wiki-links, docs shelved under concepts with
  no hub page, DB↔raw drift, tasks pointing at deleted stems. `rm` already
  does a one-off version of the link scan. Higher leverage than any new
  connector or skill. Implemented as `src/tars/doctor.py`: dangling-link
  scan covers tasks pointing at deleted stems too (a task's `Source:` link
  just becomes a dangling link once `tars rm` removes the target) rather
  than needing separate logic. Read-only — reports the fix command
  (`tars reindex` / `tars hubs`), never mutates. `tests/test_doctor.py`.
- [x] **Stop `cli.py` becoming the junk drawer.** Done 2026-10-03. The
  criterion was not line count ("~10-line adapters" would have minted
  one-function modules): logic left the CLI when it encoded an invariant, was
  duplicated, or deserved a unit test of its own. That moved capture identity
  (`ingest.resolve_target`/`note_origin`, previously only in `add` and
  re-derived in `sweep`), watermarks (`syncstate.py`, whose upsert SQL `sync`
  and `cursor` duplicated), redaction (`ingest.remove` +
  `doctor.references_to`, which also fixed `rm` reporting `[[foo-bar]]` as a
  link to `foo`), `inbox.py` and `backup.py`. What stays is argument handling,
  output, and read-only queries (`list`, `status`): 716 → 596 lines, none of
  them a rule the vault depends on.
- [x] **De-duplicate the invariants (currently in triplicate).** The origin
  contract lives in README, AGENTS.md, *and* the `connectors/__init__.py`
  docstring; vocab scoping in AGENTS.md *and* `normalize.py`. Pick one home
  per invariant (contract prose in code docstrings; AGENTS.md links, doesn't
  restate) before the copies drift. The `.claude-plugin/*.json` skill lists are
  a fresh instance of the same class — see Nits — and have *already* drifted.
  Done 2026-10-04 (#9): AGENTS.md owns the contract; README and the package
  docstring point at it; the agy manifest is a symlink.
- [x] **CI + lint + type check.** Type hints everywhere, nothing enforces
  them; a good test suite nothing runs automatically. ruff + pyright +
  a GitHub Actions workflow running `uv run pytest` — an hour of work for a
  repo pitched as shareable.

### Nits

- [ ] `_gh_json`'s `]\n[` pagination re-splitting (`github.py:53`) is
  clever-fragile; `gh api --paginate --slurp` makes it disappear.
- [ ] Slug collision handling (`store.py:175`) tries exactly one id suffix; a
  second collision silently reuses the file. A loop costs two lines.
- [ ] `normalize`'s `\b` boundaries break for variants starting/ending in
  non-word chars ("C++", "K8s?").
- [ ] `search`'s `k * 4` chunk oversample (`search.py:50`) can return fewer
  than `k` docs when one chunky document dominates the ranking.
- [ ] `raw_dir` stores `/`-joined paths and `search.py:61` splits on `/` —
  decide mac-only explicitly or normalize.
- [ ] Version still 0.1.0 across a vault-format major bump; bump alongside
  format changes.
- [x] Plugin manifests hand-list the skills and have drifted (2026-07-10):
  `.claude-plugin/marketplace.json` and `plugin.json` describe 10 skills but
  ship 12 — `sync-github` and `sync-slack` are missing from the descriptions.
  Trim the prose so it doesn't enumerate, or generate the list. Fixed
  2026-07-10: the marketplace description names categories, not skills.
- [ ] `ingest.add` mutates the caller's `RawDoc` in place (`doc.text` at
  `ingest.py:65`, `doc.concepts` at `:78`). Harmless today — no caller reuses
  the object — but a surprising side effect for what reads as an ingest call.

## Deferred — triggers armed

- **Semantic search** (vector rung): trigger = sustained ~20% vocabulary-miss
  rate in `retrieval-misses.md` (the digest counts it). When it fires: local
  multilingual embeddings (bge-m3 class — the corpus is ES/EN), stored in the
  reserved `chunks.embedding` column, always degrading to FTS.
- **Scheduled headless digest**: trigger = the backup launchd job proving the
  unattended pattern for a couple of weeks, and the manual digest rhythm
  feeling like a chore.
- **Enforcing "never hand-edit `raw/`"** (a plugin PreToolUse hook denying
  Edit/Write under `$TARS_HOME/raw/`): trigger = a hand-edit actually
  happening — `finalize` naming a drifted file no crash explains, or a raw
  diff in the vault's git that the CLI didn't make. Until then, detection is
  enough: `doctor` flags the stale hash and `finalize` names the file before
  reindexing it. Skill `allowed-tools` is *not* a substitute: it pre-approves
  tools, it doesn't restrict them; `disallowed-tools` drops whole tools for
  one turn only and can't close Bash.

## Non-goals

- No remote for the vault, ever (bundles are the durability path).
- No write-time summarization pipelines.
- No third-party retrieval services; retrieval stays in-tree and rebuildable.
