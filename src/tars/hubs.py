"""Regenerate concept hub `## Sources` sections from shelving data.

Hub membership is derived state: the raw files' `concepts` frontmatter is the
truth, and this module rewrites each hub page's `## Sources` list to match —
exactly the shelved documents, nothing else. It never depends on an agent
remembering to append a line at capture time.

Hand-written content is preserved where it can be: everything outside the
`## Sources` section is untouched, and an entry's trailing "— relevance"
clause survives as long as the document stays shelved under that concept.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import store

_SOURCES_RE = re.compile(r"(^## Sources[ \t]*$)(.*?)(?=^## |\Z)", re.M | re.S)
_ENTRY_RE = re.compile(r"^- \[\[([^\]|]+)(?:\|[^\]]*)?\]\]", re.M)


def _entry_line(stem: str, title: str | None, origin: str) -> str:
    return f"- [[{stem}|{title or origin}]]"


def regenerate(root: Path, db: sqlite3.Connection) -> tuple[int, int]:
    """Rewrite every concept page's Sources section. Returns (pages written,
    pages created). Pages for concepts with no shelved docs get an empty
    section — pruning the page itself is the gardener's call, not ours."""
    shelved: dict[str, list[sqlite3.Row]] = {}
    for row in db.execute(
        "SELECT title, origin, raw_dir, concepts FROM documents "
        "ORDER BY captured_at, raw_dir"
    ):
        for slug in json.loads(row["concepts"] or "[]"):
            shelved.setdefault(slug, []).append(row)

    concepts_dir = root / store.CONCEPTS_DIR
    existing_pages = {p.stem for p in concepts_dir.glob("*.md")}
    written = created = 0

    for slug in sorted(set(shelved) | existing_pages):
        page = concepts_dir / f"{slug}.md"
        docs = shelved.get(slug, [])
        if page.exists():
            text = page.read_text()
        else:
            # Minimal skeleton — the description is the agent's/user's to write.
            text = f"# {slug.replace('-', ' ')}\n\n## Notes\n\n## Sources\n"
            created += 1

        match = _SOURCES_RE.search(text)
        old_section = match.group(2) if match else ""
        # Keep a hand-written relevance clause for entries that stay shelved.
        kept_lines = {m.group(1): line for line in old_section.splitlines()
                      if (m := _ENTRY_RE.match(line))}
        lines = []
        for row in docs:
            stem = Path(row["raw_dir"]).stem
            lines.append(kept_lines.get(stem) or _entry_line(stem, row["title"], row["origin"]))
        section = "\n\n" + "\n".join(lines) + "\n\n" if lines else "\n\n"

        if match:
            new_text = text[:match.start(2)] + section + text[match.end(2):]
        else:  # page without a Sources heading yet — append one
            new_text = text.rstrip("\n") + "\n\n## Sources" + section
        new_text = new_text.rstrip("\n") + "\n"

        if not page.exists() or new_text != page.read_text():
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(new_text)
            written += 1
    return written, created
