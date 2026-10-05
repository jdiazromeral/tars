"""The plugin's hooks: session_log stages evidence, pending surfaces it, end-of-day consumes it."""

import datetime
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"


def run_hook(name, tmp_path, payload=None, **env):
    # Never inherit the real vault or a headless flag; never touch the real ~/.claude.
    inherited = {k: v for k, v in os.environ.items() if k not in ("TARS_HOME", "TARS_EOD_HEADLESS")}
    full_env = {
        **inherited,
        "HOME": str(tmp_path),
        "TARS_ACTIVITY_STAGING": str(tmp_path / "staging"),
        **env,
    }
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=json.dumps(payload or {}),
        capture_output=True,
        text=True,
        env=full_env,
        timeout=30,
    )


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def write_transcript(path, entries):
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def today():
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d")


def staged(tmp_path, sid="s1"):
    return json.loads((tmp_path / "staging" / today() / (sid + ".json")).read_text())


def stop(tmp_path, transcript, sid="s1"):
    payload = {"session_id": sid, "cwd": str(tmp_path), "transcript_path": str(transcript)}
    return run_hook("session_log.py", tmp_path, payload)


def test_hooks_json_points_at_shipped_scripts():
    config = json.loads((HOOKS / "hooks.json").read_text())
    commands = [
        h["command"]
        for groups in config["hooks"].values()
        for group in groups
        for h in group["hooks"]
    ]
    assert commands
    for command in commands:
        script = command.split("${CLAUDE_PLUGIN_ROOT}/hooks/")[1].rstrip('"')
        assert (HOOKS / script).is_file()


def test_session_log_stages_only_human_prompts_and_tickets(tmp_path):
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [
        user("fix DESEO-1234 in the cache path, per RFC-7231"),
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "user", "isMeta": True, "message": {"content": "meta DESEO-9"}},
        user("<command-name>/clear</command-name>"),
        user("<command-name>/tars:track</command-name><command-args>1h</command-args>"),
        {"type": "assistant", "message": {"content": "ok PROJ-1"}},
    ])
    assert stop(tmp_path, transcript).returncode == 0
    rec = staged(tmp_path)
    assert rec["prompts"] == ["fix DESEO-1234 in the cache path, per RFC-7231", "/tars:track 1h"]
    assert rec["tickets"] == ["DESEO-1234"]
    assert rec["turns"] == 1


def test_session_log_reads_only_what_each_turn_added(tmp_path):
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [user("first")])
    stop(tmp_path, transcript)
    write_transcript(transcript, [user("second")])
    stop(tmp_path, transcript)
    rec = staged(tmp_path)
    assert rec["prompts"] == ["first", "second"]
    assert rec["last_prompt"] == "second"
    assert rec["turns"] == 2


def test_session_after_review_stages_only_new_prompts(tmp_path):
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [user("reviewed already")])
    stop(tmp_path, transcript)
    # end-of-day resolves the day: its files move under reviewed/<day>/
    day = tmp_path / "staging" / today()
    reviewed = tmp_path / "staging" / "reviewed" / today()
    reviewed.mkdir(parents=True)
    shutil.move(str(day / "s1.json"), reviewed / "s1.json")
    day.rmdir()

    write_transcript(transcript, [user("after the review")])
    stop(tmp_path, transcript)
    assert staged(tmp_path)["prompts"] == ["after the review"]


def test_session_log_is_silent_in_headless_runs(tmp_path):
    transcript = tmp_path / "t.jsonl"
    write_transcript(transcript, [user("cron")])
    payload = {"session_id": "s1", "cwd": str(tmp_path), "transcript_path": str(transcript)}
    out = run_hook("session_log.py", tmp_path, payload, TARS_EOD_HEADLESS="1")
    assert out.returncode == 0 and out.stdout == ""
    assert not (tmp_path / "staging").exists()


def test_session_log_never_fails_a_turn(tmp_path):
    out = run_hook("session_log.py", tmp_path, {"session_id": "s1", "transcript_path": 42})
    assert out.returncode == 0 and out.stdout == ""


def test_pending_names_unreviewed_past_days_only(tmp_path):
    vault = tmp_path / "vault"
    (vault / "tasks").mkdir(parents=True)
    staging = tmp_path / "staging"
    for d in ("2020-01-01", "2020-01-02", today(), "reviewed/2019-12-31"):
        (staging / d).mkdir(parents=True)
    out = run_hook("pending.py", tmp_path, TARS_HOME=str(vault))
    msg = json.loads(out.stdout)["systemMessage"]
    assert "Unreviewed session logs: 2020-01-01, 2020-01-02 → /tars:end-of-day" in msg
    assert today() not in msg.split("\n", 1)[1]
    # once a day: the second startup stays quiet
    assert run_hook("pending.py", tmp_path, TARS_HOME=str(vault)).stdout == ""


def test_pending_without_a_vault_is_silent(tmp_path):
    (tmp_path / "staging" / "2020-01-01").mkdir(parents=True)
    out = run_hook("pending.py", tmp_path)
    assert out.returncode == 0 and out.stdout == ""
