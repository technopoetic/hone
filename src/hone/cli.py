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
from .markers import control_comment
from .repo import RepoManager
from .review import ReviewJob, run_review
from .server import CommandRequest, ReviewRequest, serve

_log = logging.getLogger("hone")

_PR_URL = re.compile(
    r"https?://bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)$")


def parse_pr_url(url: str) -> tuple[str, str, int]:
    m = _PR_URL.fullmatch(url.strip())
    if not m:
        raise ValueError(f"not a Bitbucket PR URL: {url}")
    return m.group(1), m.group(2), int(m.group(3))


def build_process(settings: Settings, bitbucket: BitbucketClient,
                  repos: RepoManager) -> Callable[[ReviewRequest
                                                   | CommandRequest], None]:
    """Turn a ReviewRequest/CommandRequest into one worker action.

    Review requests become one live (never dry-run) run_review call — the
    server has no dry-run mode, posting is its only behavior. Command
    requests are deterministic state flips: post the matching marker
    comment, done. run_review failures are left to propagate — run_worker
    catches and logs them so one bad job never kills the server.
    """
    def process(req: ReviewRequest | CommandRequest) -> None:
        if isinstance(req, CommandRequest):
            _log.info("command %s for %s/%s#%d", req.action, req.workspace,
                      req.repo, req.pr_id)
            bitbucket.post_comment(req.repo, req.pr_id,
                                   control_comment(req.action))
            return
        job = ReviewJob(workspace=req.workspace, repo=req.repo,
                        pr_id=req.pr_id, dry_run=False, force=req.force,
                        model=settings.synthetic_model,
                        static_checks=resolve_static_checks(
                            settings, req.repo),
                        max_turns=settings.max_turns)
        run_review(job, bitbucket=bitbucket, repos=repos,
                   api_key=settings.synthetic_api_key)

    return process


def _serve() -> int:
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Serve is live-only: without the webhook secret it could never post and
    # would only accept unsigned webhooks — refuse as a config error, in the
    # house error shape, BEFORE logging/clients/sweep leave side effects on a
    # boot that cannot proceed. serve()'s own check stays as defense in depth.
    if not settings.webhook_secret:
        print("error: HONE_WEBHOOK_SECRET is required for serve",
              file=sys.stderr)
        return 2

    # configure_logging is not idempotent — call exactly once per process.
    log_path = configure_logging(settings.data_dir)
    _log.info("logging to %s", log_path)

    # No URL to parse a workspace from on this path: the server is pinned
    # to the configured workspace (the route rejects any other), so the
    # client is pinned to it too.
    bitbucket = BitbucketClient(email=settings.bitbucket_email,
                                api_key=settings.bitbucket_api_key,
                                workspace=settings.bitbucket_workspace)
    repos = RepoManager(data_dir=settings.data_dir,
                        username=settings.bitbucket_username,
                        api_key=settings.bitbucket_api_key)
    # One sweep per boot: the only safe moment is process start, before the
    # worker can be mid-review. Best-effort: cleanup failing must warn,
    # never block startup.
    try:
        removed = repos.sweep_stale_worktrees()
    except Exception as e:
        print(f"warning: worktree sweep failed (continuing): {e}",
              file=sys.stderr)
        removed = 0
    if removed:
        _log.info("swept %d stale worktree(s) from previous runs", removed)

    process = build_process(settings, bitbucket, repos)
    serve(settings, process_factory=lambda: process)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        prog="hone",
        description="Autonomous Bitbucket PR review agent")
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

    try:
        workspace, repo, pr_id = parse_pr_url(args.url)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        settings = load_settings()
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # Same INFO stream to the console as before, plus a rotating file under
    # data_dir/logs so unattended serve runs leave an audit trail.
    log_path = configure_logging(settings.data_dir)
    _log.info("logging to %s", log_path)

    bitbucket = BitbucketClient(email=settings.bitbucket_email,
                                api_key=settings.bitbucket_api_key,
                                workspace=workspace)
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
    job = ReviewJob(workspace=workspace, repo=repo, pr_id=pr_id,
                    dry_run=args.dry_run, model=settings.synthetic_model,
                    static_checks=resolve_static_checks(settings, repo),
                    max_turns=settings.max_turns)
    try:
        result = run_review(job, bitbucket=bitbucket, repos=repos,
                            api_key=settings.synthetic_api_key)
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
