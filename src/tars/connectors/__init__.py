"""Connector registry for synced sources (Jira, GitHub, Google Workspace, Granola,
Sentry, Datadog, Slack...).

A connector is a callable `sync(root, db, cursor) -> new_cursor` that fetches
everything changed since `cursor`, feeds each item through `ingest.add()` (which
makes re-runs idempotent), and returns the new cursor to persist in `sync_state`.

Register with:

    from tars.connectors import register

    @register("jira")
    def sync(root, db, cursor):
        ...
        return new_cursor

## Origin contract (every connector, code- or skill-mediated)

Identity is `doc_id = sha256(f"{connector}:{origin}")`, and idempotence hangs off
`(connector, origin)`. So a connector's `origin` MUST be a **stable, source-native
id** — the thing that names the same item forever, regardless of when or from
where it's fetched:

    granola:<meeting_id>   jira:<ISSUE-KEY>   github:<owner>/<repo>#<n>
    slack:<channel>/<ts>   confluence:<page_id>

Never key on a wall-clock timestamp, a row number, or a local filesystem path —
those make re-capture mint duplicates. Manual connectors follow the same rule with
what's natural: `web` uses the URL; `note` (`note:<hash>`, the user's own words)
and `file` (`file:<hash>` over the raw bytes) are content-addressed so
re-capturing the same text/document dedupes across paths and machines — the path
lives in `meta` as provenance, not identity. Agent-authored notes take the
`agent` connector, keyed by a mutable `agent:<slug>` slot (or `agent:<hash>` when
frozen), keeping synthesis provenance-separate from what the user said or read. Correctness comes from `origin` (identity) plus
`documents.content_hash` (change detection); the cursor below is only a fetch
optimization and is never load-bearing for dedup.

## Two kinds of cursor — pick by the source, not the transport

- **Append-only / immutable** (log lines, chat messages): the native id is itself
  monotonic, so the cursor *is* `max(origin id)` — one concept, no separate
  watermark. An item, once seen, never changes.
- **Mutable** (Jira issues, Confluence pages): items keep changing after creation
  (new comments, status/state transitions), so the id names identity while the
  cursor is a separate `updated`-watermark for *discovering* changes in scope.
  Two consequences a scalar watermark can't cover, and connectors should offer a
  path for: (1) **refresh** — an item ingested out of the incremental scope (e.g.
  an ad-hoc pull) goes stale and must be re-fetched by id to capture its current
  state; re-ingest is a no-op when unchanged, an upsert when not. (2) **scope
  exit / deletion** — an item reassigned away or deleted simply stops appearing;
  reconciling the ingested id-set (see `tars list`) against what's in scope is how
  you'd notice, and a missing item is flagged, never silently deleted.
"""

from __future__ import annotations

from typing import Callable

CONNECTORS: dict[str, Callable] = {}


def register(name: str):
    def decorator(fn: Callable) -> Callable:
        CONNECTORS[name] = fn
        return fn
    return decorator


from . import github  # noqa: E402,F401  (imports register the connector)
