import subprocess

import pytest


@pytest.fixture
def fixture_repo(tmp_path):
    """A real git repo with a main branch and a feature branch holding one
    extra commit. Served to RepoManager via file:// so tests need no network
    and no credentials."""
    src = tmp_path / "original"
    src.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=src, check=True,
            capture_output=True, text=True,
        ).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (src / "app.py").write_text("def run():\n    return 'old'\n")
    (src / "README.md").write_text("# fixture\n")
    git("add", ".")
    git("commit", "-q", "-m", "initial")
    main_sha = git("rev-parse", "HEAD").strip()

    git("checkout", "-q", "-b", "feature")
    (src / "app.py").write_text("def run():\n    return 'new'\n")
    git("commit", "-q", "-a", "-m", "change run result")
    head_sha = git("rev-parse", "HEAD").strip()

    return {"path": src, "url": f"file://{src}",
            "main_sha": main_sha, "head_sha": head_sha, "git": git}
