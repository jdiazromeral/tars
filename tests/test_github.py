import pytest

from tars import db as database, store
from tars.connectors import github


def _pr(n, title, author="me"):
    return {
        "number": n, "title": title, "state": "closed",
        "merged_at": "2026-01-01T00:00:00Z", "draft": False, "body": "desc",
        "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-02T00:00:00Z",
        "additions": 1, "deletions": 0, "changed_files": 1,
        "html_url": f"https://github.com/acme/repo/pull/{n}",
        "user": {"login": author},
        "base": {"repo": {"full_name": "acme/repo"}, "ref": "main"},
        "head": {"ref": f"feature/{title.split()[0]}"},
    }


def _fake_gh(fetchable):
    """Dispatch gh api calls. `fetchable` = PR numbers allowed to be fetched;
    fetching any other PR raises (so we can assert a PR was filtered pre-fetch)."""
    def fake(*args):
        a0 = args[0] if args else ""
        if args == ("user",):
            return {"login": "me"}
        if "search/issues" in " ".join(args):
            return [
                {"title": "PROJ-10 add feed thing", "number": 10,
                 "repository_url": "https://api.github.com/repos/acme/repo"},
                {"title": "ACME-20 pipeline thing", "number": 20,
                 "repository_url": "https://api.github.com/repos/acme/repo"},
            ]
        for n in (10, 20):
            if a0.endswith(f"/pulls/{n}") and not a0.endswith(("comments", "reviews")):
                assert n in fetchable, f"PR #{n} should have been filtered before fetch"
                return _pr(n, {10: "PROJ-10 add feed thing", 20: "ACME-20 pipeline thing"}[n])
        return []  # comments / reviews
    return fake


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("TARS_HOME", str(tmp_path))
    store.init_root(tmp_path)
    return tmp_path


def test_ticket_prefix_filter_keeps_only_matching(root, monkeypatch):
    (root / "connectors.yml").write_text(
        "github:\n  orgs: [acme]\n  reviewers: []\n  ticket_prefixes: [PROJ]\n"
    )
    monkeypatch.setattr(github, "_gh_json", _fake_gh(fetchable={10}))  # #20 must not be fetched
    db = database.connect(root)
    github.sync(root, db, None)
    origins = {r["origin"] for r in db.execute("SELECT origin FROM documents").fetchall()}
    assert origins == {"github:acme/repo#10"}


def test_no_prefix_keeps_everything(root, monkeypatch):
    (root / "connectors.yml").write_text(
        "github:\n  orgs: [acme]\n  reviewers: []\n"  # no ticket_prefixes
    )
    monkeypatch.setattr(github, "_gh_json", _fake_gh(fetchable={10, 20}))
    db = database.connect(root)
    github.sync(root, db, None)
    origins = {r["origin"] for r in db.execute("SELECT origin FROM documents").fetchall()}
    assert origins == {"github:acme/repo#10", "github:acme/repo#20"}
