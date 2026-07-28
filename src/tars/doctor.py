"""Vault invariant checks — nothing else verifies the vault satisfies its own
contract, so drift (a hand-deleted raw file, a link left dangling after
`tars rm`, a concept nobody ran `tars hubs` for) goes unnoticed until a
search or a backlink quietly fails.

Read-only by design: fixes are either mechanical (`tars reindex`, `tars
hubs`) or a judgment call (which concept a dangling link should have
pointed at) — doctor names the problem and the fix, it doesn't guess.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import store

# Same shape as the [[stem]] / [[stem|label]] links used everywhere in the
# vault (store.py's Concepts: line, hubs.py's Sources entries) — plus
# [[stem\|label]], Obsidian's escaped-pipe form required inside Markdown
# tables (the roadmap hubs use it); the target must not swallow the escape.
_LINK_RE = re.compile(r"\[\[([^\]|\\]+)(?:\\?\|[^\]]*)?\]\]")

# Layers that hold hand-or-agent-authored links worth checking. raw/ is
# excluded on purpose: captured third-party text can contain literal
# "[[...]]" that isn't a wiki-link, and would false-positive.
_LINKED_LAYERS = (store.CONCEPTS_DIR, store.PEOPLE_DIR, store.NOTES_DIR,
                   store.TASKS_DIR, store.DIGESTS_DIR)


@dataclass
class Finding:
    check: str
    path: str
    detail: str


def _valid_link_targets(root: Path) -> set[str]:
    return {f.stem for f in store.linkable_files(root)}


def dangling_links(root: Path) -> list[Finding]:
    targets = _valid_link_targets(root)
    findings = []
    for layer in _LINKED_LAYERS:
        for md in sorted((root / layer).glob("*.md")):
            for match in _LINK_RE.finditer(md.read_text()):
                stem = match.group(1).strip()
                if stem not in targets:
                    findings.append(Finding(
                        "dangling-link", str(md.relative_to(root)),
                        f"[[{stem}]] has no matching file",
                    ))
    return findings


def ambiguous_stems(root: Path) -> list[Finding]:
    """Two files sharing a basename across layers. `dangling_links` can't see
    this — it folds stems into a set, so a collision looks like one valid
    target — yet every [[stem]] link to the pair resolves arbitrarily and the
    graph grows a duplicate node. Which file keeps the plain name is a
    judgment call, so doctor names the clash and leaves the rename alone."""
    owners: dict[str, list[str]] = {}
    for path in store.linkable_files(root):
        owners.setdefault(path.stem, []).append(str(path.relative_to(root)))
    findings = []
    for stem, paths in sorted(owners.items()):
        if len(paths) > 1:
            first, *rest = sorted(paths)
            findings.append(Finding(
                "ambiguous-stem", first,
                f"[[{stem}]] also matches {', '.join(rest)} — links to it resolve "
                "arbitrarily; rename one, then `tars reindex && tars hubs`",
            ))
    return findings


def unhubbed_concepts(root: Path, db: sqlite3.Connection) -> list[Finding]:
    existing = {p.stem for p in (root / store.CONCEPTS_DIR).glob("*.md")}
    shelved: set[str] = set()
    for row in db.execute("SELECT concepts FROM documents"):
        shelved.update(json.loads(row["concepts"] or "[]"))
    return [
        Finding("unhubbed-concept", f"{store.CONCEPTS_DIR}/{slug}.md",
                f"concept '{slug}' has shelved docs but no hub page — run `tars hubs`")
        for slug in sorted(shelved - existing)
    ]


def db_drift(root: Path, db: sqlite3.Connection) -> list[Finding]:
    raw_docs = {}
    for content_md in store.iter_raw(root):
        doc = store.read_raw(content_md)
        raw_docs[doc.id] = (content_md, doc)

    db_rows = {row["id"]: row for row in
               db.execute("SELECT id, raw_dir, content_hash FROM documents")}

    findings = []
    for doc_id, (path, doc) in raw_docs.items():
        rel = str(path.relative_to(root))
        row = db_rows.get(doc_id)
        if row is None:
            findings.append(Finding("db-drift", rel, "raw file not in index — run `tars reindex`"))
            continue
        if row["raw_dir"] != rel:
            findings.append(Finding(
                "db-drift", rel, f"index points at {row['raw_dir']} instead — run `tars reindex`"))
        if row["content_hash"] != store.content_hash(doc.text):
            findings.append(Finding("db-drift", rel, "content hash stale — run `tars reindex`"))

    for doc_id, row in db_rows.items():
        if doc_id not in raw_docs:
            findings.append(Finding(
                "db-drift", row["raw_dir"], "indexed but raw file missing — run `tars reindex`"))
    return findings


def run(root: Path, db: sqlite3.Connection) -> list[Finding]:
    return [*dangling_links(root), *ambiguous_stems(root),
            *unhubbed_concepts(root, db), *db_drift(root, db)]
