import pytest

from hone.repo import GitError, RepoManager


@pytest.fixture
def manager(tmp_path, fixture_repo):
    return RepoManager(
        data_dir=tmp_path / "data",
        username="nobody@example.com",
        api_key="unused-for-file-urls",
        remote_url_for=lambda ws, repo: fixture_repo["url"],
    )


def test_ensure_mirror_clones_on_first_use(manager):
    mirror = manager.ensure_mirror("ws", "repo")
    assert mirror.exists()
    assert (mirror / "HEAD").exists()


def test_ensure_mirror_fetch_picks_up_new_commits(manager, fixture_repo):
    mirror = manager.ensure_mirror("ws", "repo")

    # the remote moves after the clone — a plain --bare clone has no fetch
    # refspec, so a naive `git fetch origin` updates nothing and exits 0
    (fixture_repo["path"] / "app.py").write_text(
        "def run():\n    return 'newer'\n")
    fixture_repo["git"]("commit", "-q", "-a", "-m", "another change")
    new_head = fixture_repo["git"]("rev-parse", "HEAD").strip()
    assert new_head != fixture_repo["head_sha"]

    manager.ensure_mirror("ws", "repo")
    assert fixture_repo["git"]("-C", str(mirror), "rev-parse",
                               "feature").strip() == new_head


def test_create_and_remove_worktree(manager, fixture_repo):
    mirror = manager.ensure_mirror("ws", "repo")
    wt = manager.create_worktree(mirror, fixture_repo["head_sha"])
    assert (wt.path / "app.py").read_text() == "def run():\n    return 'new'\n"
    manager.remove_worktree(wt)
    assert not wt.path.exists()


def test_diff_between_base_and_head_shows_change(manager, fixture_repo):
    mirror = manager.ensure_mirror("ws", "repo")
    base = manager.merge_base(mirror, "main", fixture_repo["head_sha"])
    assert base == fixture_repo["main_sha"]
    out = manager.diff(mirror, base, fixture_repo["head_sha"])
    assert "+    return 'new'" in out
    assert "return 'old'" in out


def test_diff_single_file_unknown_path_is_empty(manager, fixture_repo):
    mirror = manager.ensure_mirror("ws", "repo")
    base = manager.merge_base(mirror, "main", fixture_repo["head_sha"])
    out = manager.diff(mirror, base, fixture_repo["head_sha"], "nope.py")
    assert out == ""


def test_git_error_surfaces_stderr(manager, fixture_repo):
    mirror = manager.ensure_mirror("ws", "repo")
    with pytest.raises(GitError):
        manager.merge_base(mirror, "main", "0" * 40)


def test_sweep_stale_worktrees_removes_orphans(manager, fixture_repo):
    """A job dir left behind by a hard kill is removed with its git
    registration; the mirror's own entry is untouched."""
    mirror = manager.ensure_mirror("ws", "repo")
    wt = manager.create_worktree(mirror, fixture_repo["head_sha"])  # orphan it

    assert manager.sweep_stale_worktrees() == 1
    assert not wt.path.exists()

    out = fixture_repo["git"]("-C", str(mirror), "worktree", "list",
                              "--porcelain")
    registrations = [line for line in out.splitlines()
                     if line.startswith("worktree ")]
    assert registrations == [f"worktree {mirror}"]


def test_sweep_stale_worktrees_leaves_non_job_worktrees(manager, tmp_path,
                                                        fixture_repo):
    """Paths outside data_dir/worktrees are not ours to touch."""
    mirror = manager.ensure_mirror("ws", "repo")
    other = tmp_path / "other-worktree"
    fixture_repo["git"]("-C", str(mirror), "worktree", "add", "--detach",
                        str(other), fixture_repo["head_sha"])

    assert manager.sweep_stale_worktrees() == 0
    assert other.exists()

    fixture_repo["git"]("-C", str(mirror), "worktree", "remove", "--force",
                        str(other))


def test_sweep_stale_worktrees_noop_without_state(manager, tmp_path):
    """No mirrors and an empty (or missing) worktrees dir: no-op."""
    (manager.data_dir / "worktrees").mkdir(parents=True)
    assert manager.sweep_stale_worktrees() == 0


def test_sweep_stale_worktrees_skips_sick_mirror(manager, fixture_repo):
    """One bad mirror must not block sweeping the healthy ones (a corrupt
    mirror, a locked worktree, a non-git dir matching *.git): a leaked dir
    is cheaper than blocked reviews."""
    mirror = manager.ensure_mirror("ws", "repo")
    wt = manager.create_worktree(mirror, fixture_repo["head_sha"])
    (manager.data_dir / "mirrors" / "ws" / "broken.git").mkdir()

    assert manager.sweep_stale_worktrees() == 1
    assert not wt.path.exists()


def test_sweep_stale_worktrees_handles_symlinked_data_dir(tmp_path,
                                                          fixture_repo):
    """A symlinked data_dir must not silently skip every worktree: the
    scoping guard resolves both sides of the comparison."""
    manager = RepoManager(
        data_dir=tmp_path / "data",
        username="nobody@example.com",
        api_key="unused-for-file-urls",
        remote_url_for=lambda ws, repo: fixture_repo["url"],
    )
    mirror = manager.ensure_mirror("ws", "repo")
    wt = manager.create_worktree(mirror, fixture_repo["head_sha"])

    link = tmp_path / "data-link"
    link.symlink_to(manager.data_dir, target_is_directory=True)
    linked = RepoManager(
        data_dir=link,
        username="nobody@example.com",
        api_key="unused-for-file-urls",
        remote_url_for=lambda ws, repo: fixture_repo["url"],
    )
    assert linked.sweep_stale_worktrees() == 1
    assert not wt.path.exists()


def test_sweep_stale_worktrees_prunes_missing_dirs(manager, fixture_repo):
    """Ghost registrations (worktree registered, dir deleted externally) are
    pruned silently and must not count as "removed" — whether remove
    tolerates ghosts varies by git version, so pin the deterministic
    prune-first outcome."""
    mirror = manager.ensure_mirror("ws", "repo")
    ghost = manager.create_worktree(mirror, fixture_repo["head_sha"])
    ghost.path.rename(ghost.path.with_name("moved-away"))

    assert manager.sweep_stale_worktrees() == 0
    out = fixture_repo["git"]("-C", str(mirror), "worktree", "list",
                              "--porcelain")
    assert str(ghost.path) not in out
