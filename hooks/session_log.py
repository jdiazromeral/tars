#!/usr/bin/env python3
"""Stop hook: stage evidence of what this session worked on, for end-of-day review.

One JSON file per session per day under the staging dir
(${TARS_ACTIVITY_STAGING:-~/.claude/state/tars-activity}/<YYYY-MM-DD>/).
Evidence only (repo, branch, ticket keys, prompts); the activity record itself
is written by /tars:end-of-day after the user confirms, which then moves the day
under reviewed/. Never blocks, never prints: any failure exits 0 silently.
"""

import datetime
import json
import os
import re
import subprocess
import sys

STAGING = os.path.expanduser(
    os.environ.get("TARS_ACTIVITY_STAGING") or "~/.claude/state/tars-activity"
)
TICKET = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")
NOT_TICKETS = {"UTF", "ISO", "SHA", "RFC", "CVE", "GPT", "AES", "HTTP"}
# Session plumbing, not work: never worth staging as a prompt.
BUILTIN_COMMANDS = {
    "/exit", "/clear", "/compact", "/hooks", "/config", "/model", "/resume",
    "/status", "/cost", "/help", "/login", "/logout", "/effort", "/fast",
}
MAX_PROMPTS = 5
PROMPT_CHARS = 200


def git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def tickets_in(text):
    return {t for t in TICKET.findall(text or "") if t.split("-")[0] not in NOT_TICKETS}


def user_text(entry):
    """Human-typed text of a transcript user entry, or None for tool results/meta."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    if not isinstance(content, str):
        return None
    cmd = re.search(r"<command-name>([^<]+)</command-name>", content)
    if cmd:
        if cmd.group(1).strip() in BUILTIN_COMMANDS:
            return None
        args = re.search(r"<command-args>([^<]*)</command-args>", content)
        return (cmd.group(1) + " " + (args.group(1) if args else "")).strip()
    if content.lstrip().startswith("<"):
        return None
    return content.strip()


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def main():
    if os.environ.get("TARS_EOD_HEADLESS"):
        return
    hook = json.load(sys.stdin)
    sid = hook.get("session_id")
    cwd = hook.get("cwd") or os.getcwd()
    if not sid:
        return
    now = datetime.datetime.now().astimezone()
    day = now.strftime("%Y-%m-%d")
    day_dir = os.path.join(STAGING, day)
    os.makedirs(day_dir, exist_ok=True)
    path = os.path.join(day_dir, sid + ".json")
    rec = load(path)
    if rec is None:
        rec = {}
        # Today was already reviewed while this session kept going: resume past
        # what was reviewed, so its earlier prompts are never proposed twice.
        done = load(os.path.join(STAGING, "reviewed", day, sid + ".json"))
        if done and done.get("transcript_path") == hook.get("transcript_path"):
            rec["transcript_offset"] = done.get("transcript_offset", 0)

    top = git(cwd, "rev-parse", "--show-toplevel")
    # --git-common-dir resolves a worktree back to its main repo's .git
    common = git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    main_root = os.path.dirname(common) if common else ""
    branch = git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    rec.setdefault("session_id", sid)
    rec.setdefault("first_seen", now.isoformat(timespec="seconds"))
    rec["last_seen"] = now.isoformat(timespec="seconds")
    rec["turns"] = rec.get("turns", 0) + 1
    locs = rec.get("locations", [])
    loc = {
        "cwd": cwd,
        "repo": os.path.basename(main_root) if main_root else None,
        "worktree": os.path.basename(top) if top and main_root and main_root != top else None,
        "branch": branch or None,
    }
    if loc not in locs:
        locs.append(loc)
    rec["locations"] = locs

    tickets = set(rec.get("tickets", [])) | tickets_in(branch) | tickets_in(cwd)
    # Read only the transcript bytes added since the last turn.
    tpath = hook.get("transcript_path")
    offset = rec.get("transcript_offset", 0)
    if tpath and os.path.exists(tpath):
        with open(tpath, "rb") as f:
            f.seek(offset)
            chunk = f.read()
        complete = chunk[: chunk.rfind(b"\n") + 1]
        rec["transcript_offset"] = offset + len(complete)
        rec["transcript_path"] = tpath
        prompts = rec.get("prompts", [])
        for line in complete.decode("utf-8", "replace").splitlines():
            try:
                text = user_text(json.loads(line))
            except ValueError:
                continue
            if text:
                tickets |= tickets_in(text)
                if len(prompts) < MAX_PROMPTS:
                    prompts.append(text[:PROMPT_CHARS])
                rec["last_prompt"] = text[:PROMPT_CHARS]
        rec["prompts"] = prompts
    rec["tickets"] = sorted(tickets)

    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(rec, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
