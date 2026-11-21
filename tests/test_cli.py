from types import SimpleNamespace

import pytest

from hone.cli import _github_repos, build_process, main, parse_pr_url
from hone.config import Settings
from hone.server import CommandRequest, ReviewRequest


def _settings(tmp_path, **overrides):
    base = dict(
        llm_api_key="sk", llm_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path / "data",
        tool_commands={"ruff": ("ruff", "check", ".")},
        repo_checks={"repo": ("ruff",)},
        max_turns=7, bitbucket_webhook_secret=None, port=8420)
    return Settings(**{**base, **overrides})


def test_serve_without_bitbucket_webhook_secret_is_a_config_error(
        tmp_path, monkeypatch, capsys):
    # serve's only mode is posting, so a missing secret is a startup config
    # error like any other: house error shape, exit 2, BEFORE logging/clients/
    # sweep leave side effects on a boot that can never proceed
    monkeypatch.setattr(
        "hone.cli.load_settings",
        lambda: _settings(tmp_path, bitbucket_webhook_secret=None))
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)

    rc = main(["serve"])

    assert rc == 2
    assert "error: BITBUCKET_WEBHOOK_SECRET is required for serve" in (
        capsys.readouterr().err)
    # failed before any startup side effect: no log dir created
    assert not (tmp_path / "data" / "logs").exists()


def test_serve_github_only_without_secret_is_a_config_error(
        tmp_path, monkeypatch, capsys):
    # same refusal, symmetric: GitHub as the only enabled forge keeps its
    # own specific error, not a Bitbucket-flavored one
    monkeypatch.setattr(
        "hone.cli.load_settings",
        lambda: _settings(
            tmp_path, bitbucket_email=None, bitbucket_username=None,
            bitbucket_api_key=None, bitbucket_workspace=None,
            github_token="tok", github_owner="octo-org",
            github_webhook_secret=None))
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)

    rc = main(["serve"])

    assert rc == 2
    assert "error: GITHUB_WEBHOOK_SECRET is required for serve" in (
        capsys.readouterr().err)
    assert not (tmp_path / "data" / "logs").exists()


def test_serve_wires_gitlab_client_and_mirror_manager(
        tmp_path, monkeypatch):
    # GitLab-only serve boot: the process's forges/repo_managers dicts
    # must carry the gitlab client (pinned to the CONFIGURED namespace)
    # and its isolated mirror manager, and rq.forge must dispatch to them
    settings = _settings(
        tmp_path, bitbucket_email=None, bitbucket_username=None,
        bitbucket_api_key=None, bitbucket_workspace=None,
        github_token=None, github_owner=None,
        gitlab_token="glpat", gitlab_url="https://git.example.com/gitlab",
        gitlab_namespace="engineering",
        gitlab_webhook_signing_token="sign")
    monkeypatch.setattr("hone.cli.load_settings", lambda: settings)
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)
    monkeypatch.setattr("hone.cli.configure_logging",
                        lambda data_dir: data_dir / "logs" / "hone.log")

    gitlab_clients = []

    class FakeGitLabClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            gitlab_clients.append(self)

    class FakeManager:
        def sweep_stale_worktrees(self):
            return 0

    managers = []

    def fake_gitlab_repos(settings, data_dir):
        mgr = FakeManager()
        managers.append(mgr)
        return mgr

    served = {}

    def fake_serve(settings, process_factory):
        served["process"] = process_factory()

    review_calls = []

    def fake_run_review(job, **kwargs):
        review_calls.append(kwargs)
        return SimpleNamespace(skipped=False, final_text="t", posted=True)

    monkeypatch.setattr("hone.cli.GitLabClient", FakeGitLabClient)
    monkeypatch.setattr("hone.cli._gitlab_repos", fake_gitlab_repos)
    monkeypatch.setattr("hone.cli.serve", fake_serve)
    monkeypatch.setattr("hone.cli.run_review", fake_run_review)

    def bitbucket_boom(*args, **kwargs):
        raise AssertionError(
            "BitbucketClient must not be constructed for a GitLab-only serve")
    def github_boom(*args, **kwargs):
        raise AssertionError(
            "GitHubClient must not be constructed for a GitLab-only serve")
    monkeypatch.setattr("hone.cli.BitbucketClient", bitbucket_boom)
    monkeypatch.setattr("hone.cli.GitHubClient", github_boom)

    rc = main(["serve"])

    assert rc == 0
    (client,) = gitlab_clients
    (mgr,) = managers
    assert client.kwargs == {"token": "glpat",
                             "base_url": "https://git.example.com/gitlab",
                             "namespace": "engineering"}
    # a gitlab job dispatched through the served process lands on exactly
    # that client and that manager
    served["process"](ReviewRequest(workspace="engineering", repo="repo",
                                     pr_id=5, forge="gitlab"))
    (kwargs,) = review_calls
    assert kwargs["forge"] is client and kwargs["repos"] is mgr


def test_parse_valid_bitbucket_url():
    forge, ws, repo, pr_id = parse_pr_url(
        "https://bitbucket.org/hmdatasolutions/mekanik/pull-requests/12")
    assert (forge, ws, repo, pr_id) == ("bitbucket", "hmdatasolutions",
                                        "mekanik", 12)


def test_parse_valid_github_url():
    forge, owner, repo, pr_id = parse_pr_url(
        "https://github.com/technopoetic/hone/pull/7")
    assert (forge, owner, repo, pr_id) == ("github", "technopoetic",
                                           "hone", 7)


@pytest.mark.parametrize("bad", [
    "https://github.com/x/y/pull/",
    "https://github.com/x/y",
    "https://bitbucket.org/ws/repo",
    "not a url",
])
def test_parse_rejects_non_pr_urls(bad):
    with pytest.raises(ValueError):
        parse_pr_url(bad)


@pytest.mark.parametrize("url,namespace,repo", [
    ("https://gitlab.com/group/project/-/merge_requests/9", "group", "project"),
    ("https://git.example.com/gitlab/group/sub/project/-/merge_requests/9",
     "group/sub", "project"),
])
def test_parse_valid_gitlab_url(url, namespace, repo):
    # GitLab recognition is tied to the CONFIGURED base URL, so a
    # self-managed instance parses against its own base, not gitlab.com
    base = "https://gitlab.com" if "gitlab.com" in url else \
        "https://git.example.com/gitlab"
    assert parse_pr_url(url, gitlab_url=base) == (
        "gitlab", namespace, repo, 9)


def test_parse_gitlab_url_strips_surrounding_whitespace():
    assert parse_pr_url(
        "  https://gitlab.com/group/project/-/merge_requests/9 \n",
        gitlab_url="https://gitlab.com") == ("gitlab", "group", "project", 9)


@pytest.mark.parametrize("url", [
    "https://evil.example/group/project/-/merge_requests/9",
    "https://git.example.com/gitlab-evil/group/project/-/merge_requests/9",
    "https://gitlab.com/group/../project/-/merge_requests/9",
    "https://gitlab.com/group/project/-/merge_requests/9/diffs",
])
def test_parse_rejects_wrong_or_traversal_gitlab_urls(url):
    # the dynamic pattern is an exact full match on the configured base,
    # so a lookalike base path, a `..` segment, or an extra MR suffix all
    # fail — never a partial-prefix match
    base = "https://gitlab.com" if "gitlab.com" in url else \
        "https://git.example.com/gitlab"
    with pytest.raises(ValueError):
        parse_pr_url(url, gitlab_url=base)


def test_gitlab_url_unrecognized_without_configured_base(tmp_path):
    # a GitLab-shaped URL on GitHub-only settings (gitlab_url is None)
    # must not parse: no configured base, no recognition
    settings = _settings(
        tmp_path, bitbucket_email=None, bitbucket_username=None,
        bitbucket_api_key=None, bitbucket_workspace=None,
        github_token="gh", github_owner="octo-org")
    with pytest.raises(ValueError):
        parse_pr_url("https://gitlab.com/group/project/-/merge_requests/9",
                     gitlab_url=settings.gitlab_url)


def test_github_repos_use_their_own_data_dir_subtree(tmp_path):
    # a repo with the same name under the same handle on both forges
    # (e.g. "technopoetic/hone" on both) must not collide on one mirror
    settings = Settings(
        llm_api_key="sk", llm_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        bitbucket_webhook_secret=None, port=8420, github_token="tok",
        github_owner="technopoetic")

    mgr = _github_repos(settings, tmp_path)

    assert mgr.data_dir == tmp_path / "github"


def test_gitlab_repos_use_their_own_data_dir_subtree(tmp_path):
    # same collision argument as the github subtree: a repo with the same
    # name under the same handle on two forges must not share one mirror.
    # Imported here (not module-level) so a missing helper fails this one
    # test, not the whole module's collection.
    from hone.cli import _gitlab_repos

    settings = _settings(
        tmp_path, gitlab_token="gl", gitlab_url="https://gitlab.com",
        gitlab_namespace="engineering")

    assert _gitlab_repos(settings, tmp_path).data_dir == tmp_path / "gitlab"


def test_build_process_runs_live_reviews(tmp_path, monkeypatch):
    # the serve seam: a ReviewRequest in, one live (never dry-run) run_review
    # out, with the job fields taken from settings, not the request
    settings = Settings(
        llm_api_key="sk", llm_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={"ruff": ("ruff", "check", ".")},
        repo_checks={"repo": ("ruff",)},
        max_turns=7, bitbucket_webhook_secret=None, port=8420)

    calls = []

    def fake_run_review(job, *, forge, repos, api_key, base_url=None):
        calls.append((job, forge, repos, api_key))
        return "result"

    monkeypatch.setattr("hone.cli.run_review", fake_run_review)

    bb, repos = object(), object()
    process = build_process(settings, forges={"bitbucket": bb},
                            repo_managers={"bitbucket": repos})
    process(ReviewRequest(workspace="ws", repo="repo", pr_id=9))

    ((job, got_bb, got_repos, got_key),) = calls
    assert (job.workspace, job.repo, job.pr_id) == ("ws", "repo", 9)
    assert job.dry_run is False  # serve is live-only: no dry-run mode exists
    assert job.model == "m"
    assert job.static_checks == {"ruff": ("ruff", "check", ".")}
    assert job.max_turns == 7
    assert got_bb is bb and got_repos is repos and got_key == "sk"


def test_build_process_passes_force_through(tmp_path, monkeypatch):
    # @hone review rides the same serve seam, but lands as a forced job
    settings = Settings(
        llm_api_key="sk", llm_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        bitbucket_webhook_secret=None, port=8420)
    calls = []

    monkeypatch.setattr("hone.cli.run_review",
                        lambda job, **kw: calls.append(job) or "result")
    process = build_process(settings, forges={"bitbucket": object()},
                            repo_managers={"bitbucket": object()})
    process(ReviewRequest(workspace="ws", repo="repo", pr_id=9, force=True))

    (job,) = calls
    assert job.force is True


def test_build_process_dispatches_on_req_forge(tmp_path, monkeypatch):
    # a multi-forge process must route each request to ITS forge's client
    # and repo manager, not always the first one configured
    settings = Settings(
        llm_api_key="sk", llm_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        bitbucket_webhook_secret=None, port=8420)

    calls = []
    monkeypatch.setattr(
        "hone.cli.run_review",
        lambda job, **kw: calls.append(kw) or "result")

    bb, bb_repos = object(), object()
    gh, gh_repos = object(), object()
    gl, gl_repos = object(), object()
    process = build_process(
        settings, forges={"bitbucket": bb, "github": gh, "gitlab": gl},
        repo_managers={"bitbucket": bb_repos, "github": gh_repos,
                       "gitlab": gl_repos})

    process(ReviewRequest(workspace="ws", repo="repo", pr_id=1,
                           forge="github"))
    process(ReviewRequest(workspace="ws", repo="repo", pr_id=2,
                           forge="bitbucket"))
    process(ReviewRequest(workspace="ws", repo="repo", pr_id=3,
                           forge="gitlab"))

    assert calls[0]["forge"] is gh and calls[0]["repos"] is gh_repos
    assert calls[1]["forge"] is bb and calls[1]["repos"] is bb_repos
    assert calls[2]["forge"] is gl and calls[2]["repos"] is gl_repos


def test_review_bitbucket_url_requires_bitbucket_group(
        tmp_path, monkeypatch, capsys):
    # Bitbucket credentials are now optional, so reviewing a Bitbucket URL
    # with no Bitbucket group must fail with a clear error (mirroring the
    # GitHub gate), not a confusing downstream 401
    monkeypatch.setattr(
        "hone.cli.load_settings",
        lambda: _settings(
            tmp_path, bitbucket_email=None, bitbucket_username=None,
            bitbucket_api_key=None, bitbucket_workspace=None,
            github_token="gh", github_owner="octo-org"))
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)
    rc = main([
        "review", "https://bitbucket.org/ws/repo/pull-requests/7"])
    assert rc == 2
    assert "BITBUCKET_EMAIL" in capsys.readouterr().err


def test_review_command_requires_github_config_for_github_url(
        tmp_path, monkeypatch, capsys):
    # settings.github_token is None here — reviewing a GitHub URL must
    # fail with a clear config error, not a confusing downstream 401
    monkeypatch.setattr("hone.cli.load_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)

    rc = main(["review", "https://github.com/technopoetic/hone/pull/7"])

    assert rc == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "data" / "logs").exists()


def test_review_gitlab_url_with_gitlab_only_config(
        tmp_path, monkeypatch, capsys):
    # GitLab-only settings: every Bitbucket and GitHub field is None —
    # `review` must work with only the matched configured forge's
    # credentials
    settings = _settings(
        tmp_path, bitbucket_email=None, bitbucket_username=None,
        bitbucket_api_key=None, bitbucket_workspace=None,
        github_token=None, github_owner=None,
        gitlab_token="glpat", gitlab_url="https://gitlab.com",
        gitlab_namespace="engineering")
    monkeypatch.setattr("hone.cli.load_settings", lambda: settings)
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)
    monkeypatch.setattr("hone.cli.configure_logging",
                        lambda data_dir: data_dir / "logs" / "hone.log")

    gitlab_clients = []

    class FakeGitLabClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            gitlab_clients.append(self)

    class FakeManager:
        def sweep_stale_worktrees(self):
            return 0

    manager = FakeManager()

    review_calls = []

    def fake_run_review(job, **kwargs):
        review_calls.append((job, kwargs))
        return SimpleNamespace(skipped=False, final_text="review",
                               posted=True)

    monkeypatch.setattr("hone.cli.GitLabClient", FakeGitLabClient)
    monkeypatch.setattr("hone.cli._gitlab_repos",
                        lambda settings, data_dir: manager)
    monkeypatch.setattr("hone.cli.run_review", fake_run_review)

    def bitbucket_boom(*args, **kwargs):
        raise AssertionError(
            "BitbucketClient must not be constructed for a GitLab review")
    monkeypatch.setattr("hone.cli.BitbucketClient", bitbucket_boom)

    rc = main(["review",
               "https://gitlab.com/group/sub/project/-/merge_requests/9",
               "--dry-run"])

    assert rc == 0
    # exactly one review call, and it was a dry-run job
    ((job, kwargs),) = review_calls
    assert (job.workspace, job.repo, job.pr_id) == ("group/sub", "project", 9)
    assert job.dry_run is True
    # the client is pinned to the URL's namespace, like GitHub's owner
    (client,) = gitlab_clients
    assert client.kwargs == {"token": "glpat",
                             "base_url": "https://gitlab.com",
                             "namespace": "group/sub"}
    assert kwargs["forge"] is client and kwargs["repos"] is manager
    assert "review" in capsys.readouterr().out  # dry-run prints the text


def test_review_gitlab_url_requires_gitlab_token(
        tmp_path, monkeypatch, capsys):
    # Defensive gate mirroring the GitHub/Bitbucket ones: a Settings with
    # the base configured but no token (only constructible directly —
    # load_settings refuses a partial group) must fail with a clear
    # config error, not a downstream GitLab 401
    settings = _settings(
        tmp_path, bitbucket_email=None, bitbucket_username=None,
        bitbucket_api_key=None, bitbucket_workspace=None,
        gitlab_token=None, gitlab_url="https://gitlab.com",
        gitlab_namespace="engineering")
    monkeypatch.setattr("hone.cli.load_settings", lambda: settings)
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)

    rc = main(["review",
               "https://gitlab.com/group/project/-/merge_requests/9"])

    assert rc == 2
    assert "GITLAB_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "data" / "logs").exists()


def test_command_requests_post_marker_comments_and_never_review(
        tmp_path, monkeypatch):
    # mute/unmute are deterministic state flips: one comment post, no LLM,
    # no mirror, no run_review
    settings = Settings(
        llm_api_key="sk", llm_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        bitbucket_webhook_secret=None, port=8420)

    def boom(*args, **kwargs):
        raise AssertionError("run_review must not run for a command request")
    monkeypatch.setattr("hone.cli.run_review", boom)

    posted = []

    class FakeBitbucket:
        def post_comment(self, repo, pr_id, markdown):
            posted.append((repo, pr_id, markdown))

    process = build_process(settings, forges={"bitbucket": FakeBitbucket()},
                            repo_managers={"bitbucket": object()})
    process(CommandRequest(workspace="ws", repo="repo", pr_id=9,
                           action="mute"))
    process(CommandRequest(workspace="ws", repo="repo", pr_id=9,
                           action="unmute"))

    (mute_repo, mute_pr, mute_text), (_, _, un_text) = posted
    assert (mute_repo, mute_pr) == ("repo", 9)
    assert mute_text.startswith("<!-- hone:muted -->")
    assert un_text.startswith("<!-- hone:unmuted -->")
