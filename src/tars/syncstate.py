"""Sync watermarks: one row per connector key in `sync_state`.

Code connectors (`tars sync`) read the cursor and persist the one they
return. Skill-fed connectors advance it in two phases instead: `begin`
stamps the sweep-start time into a pending slot, `commit` promotes it only
after the sweep ingested everything. A crash in between leaves the live
cursor untouched, so the next sweep re-scans rather than skips.

Keys are free-form: a connector that sweeps several scopes keeps one
watermark per scope under a namespaced key (e.g. `slack/<CHANNEL_ID>`).

Watermarks are stored as UTC ISO timestamps and handed to each source in the
form it reads (`as_form`). The rule behind every conversion: a gap is a
silent loss, an overlap is free (re-ingesting unchanged content is a no-op),
so a conversion may start early, never late.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import datetime, timedelta, timezone

from . import store

FORMS = ("iso", "epoch", "jql")


def as_form(value: str, form: str, lookback_hours: int = 0) -> str:
    """The watermark `value` as a source reads it, optionally `lookback_hours`
    early.

    - iso: as stored (or shifted by the lookback).
    - epoch: Unix seconds, for Gmail `after:` and Slack `oldest=`; keeps
      microseconds when the watermark has them (a Slack resume point).
    - jql: a date a day early, for `updated >= "<date>"`. JQL reads dates in
      the Jira user's time zone, which tars doesn't know; starting a day early
      covers any offset.

    A timestamp without a zone is UTC — read as local time it could land late,
    opening a gap. Raises ValueError for a value that isn't an ISO timestamp
    (only when it has to be parsed: plain iso reads print it as stored).
    """
    if form not in FORMS:
        raise ValueError(f"unknown watermark form {form!r} (one of {', '.join(FORMS)})")
    if form == "iso" and not lookback_hours:
        return value
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO timestamp") from None
    when = (when.replace(tzinfo=timezone.utc) if when.tzinfo is None
            else when.astimezone(timezone.utc)) - timedelta(hours=lookback_hours)
    if form == "epoch":
        seconds = calendar.timegm(when.utctimetuple())  # exact: no float round-trip
        return f"{seconds}.{when.microsecond:06d}" if when.microsecond else str(seconds)
    if form == "jql":
        return (when - timedelta(days=1)).date().isoformat()
    fraction = f".{when.microsecond:06d}" if when.microsecond else ""
    return when.strftime("%Y-%m-%dT%H:%M:%S") + fraction + "Z"


def get_cursor(db: sqlite3.Connection, key: str) -> str | None:
    row = db.execute("SELECT cursor FROM sync_state WHERE connector = ?", (key,)).fetchone()
    return row["cursor"] if row else None


def set_cursor(db: sqlite3.Connection, key: str, value: str) -> None:
    """Make `value` the live watermark and stamp the sync time. Drops any
    pending stamp: a set supersedes an in-flight sweep, so a later `commit`
    can't promote an older sweep-start over it."""
    with db:
        db.execute(
            "INSERT INTO sync_state (connector, cursor, last_sync) VALUES (?, ?, ?) "
            "ON CONFLICT(connector) DO UPDATE SET cursor = excluded.cursor, "
            "last_sync = excluded.last_sync, pending_cursor = NULL",
            (key, value, store.now_iso()),
        )


def clear(db: sqlite3.Connection, key: str) -> None:
    """Forget a watermark entirely (e.g. a Slack resume point once a sweep
    completes)."""
    with db:
        db.execute("DELETE FROM sync_state WHERE connector = ?", (key,))


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
