---
name: sync-all
description: Sync every configured source into TARS in one pass, then finalize and (optionally) digest. Trigger on "sync all", "sync everything", "pull everything into tars", "catch up my brain / second brain". Runs each sweeping connector's own sync flow (gmail, jira, granola, github) in sequence — Slack is excluded (deliberate, thread-level) — then `tars finalize` once. Judgment (concepts, people) stays per-connector; this skill is only the conductor.
---

# Sync all → TARS

> **Vault house rules.** Before acting, read `$TARS_HOME/AGENTS.md` if it exists
> and honor it — source allowlists, cadence, and privacy carve-outs there
> override this skill's defaults on conflict.

One invocation, every source. This skill is only the conductor: each connector
still runs its own `sync-<connector>` skill (with that connector's concept and
people judgment), and the CLI still owns storage. What this adds is order, a
single clean finish, and an optional catch-up digest.

## Scope

Default set — the connectors that *sweep* on a watermark:

- **gmail** (`sync-gmail`) — incremental inbox sweep
- **jira** (`sync-jira`) — issues assigned since the watermark
- **granola** (`sync-granola`) — recent meetings
- **github** (`sync-github`) — PRs per `connectors.yml`

**Slack is excluded by default** — deliberate Mode A thread capture remains
the default Slack posture. Its Mode B channel sweep joins the pass only when
asked explicitly ("sync all including slack") and only over the
`connectors.yml` slack allowlist. Narrow or widen on request ("sync all
except gmail", "just jira and github"). Read `connectors.yml` for the
code-connector scope (orgs / repos / authors).

## Steps

1. **Pick the set** from the request + `connectors.yml`, and announce what you
   will run so the user can veto before the work starts.

2. **Run each connector in sequence**, following its own `sync-<connector>`
   skill end to end (fetch → shelve concepts → wire people → label / commit
   cursor). Keep context lean: run the heavy MCP-mediated sweeps (gmail, jira,
   granola) as a **subagent per connector** and collect its report; github is a
   CLI fetch (`tars sync github`) plus light judgment.

   - **A connector whose MCP isn't authorized** (the claude.ai Gmail / Granola
     connectors can be absent in headless or cron runs) → skip it, note it, and
     keep going. Never fail the whole pass because one source is unavailable.
   - Each connector's own two-phase cursor (`--begin` / `--commit`) still guards
     its watermark, so a connector that errors leaves its cursor uncommitted and
     the others are unaffected.

3. **Finalize once** — after every connector finishes, run `tars finalize`
   (regenerate hubs, clear any index drift, re-check invariants). Each sync
   already finalizes itself, so this is the cheap global confirmation that the
   whole pass left the vault consistent; it exits non-zero if anything remains.

4. **Digest (default yes)** — run the `digest` skill to summarize everything
   that entered since the last digest, by concept, with open questions and
   extracted commitments. Skip on request.

5. **Report**: per-connector added / updated / skipped counts and each new
   watermark, any connector skipped for auth or availability, concepts created
   vs reused across the whole pass, people pages touched, and the final
   `doctor` verdict.

## Notes

- **Judgment stays per-connector.** This skill never centralizes concept or
  people decisions — each `sync-<connector>` skill owns those for its own
  sources, so the taxonomy stays coherent.
- **Idempotent.** Origins dedup, so a re-run only picks up what is new and a
  half-finished pass is safe to resume.
- **Not a scheduler.** For a hands-off cadence, wrap this in a scheduled agent —
  but the claude.ai-MCP connectors (gmail, granola) may not authenticate in an
  unattended run, so a schedule reliably covers the code connectors (github,
  jira) and the MCP-only ones may still need an attended pass.
