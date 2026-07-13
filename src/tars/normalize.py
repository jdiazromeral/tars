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


def apply(text: str, rules: list[Rule], connector: str | None = None) -> str:
    for rule in rules:
        if rule.connectors is not None and connector not in rule.connectors:
            continue
        text = rule.pattern.sub(lambda _m, c=rule.canonical: c, text)
    return text
