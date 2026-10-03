"""Off-machine durability for a vault that never gets a remote: full git
bundles written to a directory the user trusts (an encrypted disk, private
storage). Restore with `git clone <bundle> <vault-dir>`.
"""

from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

BUNDLE_GLOB = "tars-vault-*.bundle"


class BackupError(Exception):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def has_uncommitted_changes(root: Path) -> bool:
    """Bundles carry committed history only; anything this flags is left out."""
    return bool(_git(root, "status", "--porcelain").stdout.strip())


def create_bundle(root: Path, dest: Path) -> Path:
    """Write a timestamped bundle of every ref in the vault repo into `dest`."""
    if not (root / ".git").exists():
        raise BackupError(f"vault at {root} is not a git repo — `git init` and commit it first")
    dest.mkdir(parents=True, exist_ok=True)
    bundle = dest / f"tars-vault-{datetime.now().strftime('%Y%m%d-%H%M%S')}.bundle"
    result = _git(root, "bundle", "create", str(bundle), "--all")
    if result.returncode != 0:
        raise BackupError(f"git bundle failed: {result.stderr.strip()}")
    return bundle


def prune(dest: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` bundles in `dest`; returns what was removed.
    Timestamped names sort chronologically, so name order is age order."""
    old = sorted(dest.glob(BUNDLE_GLOB))[:-keep]
    for path in old:
        path.unlink()
    return old
