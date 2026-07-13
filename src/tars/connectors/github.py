"""GitHub connector: pull requests as documents — the first code connector.

GitHub is reachable with the already-authenticated `gh` CLI, so unlike the
skill-mediated connectors (Granola, Jira via MCP) the whole fetch/assemble/
ingest path is deterministic code; agent judgment (concept shelving, people
backfill) happens after the sync, in the sync-github skill.

What a PR document holds: metadata, the description, and the full review
discussion verbatim, chronological — decisions live in review threads. NOT
the diff: code's source of truth is the repository itself.

Scope is corpus-owned config in `connectors.yml` at the vault root, and it is
a hard requirement — the connector refuses to sweep an unscoped account, so
personal repos never leak into a work vault by default:

    github:
      orgs: [acme]     # sweep only these owners (required, or repos)
      repos: []           # optional extra owner/repo entries
      authors: []         # whose PRs to track; empty = authenticated user
      reviewers: []       # also capture PRs these handles are asked to review
                          # (review-requested: + reviewed-by:); [] = off
      window_days: 30     # first-run backfill window (no watermark yet)
      ignore_authors: []  # extra noise authors; *[bot] handles always skipped
      ticket_prefixes: [] # if set, keep only PRs whose title carries one of
                          # these ticket prefixes (e.g. [PROJ] → PROJ-123);
                          # [] = keep everything. Applies to authored AND
                          # reviewed PRs, so cross-team review noise (other
                          # squads' tickets) is dropped without losing your
                          # own team's PRs that you only reviewed.

PRs are mutable (reviews, comments, merges keep arriving): the cursor is an
`updated` watermark stamped at sweep start and persisted by `tars sync` only
on clean completion, so a failed run simply re-scans — idempotence comes from
the stable origin `github:<owner>/<repo>#<number>` plus content hashing.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import yaml

from .. import ingest, store
from ..store import RawDoc
from . import register

CONFIG_FILE = "connectors.yml"


def _gh_json(*args: str) -> list | dict:
    """Run `gh api` and parse JSON. Module-level so tests can monkeypatch."""
    result = subprocess.run(["gh", "api", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh api {' '.join(args)} failed: {result.stderr.strip()}")
    # --paginate concatenates JSON arrays; wrap and re-split defensively
    text = result.stdout.strip()
    if text.startswith("[") and "]\n[" in text:
        merged: list = []
        for chunk in text.split("]\n["):
            merged.extend(json.loads(f"[{chunk.strip('[]')}]"))
        return merged
    return json.loads(text)


def load_config(root: Path) -> dict:
    path = root / CONFIG_FILE
    data = yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg = (data or {}).get("github") or {}
    if not cfg.get("orgs") and not cfg.get("repos"):
        raise RuntimeError(
            f"github connector needs a scope: set github.orgs (e.g. [acme]) or "
            f"github.repos in {path} — it will not sweep your whole account."
        )
    return cfg


def _search_scope(cfg: dict) -> str:
    parts = [f"org:{o}" for o in cfg.get("orgs") or []]
    parts += [f"repo:{r}" for r in cfg.get("repos") or []]
    return " ".join(parts)


def _iso(ts: str | None) -> str:
    return ts or ""


def _assemble(pr: dict, discussion: list[dict]) -> str:
    repo = pr["base"]["repo"]["full_name"]
    state = "merged" if pr.get("merged_at") else pr["state"]
    lines = [
        f"# {repo.split('/')[1]}#{pr['number']} {pr['title']}",
        "",
        f"- repo: {repo}",
        f"- number: {pr['number']}",
        f"- author: {pr['user']['login']}",
        f"- state: {state}" + (" (draft)" if pr.get("draft") else ""),
        f"- created: {_iso(pr.get('created_at'))}",
        f"- updated: {_iso(pr.get('updated_at'))}",
    ]
    if pr.get("merged_at"):
        lines.append(f"- merged: {pr['merged_at']}")
    lines += [
        f"- branch: {pr['head']['ref']} → {pr['base']['ref']}",
        f"- changes: +{pr.get('additions', 0)} −{pr.get('deletions', 0)} "
        f"across {pr.get('changed_files', 0)} files",
        f"- url: {pr['html_url']}",
        "",
        "## Description",
        "",
        (pr.get("body") or "(no description)").strip(),
    ]
    if discussion:
        lines += ["", "## Discussion", ""]
        for entry in discussion:
            # Bodies are blockquoted so their own markdown (###, ---) can't
            # collide with the document structure.
            quoted = "\n".join(f"> {ln}" for ln in entry["body"].strip().splitlines())
            lines += [f"### {entry['author']} — {entry['when']} — {entry['kind']}", "",
                      quoted, ""]
    return "\n".join(lines).rstrip() + "\n"


def _is_noise(author: str, ignore: set[str]) -> bool:
    """Bots are machine noise, not conversation — CI dumps and token bots
    dwarf the human discussion (a 139KB PR doc was ~90% bot bodies). Skipping
    them makes the capture MORE faithful to what people actually said."""
    return author.endswith("[bot]") or author in ignore


def _fetch_discussion(repo: str, number: int, ignore: set[str]) -> list[dict]:
    """Human issue comments + reviews + inline review comments, chronological."""
    entries: list[dict] = []
    for c in _gh_json(f"repos/{repo}/issues/{number}/comments", "--paginate"):
        if not _is_noise(c["user"]["login"], ignore):
            entries.append({"when": c["created_at"], "author": c["user"]["login"],
                            "kind": "comment", "body": c.get("body") or ""})
    for r in _gh_json(f"repos/{repo}/pulls/{number}/reviews", "--paginate"):
        if _is_noise(r["user"]["login"], ignore):
            continue
        if r.get("body") or r.get("state") not in (None, "COMMENTED"):
            entries.append({"when": r.get("submitted_at") or "", "author": r["user"]["login"],
                            "kind": f"review:{r.get('state', '')}", "body": r.get("body") or ""})
    for c in _gh_json(f"repos/{repo}/pulls/{number}/comments", "--paginate"):
        if not _is_noise(c["user"]["login"], ignore):
            entries.append({"when": c["created_at"], "author": c["user"]["login"],
                            "kind": f"inline {c.get('path', '')}", "body": c.get("body") or ""})
    return sorted(entries, key=lambda e: e["when"])


@register("github")
def sync(root: Path, db, cursor: str | None) -> str:
    cfg = load_config(root)
    authors = cfg.get("authors") or [_gh_json("user")["login"]]
    since = cursor or (
        datetime.now(timezone.utc) - timedelta(days=int(cfg.get("window_days", 30)))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    sweep_start = store.now_iso()
    scope = _search_scope(cfg)
    prefixes = cfg.get("ticket_prefixes") or []
    ticket_re = re.compile(
        r"(?i)(?:" + "|".join(re.escape(p) for p in prefixes) + r")-\d+"
    ) if prefixes else None

    # One sweep per search qualifier. Review involvement needs two: GitHub
    # moves a PR from review-requested: to reviewed-by: the moment the review
    # is submitted, so sweeping both keeps the thread updating across the flip.
    sweeps = [(author, f"author:{author}") for author in authors]
    for reviewer in cfg.get("reviewers") or []:
        sweeps += [(f"review-requested:{reviewer}", f"review-requested:{reviewer}"),
                   (f"reviewed-by:{reviewer}", f"reviewed-by:{reviewer}")]

    seen: dict[str, str] = {}
    for label, qualifier in sweeps:
        hits = _gh_json(
            "-X", "GET", "search/issues",
            "-f", f"q=is:pr {qualifier} {scope} updated:>={since}",
            "-f", "sort=updated", "-f", "order=asc", "-f", "per_page=100",
            "--paginate", "--jq", ".items",
        )
        counts = {"added": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        for hit in hits:
            # Off-prefix PRs are rejected before the expensive PR + discussion
            # fetch, so cross-team review noise costs almost nothing.
            if ticket_re and not ticket_re.search(hit.get("title", "")):
                counts["skipped"] += 1
                continue
            repo = hit["repository_url"].split("/repos/")[-1]
            number = hit["number"]
            origin = f"github:{repo}#{number}"
            if origin in seen:
                continue
            seen[origin] = repo
            pr = _gh_json(f"repos/{repo}/pulls/{number}")
            ignore = set(cfg.get("ignore_authors") or [])
            doc = RawDoc(
                connector="github",
                origin=origin,
                text=_assemble(pr, _fetch_discussion(repo, number, ignore)),
                title=f"{repo.split('/')[1]}#{number} {pr['title']}",
                tags=["github", "pr"],
                meta={"url": pr["html_url"], "author": pr["user"]["login"],
                      "state": "merged" if pr.get("merged_at") else pr["state"]},
            )
            _, status = ingest.add(root, db, doc)
            counts[status] += 1
            click.echo(f"{status}  {doc.id}  {doc.title}")
        summary = (f"github/{label}: {counts['added']} added, {counts['updated']} updated, "
                   f"{counts['unchanged']} unchanged")
        if counts["skipped"]:
            summary += f", {counts['skipped']} off-prefix skipped"
        click.echo(summary)
    return sweep_start
