from test_agent import FakeBlock, FakeClientWrapper, FakeResponse

from hone.review import ReviewJob, run_review


class FakeBitbucket:
    def __init__(self, repo, comments=()):
        from hone.bitbucket import PullRequest
        self.posted = []
        self._comments = list(comments)
        self._pr = PullRequest(
            id=7, title="fixture PR", description="", author="Test",
            source_branch="feature",
            source_commit=repo["head_sha"],
            dest_branch="main", dest_commit=repo["main_sha"])

    def get_pull_request(self, repo, pr_id):
        return self._pr

    def get_diffstat(self, repo, pr_id):
        return [("app.py", "modified", 1, 1)]

    def get_comments(self, repo, pr_id):
        return self._comments

    def post_comment(self, repo, pr_id, markdown):
        self.posted.append(markdown)


def test_dry_run_returns_text_and_cleans_up(tmp_path, fixture_repo,
                                            monkeypatch, capsys):
    responses = [
        FakeResponse(content=[FakeBlock(type="tool_use", name="get_pr_diff")],
                     stop_reason="tool_use"),
        FakeResponse(content=[FakeBlock(type="text", text="## review"),
                              FakeBlock(type="tool_use", name="post_review",
                                        input={"markdown": "review body"})],
                     stop_reason="tool_use"),
    ]
    monkeypatch.setattr(
        "hone.review.make_client",
        lambda api_key: FakeClientWrapper(responses),
    )
    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])
    bb = FakeBitbucket(fixture_repo)

    result = run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7,
                                  dry_run=True),
                        bitbucket=bb, repos=mgr, api_key="fake")

    assert result.posted is True
    assert bb.posted == []                 # dry run never posts
    assert "review body" in capsys.readouterr().out  # printed instead
    worktrees = (tmp_path / "data" / "worktrees")
    assert not any(worktrees.iterdir())    # worktree cleaned up


def test_dry_run_ignores_dedup_marker(tmp_path, fixture_repo, monkeypatch):
    # a marker is on the PR, but dry-run is for human re-iteration and
    # posts nothing — skipping it protects nothing
    prior = f"<!-- hone:review:{fixture_repo['head_sha']} -->\nold review"
    bb = FakeBitbucket(fixture_repo, comments=[prior])

    reached = []
    responses = [
        FakeResponse(content=[FakeBlock(type="tool_use", name="get_pr_diff")],
                     stop_reason="tool_use"),
        FakeResponse(content=[FakeBlock(type="text", text="## review")],
                     stop_reason="end_turn"),
    ]

    def make_client(api_key):
        reached.append(api_key)
        return FakeClientWrapper(responses)
    monkeypatch.setattr("hone.review.make_client", make_client)

    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])

    result = run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7,
                                  dry_run=True),
                        bitbucket=bb, repos=mgr, api_key="fake")

    assert reached == ["fake"]          # agent actually ran
    assert result.skipped is False
    assert result.final_text == "## review"
    assert bb.posted == []              # still a dry run — nothing posted


def test_skips_when_head_already_reviewed(tmp_path, fixture_repo,
                                           monkeypatch):
    # marker comment from a previous run is already on the PR
    prior = f"<!-- hone:review:{fixture_repo['head_sha']} -->\nold review"
    bb = FakeBitbucket(fixture_repo, comments=[prior])

    # the dedup check must fire before any LLM client is even constructed
    def boom(api_key):
        raise AssertionError("make_client must not be called on a skip")
    monkeypatch.setattr("hone.review.make_client", boom)

    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])

    result = run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7),
                        bitbucket=bb, repos=mgr, api_key="fake")

    assert result.posted is False
    assert result.skipped is True
    assert bb.posted == []
    worktrees = tmp_path / "data" / "worktrees"
    assert not worktrees.exists() or not any(worktrees.iterdir())


def _runnable_responses():
    """Two-turn script: one tool call, then post_review — enough for any
    test that needs the agent to actually complete a review."""
    return [
        FakeResponse(content=[FakeBlock(type="tool_use", name="get_pr_diff")],
                     stop_reason="tool_use"),
        FakeResponse(content=[FakeBlock(type="text", text="## review"),
                              FakeBlock(type="tool_use", name="post_review",
                                        input={"markdown": "review body"})],
                     stop_reason="tool_use"),
    ]


MUTED_COMMENTS = ["<!-- hone:muted -->\nhone muted for this PR"]


def test_skips_when_pr_is_muted(tmp_path, fixture_repo, monkeypatch):
    # the mute marker is the PR-level off switch: no LLM, no mirror, no
    # worktree — the skip must fire in the same pre-mirror scan as dedup
    bb = FakeBitbucket(fixture_repo, comments=MUTED_COMMENTS)

    def boom(api_key):
        raise AssertionError("make_client must not be called on a muted PR")
    monkeypatch.setattr("hone.review.make_client", boom)

    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])

    result = run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7),
                        bitbucket=bb, repos=mgr, api_key="fake")

    assert result.skipped is True
    assert bb.posted == []


def test_force_review_runs_despite_mute_and_dedup(tmp_path, fixture_repo,
                                                  monkeypatch):
    # @hone review means "review now, period": bypasses the mute state
    # AND the same-head dedup marker
    prior = f"<!-- hone:review:{fixture_repo['head_sha']} -->\nold review"
    bb = FakeBitbucket(fixture_repo, comments=[prior] + MUTED_COMMENTS)

    monkeypatch.setattr(
        "hone.review.make_client",
        lambda api_key: FakeClientWrapper(_runnable_responses()))

    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])

    result = run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7,
                                  force=True),
                        bitbucket=bb, repos=mgr, api_key="fake")

    assert result.skipped is False
    assert result.posted is True


def test_dry_run_ignores_mute(tmp_path, fixture_repo, monkeypatch):
    # dry-run is the human's re-testing path; the mute switch governs
    # automatic reviews, not an explicit human invocation
    bb = FakeBitbucket(fixture_repo, comments=MUTED_COMMENTS)

    monkeypatch.setattr(
        "hone.review.make_client",
        lambda api_key: FakeClientWrapper(_runnable_responses()))

    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])

    result = run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7,
                                  dry_run=True),
                        bitbucket=bb, repos=mgr, api_key="fake")

    assert result.skipped is False


def test_prior_reviews_flow_into_the_prompt(tmp_path, fixture_repo,
                                            monkeypatch):
    # delta reviews: hone's own earlier review comments (and only those)
    # are handed to the prompt as previous rounds, markers stripped
    prior = "<!-- hone:review:oldhead -->\nprior findings live here"
    bb = FakeBitbucket(fixture_repo, comments=[prior, "a human comment"])

    seen = {}
    import hone.review as review_mod

    real_initial_context = review_mod.initial_context

    def spy(pr, diffstat, previous_reviews=()):
        seen["previous_reviews"] = list(previous_reviews)
        return real_initial_context(pr, diffstat,
                                    previous_reviews=previous_reviews)
    monkeypatch.setattr("hone.review.initial_context", spy)

    responses = [
        FakeResponse(content=[FakeBlock(type="text", text="## review")],
                     stop_reason="end_turn"),
    ]
    monkeypatch.setattr(
        "hone.review.make_client",
        lambda api_key: FakeClientWrapper(responses))

    from hone.repo import RepoManager

    mgr = RepoManager(data_dir=tmp_path / "data",
                      username="e", api_key="k",
                      remote_url_for=lambda ws, repo: fixture_repo["url"])

    run_review(ReviewJob(workspace="ws", repo="repo", pr_id=7),
               bitbucket=bb, repos=mgr, api_key="fake")

    assert seen["previous_reviews"] == ["prior findings live here"]
