"""Token-frugal views over a single raw document (`tars show --head/--grep`).

The full document stays one command away; these carve out just the slice an
agent needs — the frontmatter and opening, or the lines around a match — so
reading a 30k-token transcript is a choice, never the only option.
"""

from __future__ import annotations

import re


def head(text: str, n: int) -> str:
    """First n lines, with a one-line marker when the document continues."""
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[:n]) + f"\n… ({len(lines) - n} more lines)"


def grep(text: str, pattern: str, context: int) -> str:
    """Case-insensitive regex over lines, printed with `context` lines around
    each match, `--` between gaps, and 1-based line numbers (so a follow-up
    `--head` or a wider `-C` can aim at the same spot)."""
    rx = re.compile(pattern, re.IGNORECASE)
    lines = text.splitlines()
    matched = [i for i, line in enumerate(lines) if rx.search(line)]
    if not matched:
        return "no matches"
    keep: set[int] = set()
    for i in matched:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    out: list[str] = []
    prev = None
    for i in sorted(keep):
        if prev is not None and i > prev + 1:
            out.append("--")
        out.append(f"{i + 1}: {lines[i]}")
        prev = i
    return "\n".join(out)
