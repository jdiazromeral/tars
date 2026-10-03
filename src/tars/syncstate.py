"""Sync watermarks: one row per connector key in `sync_state`.

Code connectors (`tars sync`) read the cursor and persist the one they
return. Skill-fed connectors advance it in two phases instead: `begin`
stamps the sweep-start time into a pending slot, `commit` promotes it only
after the sweep ingested everything. A crash in between leaves the live
cursor untouched, so the next sweep re-scans rather than skips.

Keys are free-form: a connector that sweeps several scopes keeps one
watermark per scope under a namespaced key (e.g. `slack/<CHANNEL_ID>`).
"""

from __future__ import annotations

import sqlite3

from . import store


def get_cursor(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT cursor FROM sync_state WHERE connector = ?", (key,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(db: sqlite3.Connection, key: str, value: str) -> None:
    """Make `value` the live watermark and stamp the sync time."""
    with db:
        db.execute(
            "INSERT INTO sync_state (connector, cursor, last_sync) VALUES (?, ?, ?) "
            "ON CONFLICT(connector) DO UPDATE SET cursor = excluded.cursor, "
            "last_sync = excluded.last_sync",
            (key, value, store.now_iso()),
        )


def begin(db: sqlite3.Connection, key: str) -> str:
    """Stamp now() into the pending slot and return it; the live cursor is untouched."""
    stamp = store.now_iso()
    with db:
        db.execute(
            "INSERT INTO sync_state (connector, pending_cursor) VALUES (?, ?) "
            "ON CONFLICT(connector) DO UPDATE SET pending_cursor = excluded.pending_cursor",
            (key, stamp),
        )
    return stamp


def commit(db: sqlite3.Connection, key: str) -> str:
    """Promote the pending watermark to live and return it.

    Raises LookupError when there is no pending watermark to promote.
    """
    row = db.execute(
        "SELECT pending_cursor FROM sync_state WHERE connector = ?", (key,)
    ).fetchone()
    if not row or not row["pending_cursor"]:
        raise LookupError(f"no pending watermark for {key}")
    pending = row["pending_cursor"]
    with db:
        db.execute(
            "UPDATE sync_state SET cursor = ?, pending_cursor = NULL, last_sync = ? "
            "WHERE connector = ?",
            (pending, store.now_iso(), key),
        )
    return pending
