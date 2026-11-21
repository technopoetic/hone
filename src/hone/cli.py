"""hone CLI. `review` does one PR by URL, posting only when --dry-run is
absent; `serve` runs the webhook server, which is live-only by design."""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable

from dotenv import load_dotenv

from .bitbucket import BitbucketClient
from .config import (
    ConfigError,
    Settings,
    configure_logging,
    load_settings,
    resolve_static_checks,
)
from .forge import Forge
from .github import GitHubClient
from .gitlab import GitLabClient
from .markers import control_comment
from .repo import RepoManager
from .review import ReviewJob, run_review
from .server import (
    CommandRequest,
    ReviewRequest,
    serve,
    validate_serve_webhooks,
)

_log = logging.getLogger("hone")

_BITBUCKET_PR_URL = re.compile(
    r"https?://bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)$")
_GITHUB_PR_URL = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)$")


def parse_pr_url(url: str, gitlab_url: str | None = None
                 ) -> tuple[str, str, str, int]:
    """(forge, namespace, repo, pr_id). `namespace` is Bitbucket's
    workspace, GitHub's owner, or GitLab's (possibly nested) group path —
    same role, different forge's word.

    GitLab recognition requires the CONFIGURED base URL: an
    arbitrary/unconfigured GitLab-shaped host is not a supported URL.
    The dynamic pattern is an exact full match, so a lookalike base path
    (`gitlab-evil` vs `gitlab`), a `..` namespace segment, or an extra
    suffix after the MR number all fall through to the ValueError."""
    url = url.strip()
    m = _BITBUCKET_PR_URL.fullmatch(url)
    if m:
        return "bitbucket", m.group(1), m.group(2), int(m.group(3))
    m = _GITHUB_PR_URL.fullmatch(url)
    if m:
        return "github", m.group(1), m.group(2), int(m.group(3))
    if gitlab_url:
        pattern = (
            re.escape(gitlab_url.rstrip("/"))
            + r"/(.+)/([^/]+)/-/merge_requests/(\d+)")
        m = re.fullmatch(pattern, url)
        if m:
            namespace = m.group(1)
            segments = namespace.split("/")
            if all(segment not in {"", ".", ".."} for segment in segments):
                return "gitlab", namespace, m.group(2), int(m.group(3))
    raise ValueError(
        f"not a supported Bitbucket, GitHub, or GitLab URL: {url}")


def _gitlab_repos(settings: Settings, data_dir) -> RepoManager:
    # Own data_dir subtree, same collision argument as _github_repos
    # below: mirrors are keyed only by namespace/repo (repo.py), so a
    # repo with the same name under the same handle on two forges would
    # otherwise collide on one mirror and fetch from the wrong remote.
    #
    # gitlab_url keeps its normalized form (trailing slash stripped, and
    # self-managed path prefixes kept), so plain concatenation builds the
    # clone URL; nested namespaces are fine as path material since
    # RepoManager confines paths under its data_dir.
    #
    # GitLab's documented convention for PAT-over-git-HTTPS: username
    # "oauth2", the token as the password.
    return RepoManager(
        data_dir=data_dir / "gitlab", username="oauth2",
        api_key=settings.gitlab_token,
        remote_url_for=lambda namespace, repo: (
            f"{settings.gitlab_url}/{namespace}/{repo}.git"))


def _github_repos(settings: Settings, data_dir) -> RepoManager:
    # Own subdirectory, not the bare data_dir: mirrors are keyed only by
    # namespace/repo (repo.py), so a repo with the same name under the
    # same handle on both forges (e.g. "technopoetic/hone" on both) would
    # otherwise collide on one mirror and fetch from the wrong remote.
    #
    # GitHub's documented convention for PAT-over-git-HTTPS: any non-empty
    # username, the PAT as the password.
    return RepoManager(
        data_dir=data_dir / "github", username="x-access-token",
        api_key=settings.github_token,
        remote_url_for=lambda ns, repo: f"https://github.com/{ns}/{repo}.git")


def build_process(settings: Settings, *, forges: dict[str, Forge],
                  repo_managers: dict[str, RepoManager]
                  ) -> Callable[[ReviewRequest | CommandRequest], None]:
    """Turn a ReviewRequest/CommandRequest into one worker action.
    `forges`/`repo_managers` are keyed by `req.forge` — whichever forges
    are configured for this process.

    Review requests become one live (never dry-run) run_review call — the
    server has no dry-run mode, posting is its only behavior. Command
    requests are deterministic state flips: post the matching marker
    comment, done. run_review failures are left to propagate — run_worker
    catches and logs them so one bad job never kills the server.
    """
    def process(req: ReviewRequest | CommandRequest) -> None:
        forge = forges[req.forge]
        if isinstance(req, CommandRequest):
            _log.info("command %s for %s/%s#%d", req.action, req.workspace,
                      req.repo, req.pr_id)
            forge.post_comment(req.repo, req.pr_id,
                               control_comment(req.action))
            return
        job = ReviewJob(workspace=req.workspace, repo=req.repo,
                        pr_id=req.pr_id, dry_run=False, force=req.force,
                        model=settings.llm_model,
                        static_checks=resolve_static_checks(
                            settings, req.repo),
                        max_turns=settings.max_turns)
        run_review(job, forge=forge, repos=repo_managers[req.forge],
                   api_key=settings.llm_api_key,
                   base_url=settings.llm_base_url)

    return process


def _serve() -> int:
    try:
        settings = load_settings()
        # Serve is live-only: with no enabled forge holding a usable
        # webhook secret it could never post and would only accept
        # unsigned webhooks — refuse as a config error, in the house
        # error shape, BEFORE logging/clients/sweep leave side effects on
        # a boot that cannot proceed. serve()'s own validation stays as
        # defense in depth.
        validate_serve_webhooks(settings)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # configure_logging is not idempotent — call exactly once per process.
    log_path = configure_logging(settings.data_dir)
    _log.info("logging to %s", log_path)

    # Only enabled forges get clients: a disabled forge's route is never
    # mounted (see serve), so a client for it could never receive a job.
    forges: dict[str, Forge] = {}
    repo_managers: dict[str, RepoManager] = {}
    if settings.bitbucket_api_key:
        # No URL to parse a workspace from on this path: the server is
        # pinned to the configured workspace (the route rejects any
        # other), so the client is pinned to it too.
        forges["bitbucket"] = BitbucketClient(
            email=settings.bitbucket_email,
            api_key=settings.bitbucket_api_key,
            workspace=settings.bitbucket_workspace)
        repo_managers["bitbucket"] = RepoManager(
            data_dir=settings.data_dir,
            username=settings.bitbucket_username,
            api_key=settings.bitbucket_api_key)
    if settings.github_token:
        forges["github"] = GitHubClient(token=settings.github_token,
                                         owner=settings.github_owner)
        repo_managers["github"] = _github_repos(settings, settings.data_dir)
    if settings.gitlab_token:
        # Pinned to the CONFIGURED namespace like the Bitbucket client
        # above: the server has no URL to parse one from, and the GitLab
        # route that lands in Task 6 is pinned to the same namespace. The
        # mirror manager gets the isolated gitlab subtree (see
        # _gitlab_repos) and therefore its own sweep in the loop below.
        forges["gitlab"] = GitLabClient(
            token=settings.gitlab_token,
            base_url=settings.gitlab_url,
            namespace=settings.gitlab_namespace)
        repo_managers["gitlab"] = _gitlab_repos(settings, settings.data_dir)

    # One sweep per boot: the only safe moment is process start, before the
    # worker can be mid-review. Best-effort: cleanup failing must warn,
    # never block startup. Each forge's RepoManager now has its own
    # data_dir subtree (see _github_repos), so each needs its own sweep.
    removed = 0
    for mgr in repo_managers.values():
        try:
            removed += mgr.sweep_stale_worktrees()
        except Exception as e:
            print(f"warning: worktree sweep failed (continuing): {e}",
                  file=sys.stderr)
    if removed:
        _log.info("swept %d stale worktree(s) from previous runs", removed)

    process = build_process(settings, forges=forges,
                            repo_managers=repo_managers)
    serve(settings, process_factory=lambda: process)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="hone",
        description="Autonomous PR review agent (Bitbucket, GitHub, GitLab)")
    sub = parser.add_subparsers(dest="command", required=True)
    rev = sub.add_parser("review", help="review one pull request")
    rev.add_argument("url", help="full URL of the pull request")
    rev.add_argument("--dry-run", action="store_true",
                     help="run the review, print it, never post a comment")
    sub.add_parser("serve", help="run the webhook server (posts reviews; "
                                 "no dry-run mode)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve()

    # Settings load BEFORE the URL parses: GitLab recognition is tied to
    # the configured base URL (settings.gitlab_url), so the URL alone
    # cannot decide the forge anymore.
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        forge_name, namespace, repo, pr_id = parse_pr_url(
            args.url, gitlab_url=settings.gitlab_url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if forge_name == "github" and not settings.github_token:
        print("error: GITHUB_TOKEN and GITHUB_OWNER are required to "
              "review a GitHub PR", file=sys.stderr)
        return 2
    if forge_name == "bitbucket" and not settings.bitbucket_api_key:
        # bitbucket credentials are optional now; a Bitbucket URL with no
        # Bitbucket group must fail like the GitHub gate above, not with
        # a confusing downstream 401
        print("error: BITBUCKET_EMAIL, BITBUCKET_API_KEY, and "
              "BITBUCKET_WORKSPACE are required to review a Bitbucket PR",
              file=sys.stderr)
        return 2
    if forge_name == "gitlab" and not settings.gitlab_token:
        # defensive: load_settings already refuses a partial GitLab
        # group, so this only fires for directly-constructed Settings
        print("error: GITLAB_TOKEN, GITLAB_URL, and GITLAB_NAMESPACE are "
              "required to review a GitLab merge request", file=sys.stderr)
        return 2

    # Same INFO stream to the console as before, plus a rotating file under
    # data_dir/logs so unattended serve runs leave an audit trail.
    log_path = configure_logging(settings.data_dir)
    _log.info("logging to %s", log_path)

    if forge_name == "github":
        forge: Forge = GitHubClient(token=settings.github_token,
                                    owner=namespace)
        repos = _github_repos(settings, settings.data_dir)
    elif forge_name == "gitlab":
        # Like the GitHub branch: the client is pinned to the URL's
        # parsed namespace (which may nest, group/sub), and mirrors live
        # in the isolated gitlab subtree.
        forge = GitLabClient(token=settings.gitlab_token,
                             base_url=settings.gitlab_url,
                             namespace=namespace)
        repos = _gitlab_repos(settings, settings.data_dir)
    else:
        forge = BitbucketClient(email=settings.bitbucket_email,
                                api_key=settings.bitbucket_api_key,
                                workspace=namespace)
        repos = RepoManager(data_dir=settings.data_dir,
                            username=settings.bitbucket_username,
                            api_key=settings.bitbucket_api_key)
    # Sweep orphaned job worktrees left by hard kills — only safe now, at
    # process start, when no review job can be running. Best-effort even at
    # this level: cleanup failing must warn, never block the review.
    try:
        removed = repos.sweep_stale_worktrees()
    except Exception as e:
        print(f"warning: worktree sweep failed (continuing): {e}",
              file=sys.stderr)
        removed = 0
    if removed:
        _log.info("swept %d stale worktree(s) from previous runs", removed)
    job = ReviewJob(workspace=namespace, repo=repo, pr_id=pr_id,
                    dry_run=args.dry_run, model=settings.llm_model,
                    static_checks=resolve_static_checks(settings, repo),
                    max_turns=settings.max_turns)
    try:
        result = run_review(job, forge=forge, repos=repos,
                            api_key=settings.llm_api_key,
                            base_url=settings.llm_base_url)
    except Exception as e:  # fail open: log, exit nonzero, nothing posted
        print(f"review failed (nothing posted): {e}", file=sys.stderr)
        return 1

    if result.skipped:
        print("already reviewed this head commit — nothing to do")
        return 0
    if args.dry_run and result.final_text:
        print(result.final_text)
    if not result.posted:
        print("warning: no review was posted (see logs)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
