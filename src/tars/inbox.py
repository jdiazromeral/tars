"""inbox/: the zero-ceremony landing zone. Anything that can write a text file
there (a phone folder-sync, a folder action, `cat >>`) is a capture path;
`sweep` drains it into raw/note/.

Sweep is plumbing: origins are content-addressed so the same drop never
duplicates, and shelving stays the agent's job afterwards (`tars tag` + hubs).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import ingest, store
from .store import RawDoc

TEXT_SUFFIXES = {".md", ".txt", ""}
GENERIC_STEMS = {"note", "new note", "untitled"}


@dataclass
class Drop:
    """What sweep did with one inbox file."""
    file: str
    status: str  # added | updated | unchanged | skipped (not text) | empty (cleared)
    #             | kept (couldn't ingest; left in inbox/, reason in title)
    doc_id: str | None = None
    title: str | None = None


def title_for(path: Path, text: str) -> str:
    """Filename wins when it's meaningful; date-ish or generic names fall back
    to the note's first line."""
    stem = path.stem.strip()
    if re.fullmatch(r"[\d\-_. ]*", stem) or stem.lower() in GENERIC_STEMS:
        first = text.lstrip().splitlines()[0].lstrip("# ").strip()
        return first[:60] or stem or "inbox note"
    return stem.replace("-", " ").replace("_", " ")


def sweep(root: Path, db: sqlite3.Connection) -> list[Drop]:
    """Ingest every plain-text file in inbox/ as a note and delete it.

    Dotfiles are ignored; non-text files are left in place and reported as
    skipped; empty files are deleted without ingesting anything.
    """
    inbox = root / store.INBOX_DIR
    inbox.mkdir(exist_ok=True)
    drops = []
    for f in sorted(p for p in inbox.iterdir() if p.is_file()):
        if f.name.startswith("."):
            continue
        if f.suffix.lower() not in TEXT_SUFFIXES:
            drops.append(Drop(f.name, "skipped"))
            continue
        text = f.read_text(errors="replace").strip()
        if not text:
            f.unlink()
            drops.append(Drop(f.name, "empty"))
            continue
        doc = RawDoc(
            connector="note",
            origin=ingest.note_origin(text),
            text=text,
            title=title_for(f, text),
            meta={"source": "inbox", "inbox_file": f.name},
        )
        try:
            doc_id, status = ingest.add(root, db, doc)
        except ingest.WouldReplace as exc:  # leave it in inbox/: never unlink what wasn't ingested
            drops.append(Drop(f.name, "kept", title=str(exc)))
            continue
        except store.UnparseableRaw as exc:
            drops.append(Drop(f.name, "kept", title=f"{exc.path.relative_to(root)}: {exc.reason}"))
            continue
        f.unlink()
        drops.append(Drop(f.name, status, doc_id, doc.title))
    return drops
