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


def as_form(iso: str, form: str) -> str:
    """The watermark `iso` as a source reads it.

    - iso: as stored.
    - epoch: Unix seconds, for Gmail `after:` and Slack `oldest=`; keeps
      microseconds when the watermark has them (a Slack resume point).
    - jql: a date a day early, for `updated >= "<date>"`. JQL reads dates in
      the Jira user's time zone, which tars doesn't know; starting a day early
      covers any offset.
    """
    when = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    if form == "iso":
        return iso
    if form == "epoch":
        seconds = calendar.timegm(when.utctimetuple())  # exact: no float round-trip
        return f"{seconds}.{when.microsecond:06d}" if when.microsecond else str(seconds)
    if form == "jql":
        return (when - timedelta(days=1)).date().isoformat()
    raise ValueError(f"unknown watermark form {form!r} (one of {', '.join(FORMS)})")


def from_slack_ts(ts: str) -> str:
    """A Slack message ts (`1791000060.000100`) as an exact ISO watermark."""
    seconds, _, fraction = ts.partition(".")
    base = datetime.fromtimestamp(int(seconds), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    micro = fraction.ljust(6, "0")[:6]
    return f"{base}.{micro}Z" if int(micro or 0) else f"{base}Z"


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
