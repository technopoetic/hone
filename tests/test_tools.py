import pytest

from hone.repo import RepoManager
from hone.tools import ToolBox


@pytest.fixture
def toolbox(tmp_path, fixture_repo):
    mgr = RepoManager(
        data_dir=tmp_path / "data",
        username="nobody@example.com",
        api_key="unused",
        remote_url_for=lambda ws, repo: fixture_repo["url"],
    )
    mirror = mgr.ensure_mirror("ws", "repo")
    wt = mgr.create_worktree(mirror, fixture_repo["head_sha"])
    base = mgr.merge_base(mirror, "main", fixture_repo["head_sha"])
    posted = []
    tb = ToolBox(
        repo_manager=mgr, worktree=wt,
        base_sha=base, head_sha=fixture_repo["head_sha"],
        static_checks={"ruff": ("ruff", "check", ".")},
        post=posted.append,
    )
    yield tb, posted
    mgr.remove_worktree(wt)


def test_schemas_match_configured_static_checks(toolbox):
    tb, _ = toolbox
    names = {s["name"] for s in tb.schemas()}
    assert names == {"get_pr_diff", "rg_search", "read_file",
                     "git_log", "run_static_check", "post_review"}
    static = next(s for s in tb.schemas() if s["name"] == "run_static_check")
    assert static["input_schema"]["properties"]["tool"]["enum"] == ["ruff"]


def test_get_pr_diff_returns_change(toolbox):
    tb, _ = toolbox
    r = tb.execute("get_pr_diff", {})
    assert not r.is_error
    assert "+    return 'new'" in r.content


def test_get_pr_diff_unknown_file_says_so(toolbox):
    tb, _ = toolbox
    r = tb.execute("get_pr_diff", {"file_path": "nope.py"})
    assert "no diff" in r.content.lower()


def test_read_file_content_and_traversal_blocked(toolbox):
    tb, _ = toolbox
    ok = tb.execute("read_file", {"path": "app.py"})
    assert "return 'new'" in ok.content
    bad = tb.execute("read_file", {"path": "../../etc/passwd"})
    assert bad.is_error
    assert "escape" in bad.content.lower()


def test_read_file_missing(toolbox):
    tb, _ = toolbox
    r = tb.execute("read_file", {"path": "ghost.py"})
    assert r.is_error


def test_rg_search_finds_and_reports_match(toolbox):
    tb, _ = toolbox
    r = tb.execute("rg_search", {"pattern": "return 'new'"})
    assert "app.py:2" in r.content


def test_rg_search_no_match_is_not_an_error(toolbox):
    tb, _ = toolbox
    r = tb.execute("rg_search", {"pattern": "zzz-no-such-string"})
    assert not r.is_error
    assert "no matches" in r.content.lower()


def test_git_log_lists_commits(toolbox):
    tb, _ = toolbox
    r = tb.execute("git_log", {})
    assert "change run result" in r.content


def test_run_static_check_rejects_non_allowlisted(toolbox):
    tb, _ = toolbox
    r = tb.execute("run_static_check", {"tool": "pytest"})
    assert r.is_error
    assert "allowlist" in r.content.lower()


def test_run_static_check_resolves_eslint_builtin(tmp_path, fixture_repo):
    # eslint has no literal command in repos.toml (static_checks maps it to
    # None) — the builtin in tools.py must resolve a real argv pointing at
    # hone's OWN node_modules, never the worktree's, and actually run
    # it. The fixture repo has no eslint.config.js, so ESLint itself will
    # complain about that — which still proves the binary was found and
    # executed, as opposed to "runner is not installed".
    mgr = RepoManager(
        data_dir=tmp_path / "data", username="nobody@example.com",
        api_key="unused", remote_url_for=lambda ws, repo: fixture_repo["url"],
    )
    mirror = mgr.ensure_mirror("ws", "repo")
    wt = mgr.create_worktree(mirror, fixture_repo["head_sha"])
    base = mgr.merge_base(mirror, "main", fixture_repo["head_sha"])
    tb = ToolBox(
        repo_manager=mgr, worktree=wt, base_sha=base,
        head_sha=fixture_repo["head_sha"],
        static_checks={"eslint": None}, post=lambda _: None,
    )
    try:
        r = tb.execute("run_static_check", {"tool": "eslint"})
    finally:
        mgr.remove_worktree(wt)
    assert "not on the allowlist" not in r.content
    assert "is not installed" not in r.content
    assert "no command configured" not in r.content


def test_post_review_includes_marker_and_calls_callback(toolbox):
    tb, posted = toolbox
    r = tb.execute("post_review", {"markdown": "looks good"})
    assert not r.is_error
    assert len(posted) == 1
    assert "<!-- hone:review:" in posted[0]
    assert "looks good" in posted[0]


def test_unknown_tool_is_error(toolbox):
    tb, _ = toolbox
    r = tb.execute("summon_dragon", {})
    assert r.is_error
