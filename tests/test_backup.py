import subprocess

from tars import backup


def test_prune_keeps_the_newest_bundles(tmp_path):
    names = [f"tars-vault-2026100{d}-090000.bundle" for d in range(1, 5)]
    for name in names:
        (tmp_path / name).write_bytes(b"")
    (tmp_path / "unrelated.bundle").write_bytes(b"")
    removed = backup.prune(tmp_path, keep=2)
    assert [p.name for p in removed] == names[:2]
    assert sorted(p.name for p in tmp_path.iterdir()) == names[2:] + ["unrelated.bundle"]


def _commit_repo(path):
    """A one-commit repo at `path`; returns (HEAD sha, commit count)."""
    path.mkdir()
    git = ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    (path / "f.txt").write_text(path.name)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", path.name], check=True)
    return _snapshot(path)


def _snapshot(path):
    def out(*args):
        return subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                              text=True, check=True).stdout.strip()
    return out("rev-parse", "HEAD"), out("rev-list", "--count", "HEAD"), out("config", "core.bare")


def test_git_calls_ignore_an_inherited_git_dir(tmp_path, monkeypatch):
    # `git rebase --exec` and git hooks export GIT_DIR; without scrubbing, backup
    # would act on the enclosing repo instead of the vault.
    vault, decoy = tmp_path / "vault", tmp_path / "decoy"
    vault_head = _commit_repo(vault)[0]
    decoy_before = _commit_repo(decoy)
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))

    assert backup.has_uncommitted_changes(vault) is False
    bundle = backup.create_bundle(vault, tmp_path / "bundles")

    heads = subprocess.run(["git", "bundle", "list-heads", str(bundle)], capture_output=True,
                           text=True, env=backup.git_env(), check=True).stdout
    assert vault_head in heads
    assert _snapshot(decoy) == decoy_before
