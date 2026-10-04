"""Canonicalization of known capture-tool (STT) transcription errors.

Driven by a corpus-owned `vocab.yml` at the TARS root. A speech-to-text
mishearing ("AcneCloud" for "AcmeCloud") is capture noise, not content, so
normalizing it makes the archive *more* faithful to what was actually said.

This is a narrow, deliberate exception to verbatim-raw: it is deterministic,
auditable (the map is the record of every correction), and re-runnable, and it
runs inside the ingest pipeline so it also survives re-sync. It is never a
license to hand-edit raw files.

vocab.yml format — canonical form → variants, optionally scoped to connectors:

    AcmeCloud:
      variants: [AcneCloud, Acnecloud, Acneson]
      connectors: [granola]        # only STT sources; omit = every connector
    Acme: [Acne]                 # legacy flat form = unscoped (all connectors)

Scoping matters: "Acne" is a real word, and a web article about fiber optics
captured faithfully must NOT be rewritten to fix a Granola hearing problem.
STT corrections are only "more faithful than raw" for STT sources — scope
them there.

Variants match case-insensitively at word boundaries and are replaced with the
canonical form verbatim.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple

import yaml

VOCAB_FILE = "vocab.yml"


class Rule(NamedTuple):
    pattern: re.Pattern
    canonical: str
    connectors: frozenset[str] | None  # None = applies to every connector


def load_rules(root: Path) -> list[Rule]:
    path = root / VOCAB_FILE
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    rules: list[Rule] = []
    for canonical, spec in data.items():
        if isinstance(spec, dict):
            variants = spec.get("variants") or []
            connectors = frozenset(spec["connectors"]) if spec.get("connectors") else None
        else:  # legacy flat form: canonical: [variants]
            variants, connectors = spec or [], None
        for variant in variants:
            rules.append(Rule(
                re.compile(rf"\b{re.escape(str(variant))}\b", re.IGNORECASE),
                str(canonical), connectors))
    # Longer variants first so a shorter one can't shadow a longer match.
    rules.sort(key=lambda r: -len(r.pattern.pattern))
    return rules


# Addresses, not prose: a URL, a mailto:/www. link, or an email address. A
# link ends at whitespace or at the delimiters that wrap one in markdown or
# Slack (<url|label>, [label](url)).
_ADDRESS = re.compile(r"(?:https?://|mailto:|www\.)[^\s<>|)\]]+"
                      r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def apply(text: str, rules: list[Rule], connector: str | None = None) -> str:
    """Rewrite STT variants to their canonical form — outside addresses only: a
    variant inside a link's host or path, or an email, is an address, not a
    mishearing. Matched against the whole text, so word boundaries next to an
    address mean what they always did."""
    for rule in rules:
        if rule.connectors is not None and connector not in rule.connectors:
            continue
        spans = [m.span() for m in _ADDRESS.finditer(text)]

        def replace(m: re.Match, canonical: str = rule.canonical) -> str:
            inside = any(start <= m.start() < end for start, end in spans)
            return m.group(0) if inside else canonical

        text = rule.pattern.sub(replace, text)
    return text
