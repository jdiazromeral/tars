import pytest

from tars.backup import GIT_REPO_ENV_VARS


@pytest.fixture(autouse=True)
def _no_inherited_git_repo(monkeypatch):
    # Tests run git in temp dirs; under `git rebase --exec` or a hook the parent's
    # GIT_DIR would redirect those calls onto the enclosing repo.
    for var in GIT_REPO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
