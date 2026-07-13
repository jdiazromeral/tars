"""Lexical retrieval over the FTS5 index. Vector search slots in here later."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass
class Hit:
    doc_id: str
    connector: str
    origin: str
    title: str | None
    snippet: str
    score: float
    file: str  # raw filename stem — the [[wiki-link]] target for this document
    chunk: str | None = None  # full text of the best-matching chunk (opt-in)


def to_match_expr(query: str) -> str:
    """Sanitize free text into an FTS5 OR-of-terms expression.

    OR, not AND: queries are natural language, and bm25 already ranks docs
    matching more terms higher — one absent word must not zero the results.
    """
    terms = re.findall(r"[\w./-]+", query)
    if not terms:
        raise ValueError("query has no searchable terms")
    return " OR ".join(f'"{term}"' for term in terms)


def search(db: sqlite3.Connection, query: str, k: int = 8,
           connector: str | None = None, raw_match: bool = False,
           with_chunk: bool = False) -> list[Hit]:
    """Ranked documents for `query`. `with_chunk` includes each document's
    best-matching chunk verbatim (~1.6k chars) — enough context to answer from
    without a full `tars show` on the whole document."""
    match = query if raw_match else to_match_expr(query)
    sql = """
        SELECT d.id, d.connector, d.origin, d.title, d.raw_dir, c.text AS chunk_text,
               snippet(chunks_fts, 0, '[', ']', ' … ', 18) AS snip,
               bm25(chunks_fts) AS score
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN documents d ON d.id = c.doc_id
        WHERE chunks_fts MATCH ?
    """
    params: list = [match]
    if connector:
        sql += " AND d.connector = ?"
        params.append(connector)
    sql += " ORDER BY score LIMIT ?"
    params.append(k * 4)  # oversample chunks, then dedupe per document

    hits: list[Hit] = []
    seen: set[str] = set()
    for row in db.execute(sql, params):
        if row["id"] in seen:
            continue
        seen.add(row["id"])
        hits.append(Hit(doc_id=row["id"], connector=row["connector"],
                        origin=row["origin"], title=row["title"],
                        snippet=row["snip"], score=row["score"],
                        file=row["raw_dir"].rsplit("/", 1)[-1].removesuffix(".md"),
                        chunk=row["chunk_text"] if with_chunk else None))
        if len(hits) >= k:
            break
    return hits
