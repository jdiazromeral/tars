"""The append-only ingestion log (`log/ingestions.jsonl`).

One JSON line per add / update / delete event. It is genuinely new state — raw
frontmatter records only a doc's *latest* `captured_at`, not the sequence of
events — so it is NOT regenerable, and it is tracked in git like the rest of
the vault's truth.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import store


def log_ingestion(root: Path, *, action: str, doc_id: str, connector: str,
                  origin: str, title: str | None = None) -> None:
    """Append one event to the append-only ingestion log. Called for real
    ingestions only (add / update / delete) — never for `unchanged` or for a
    cache rebuild (`reindex` passes `log=False` to `ingest.add`)."""
    log_path = root / store.INGEST_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": store.now_iso(), "action": action, "id": doc_id,
             "connector": connector, "origin": origin, "title": title}
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_log(root: Path) -> list[dict]:
    """Read the ingestion log newest-first. A missing log is an empty history."""
    log_path = root / store.INGEST_LOG
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries.reverse()
    return entries
