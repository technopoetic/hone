import pytest

from hone.cli import build_process, main, parse_pr_url
from hone.config import Settings
from hone.server import CommandRequest, ReviewRequest


def _settings(tmp_path, **overrides):
    base = dict(
        synthetic_api_key="sk", synthetic_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path / "data",
        tool_commands={"ruff": ("ruff", "check", ".")},
        repo_checks={"repo": ("ruff",)},
        max_turns=7, webhook_secret=None, port=8420)
    return Settings(**{**base, **overrides})


def test_serve_without_webhook_secret_is_a_config_error(tmp_path, monkeypatch,
                                                        capsys):
    # serve's only mode is posting, so a missing secret is a startup config
    # error like any other: house error shape, exit 2, BEFORE logging/clients/
    # sweep leave side effects on a boot that can never proceed
    monkeypatch.setattr("hone.cli.load_settings",
                        lambda: _settings(tmp_path, webhook_secret=None))
    monkeypatch.setattr("hone.cli.load_dotenv", lambda: None)

    rc = main(["serve"])

    assert rc == 2
    assert "error: HONE_WEBHOOK_SECRET is required for serve" in (
        capsys.readouterr().err)
    # failed before any startup side effect: no log dir created
    assert not (tmp_path / "data" / "logs").exists()


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


def test_build_process_runs_live_reviews(tmp_path, monkeypatch):
    # the serve seam: a ReviewRequest in, one live (never dry-run) run_review
    # out, with the job fields taken from settings, not the request
    settings = Settings(
        synthetic_api_key="sk", synthetic_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={"ruff": ("ruff", "check", ".")},
        repo_checks={"repo": ("ruff",)},
        max_turns=7, webhook_secret=None, port=8420)

    calls = []

    def fake_run_review(job, *, forge, repos, api_key):
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
        synthetic_api_key="sk", synthetic_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        webhook_secret=None, port=8420)
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
        synthetic_api_key="sk", synthetic_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        webhook_secret=None, port=8420)

    calls = []
    monkeypatch.setattr(
        "hone.cli.run_review",
        lambda job, **kw: calls.append(kw) or "result")

    bb, bb_repos = object(), object()
    gh, gh_repos = object(), object()
    process = build_process(
        settings, forges={"bitbucket": bb, "github": gh},
        repo_managers={"bitbucket": bb_repos, "github": gh_repos})

    process(ReviewRequest(workspace="ws", repo="repo", pr_id=1,
                          forge="github"))
    process(ReviewRequest(workspace="ws", repo="repo", pr_id=2,
                          forge="bitbucket"))

    assert calls[0]["forge"] is gh and calls[0]["repos"] is gh_repos
    assert calls[1]["forge"] is bb and calls[1]["repos"] is bb_repos


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


def test_command_requests_post_marker_comments_and_never_review(
        tmp_path, monkeypatch):
    # mute/unmute are deterministic state flips: one comment post, no LLM,
    # no mirror, no run_review
    settings = Settings(
        synthetic_api_key="sk", synthetic_model="m", bitbucket_email="e",
        bitbucket_username="u", bitbucket_api_key="k",
        bitbucket_workspace="ws", data_dir=tmp_path,
        tool_commands={}, repo_checks={}, max_turns=7,
        webhook_secret=None, port=8420)

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
