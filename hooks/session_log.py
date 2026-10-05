#!/usr/bin/env python3
"""Stop hook: stage evidence of what this session worked on, for end-of-day review.

Layout under ${TARS_ACTIVITY_STAGING:-~/.claude/state/tars-activity}/:

  <YYYY-MM-DD>/<session_id>.json   the day's evidence: repo, branch, tickets, prompts
  sessions/<session_id>.json       how far into the transcript this session has been read
  reviewed/<YYYY-MM-DD>/           days /tars:end-of-day resolved (it moves them there)

Evidence only; the activity record itself is written by /tars:end-of-day after
the user confirms. The per-session offset is what every day continues from, so a
session that runs past midnight or past a review never stages a prompt twice; a
session that runs end-of-day stages nothing more that day (the review is not work).
Never blocks, never prints: any failure exits 0 silently.
"""

import datetime
import fcntl
import json
import os
import re
import subprocess
import sys

STAGING = os.path.expanduser(
    os.environ.get("TARS_ACTIVITY_STAGING") or "~/.claude/state/tars-activity"
)
TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
# Branches and worktree dirs are usually lowercase: feat/deseo-1343-redirect.
TICKET_ANY_CASE = re.compile(TICKET.pattern, re.IGNORECASE)
NOT_TICKETS = {"UTF", "ISO", "SHA", "RFC", "CVE", "GPT", "AES", "HTTP"}
# Session plumbing, not work: never worth staging as a prompt.
BUILTIN_COMMANDS = {
    "/exit", "/clear", "/compact", "/hooks", "/config", "/model", "/resume",
    "/status", "/cost", "/help", "/login", "/logout", "/effort", "/fast",
}
REVIEW_SKILLS = {"tars:end-of-day", "end-of-day"}
# Context the client prepends to what was typed: <ide_opened_file>…</ide_opened_file> etc.
LEADING_TAGS = re.compile(r"^\s*(?:<([\w-]+)[^>]*>.*?</\1>\s*)+", re.S)
MAX_PROMPTS = 5
PROMPT_CHARS = 200


def git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def tickets_in(text, pattern=TICKET):
    keys = {t.upper() for t in pattern.findall(text or "")}
    return {t for t in keys if t.split("-")[0] not in NOT_TICKETS}


def typed(text):
    """What the user typed in one text block, without the client's leading context tags."""
    text = LEADING_TAGS.sub("", text).strip()
    return None if not text or text.startswith("<") else text


def user_text(entry):
    """Human-typed text of a transcript user entry, or None for tool results/meta."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        blocks = [content]
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        blocks = [b.get("text", "") for b in content if isinstance(b, dict)]
    else:
        return None
    for block in blocks:
        cmd = re.search(r"<command-name>([^<]+)</command-name>", block)
        if cmd:
            if cmd.group(1).strip() in BUILTIN_COMMANDS:
                return None
            args = re.search(r"<command-args>([^<]*)</command-args>", block)
            return (cmd.group(1) + " " + (args.group(1) if args else "")).strip()
    texts = [t for t in map(typed, blocks) if t]
    return " ".join(texts) or None


def runs_review(entry, text):
    """True when this entry starts /tars:end-of-day — typed, or invoked as a skill."""
    if text and text.split()[0].lstrip("/") in REVIEW_SKILLS:
        return True
    if entry.get("type") != "assistant":
        return False
    content = (entry.get("message") or {}).get("content")
    return isinstance(content, list) and any(
        isinstance(b, dict)
        and b.get("type") == "tool_use"
        and b.get("name") == "Skill"
        and (b.get("input") or {}).get("skill") in REVIEW_SKILLS
        for b in content
    )


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def read_new(tpath, offset):
    """Complete transcript lines past offset → (new offset, [(entry, user text)])."""
    with open(tpath, "rb") as f:
        f.seek(offset)
        chunk = f.read()
    complete = chunk[: chunk.rfind(b"\n") + 1]
    entries = []
    for line in complete.decode("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append((entry, user_text(entry)))
    return offset + len(complete), entries


def main():
    if os.environ.get("TARS_EOD_HEADLESS"):
        return
    hook = json.load(sys.stdin)
    sid = hook.get("session_id")
    tpath = hook.get("transcript_path")
    cwd = hook.get("cwd") or os.getcwd()
    if not sid or not isinstance(tpath, str) or not os.path.exists(tpath):
        return
    now = datetime.datetime.now().astimezone()
    day = now.strftime("%Y-%m-%d")

    sessions = os.path.join(STAGING, "sessions")
    os.makedirs(sessions, exist_ok=True)
    # One lock per session: overlapping async Stops for it run one after the other.
    with open(os.path.join(sessions, sid + ".json"), "a+") as index_file:
        fcntl.flock(index_file, fcntl.LOCK_EX)
        index_file.seek(0)
        try:
            index = json.loads(index_file.read() or "{}")
        except ValueError:
            index = {}
        if "offset" not in index:
            # Staged before the per-session index existed: continue from the day record.
            old = load(os.path.join(STAGING, day, sid + ".json")) or {}
            index = {"transcript_path": old.get("transcript_path"),
                     "offset": old.get("transcript_offset", 0)}
        # A different transcript is read from its start, never from another file's offset.
        offset = index.get("offset", 0) if index.get("transcript_path") == tpath else 0
        offset, entries = read_new(tpath, offset)
        if any(runs_review(entry, text) for entry, text in entries):
            index["review_day"] = day
        # The review conversation is not work: once a session runs end-of-day, the rest
        # of its day stays unstaged — so a review never leaves a fresh day behind.
        if index.get("review_day") != day:
            stage(sid, cwd, day, now, [t for _, t in entries if t])
        index.update(transcript_path=tpath, offset=offset)
        index_file.seek(0)
        index_file.truncate()
        json.dump(index, index_file)


def stage(sid, cwd, day, now, new_prompts):
    path = os.path.join(STAGING, day, sid + ".json")
    rec = load(path) or {}
    # Whatever end-of-day already resolved stays resolved, even if a write raced its move.
    done = load(os.path.join(STAGING, "reviewed", day, sid + ".json")) or {}
    done_prompts = set(done.get("prompts", []))
    prompts = []
    for p in rec.get("prompts", []) + [p[:PROMPT_CHARS] for p in new_prompts]:
        if p not in done_prompts and p not in prompts:
            prompts.append(p)
    if not prompts:
        return  # nothing typed that isn't already reviewed: no record, no empty day dir

    top = git(cwd, "rev-parse", "--show-toplevel")
    # --git-common-dir resolves a worktree back to its main repo's .git
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main_root = os.path.dirname(common) if common else ""
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    worktree = os.path.basename(top) if top and main_root and main_root != top else None
    loc = {
        "cwd": cwd,
        "repo": os.path.basename(main_root) if main_root else None,
        "worktree": worktree,
        "branch": branch or None,
    }
    locs = rec.get("locations", [])
    if loc not in locs:
        locs.append(loc)

    tickets = set(rec.get("tickets", [])) - set(done.get("tickets", []))
    tickets |= tickets_in(branch, TICKET_ANY_CASE) | tickets_in(worktree, TICKET_ANY_CASE)
    tickets |= tickets_in(cwd)
    for p in new_prompts:
        tickets |= tickets_in(p)

    rec.setdefault("session_id", sid)
    rec.setdefault("first_seen", now.isoformat(timespec="seconds"))
    rec["last_seen"] = now.isoformat(timespec="seconds")
    rec["locations"] = locs
    rec["tickets"] = sorted(tickets)
    rec["prompts"] = prompts[:MAX_PROMPTS]
    rec["last_prompt"] = prompts[-1]
    rec.pop("turns", None)
    rec.pop("transcript_offset", None)
    write_json(path, rec)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
