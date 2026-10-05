#!/usr/bin/env python3
"""SessionStart hook: once a day, surface overdue / due-soon TARS tasks.

Shows a short summary to the user (systemMessage) and gives the same text to
the session as context. Fires on the first startup of each day only, and
only when TARS_HOME names the vault; any failure exits 0 silently so a session
never fails to start because of it. Days of staged session logs (see
session_log.py) older than today that end-of-day has not reviewed are named too.
"""
import datetime
import glob
import json
import os
import re

TARS_HOME = os.environ.get("TARS_HOME")
STAMP = os.path.expanduser("~/.claude/state/tars-pending-last")
STAGING = os.path.expanduser(
    os.environ.get("TARS_ACTIVITY_STAGING") or "~/.claude/state/tars-activity"
)
DUE_SOON_DAYS = 2
STALE_DAYS = 30
MAX_LINES = 5


def parse(path):
    text = open(path, encoding="utf-8").read()
    m = re.match(r"---\n(.*?)\n---\n(.*)", text, re.S)
    if not m:
        return None
    fm = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.split("#")[0].strip()
    body = [ln.strip() for ln in m.group(2).splitlines() if ln.strip()]
    action = next((ln for ln in body if not re.match(r"(Source|Concepts|Owner):", ln)), "")
    action = re.sub(r"\[\[[^|\]]+\|([^\]]+)\]\]", r"\1", action)
    return fm, action


def as_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def has_sessions(day_dir):
    """A day is only worth reviewing if something in it was staged."""
    for path in glob.glob(os.path.join(day_dir, "*.json")):
        try:
            with open(path) as f:
                if json.load(f).get("prompts"):
                    return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def short(text, n=110):
    return text if len(text) <= n else text[: n - 1] + "…"


def main():
    if os.environ.get("TARS_EOD_HEADLESS") or not TARS_HOME:
        return
    today = datetime.date.today()
    try:
        if open(STAMP).read().strip() == today.isoformat():
            return
    except OSError:
        pass

    overdue, soon, stale = [], [], 0
    for path in glob.glob(os.path.join(TARS_HOME, "tasks", "2*.md")):
        try:
            parsed = parse(path)
        except (OSError, ValueError):
            continue  # one unreadable task file never silences the whole summary
        if not parsed or parsed[0].get("status") != "open":
            continue
        fm, action = parsed
        due, created = as_date(fm.get("due")), as_date(fm.get("created"))
        who = "" if fm.get("owner", "me") == "me" else f" ({fm['owner']})"
        if due and due < today:
            overdue.append((due, f"{due} — {short(action)}{who}"))
        elif due and (due - today).days <= DUE_SOON_DAYS:
            soon.append((due, f"{due} — {short(action)}{who}"))
        elif not due and created and (today - created).days > STALE_DAYS:
            stale += 1

    unreviewed = sorted(
        os.path.basename(d) for d in glob.glob(os.path.join(STAGING, "2*"))
        if os.path.basename(d) < today.isoformat() and has_sessions(d)
    )

    lines = []
    for title, items in (("Overdue", overdue), ("Due soon", soon)):
        if items:
            lines.append(f"{title} ({len(items)}):")
            lines += [f"  • {line}" for _, line in sorted(items)[:MAX_LINES]]
            if len(items) > MAX_LINES:
                lines.append(f"  • … +{len(items) - MAX_LINES} more")
    if stale:
        lines.append(f"{stale} open tasks older than {STALE_DAYS} days with no due date.")
    if unreviewed:
        lines.append(f"Unreviewed session logs: {', '.join(unreviewed)} → /tars:end-of-day")

    os.makedirs(os.path.dirname(STAMP), exist_ok=True)
    with open(STAMP, "w") as f:
        f.write(today.isoformat())
    if not lines:
        return
    summary = "TARS — " + today.isoformat() + "\n" + "\n".join(lines)
    print(json.dumps({
        "systemMessage": summary,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": summary + "\n(Daily TARS task summary. Mention it only"
            " if the user asks about tasks or their day.)",
        },
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
