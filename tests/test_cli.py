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


def test_parse_valid_url():
    ws, repo, pr_id = parse_pr_url(
        "https://bitbucket.org/hmdatasolutions/mekanik/pull-requests/12")
    assert (ws, repo, pr_id) == ("hmdatasolutions", "mekanik", 12)


@pytest.mark.parametrize("bad", [
    "https://github.com/x/y/pull/12",
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

    def fake_run_review(job, *, bitbucket, repos, api_key):
        calls.append((job, bitbucket, repos, api_key))
        return "result"

    monkeypatch.setattr("hone.cli.run_review", fake_run_review)

    bb, repos = object(), object()
    process = build_process(settings, bb, repos)
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
    process = build_process(settings, object(), object())
    process(ReviewRequest(workspace="ws", repo="repo", pr_id=9, force=True))

    (job,) = calls
    assert job.force is True


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

    process = build_process(settings, FakeBitbucket(), object())
    process(CommandRequest(workspace="ws", repo="repo", pr_id=9,
                           action="mute"))
    process(CommandRequest(workspace="ws", repo="repo", pr_id=9,
                           action="unmute"))

    (mute_repo, mute_pr, mute_text), (_, _, un_text) = posted
    assert (mute_repo, mute_pr) == ("repo", 9)
    assert mute_text.startswith("<!-- hone:muted -->")
    assert un_text.startswith("<!-- hone:unmuted -->")
