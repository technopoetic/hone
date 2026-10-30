"""One review job end to end: metadata → mirror → worktree → agent loop →
cleanup. The loop's `post_review` tool is the only path that can comment,
so --dry-run is enforced here, in one place."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .agent import make_client, run_agent
from .forge import Forge
from .markers import latest_control_state, prior_review_bodies
from .prompts import SYSTEM_PROMPT, initial_context
from .repo import RepoManager
from .tools import MARKER_PREFIX, ToolBox

log = logging.getLogger("hone")


@dataclass(frozen=True)
class ReviewJob:
    workspace: str
    repo: str
    pr_id: int
    dry_run: bool = False
    # @hone review: bypass the mute state and the same-head dedup
    # marker — an explicit human command overrides both standing skips.
    force: bool = False
    model: str = "syn:large:text"
    # tool name -> argv (None = builtin), already resolved for `repo` via
    # config.resolve_static_checks — this repo's slice of repos.toml, not
    # the whole registry.
    static_checks: dict[str, tuple[str, ...] | None] = field(
        default_factory=dict)
    max_turns: int = 10


@dataclass(frozen=True)
class ReviewResult:
    final_text: str | None
    posted: bool
    skipped: bool = False


def run_review(job: ReviewJob, *, forge: Forge,
               repos: RepoManager, api_key: str) -> ReviewResult:
    pr = forge.get_pull_request(job.repo, job.pr_id)
    # One comment fetch feeds all three standing checks: mute state, dedup,
    # and the delta prompt's previous rounds. Deliberately before
    # ensure_mirror: one paginated GET saves a fetch plus the whole agent
    # run when the PR is muted or the head was already reviewed. Dry runs
    # post nothing, so neither check protects anything there — and dry-run
    # is exactly how a human re-tests a prompt against a reviewed head.
    comments = forge.get_comments(job.repo, job.pr_id)
    if not job.dry_run and not job.force:
        if latest_control_state(comments) == "muted":
            log.info("pr #%d is muted — skipping review", job.pr_id)
            return ReviewResult(final_text=None, posted=False, skipped=True)
        marker = f"{MARKER_PREFIX}{pr.source_commit}"
        if any(marker in c for c in comments):
            log.info("review for %s already posted — skipping",
                     pr.source_commit)
            return ReviewResult(final_text=None, posted=False, skipped=True)
    previous_reviews = prior_review_bodies(comments)
    diffstat = forge.get_diffstat(job.repo, job.pr_id)
    mirror = repos.ensure_mirror(job.workspace, job.repo)
    worktree = repos.create_worktree(mirror, pr.source_commit)
    try:
        base = repos.merge_base(mirror, pr.dest_branch, pr.source_commit)

        def post(markdown: str) -> None:
            if job.dry_run:
                # the point of a dry run is to see what would be posted
                print(markdown)
                return
            forge.post_comment(job.repo, job.pr_id, markdown)

        toolbox = ToolBox(repo_manager=repos, worktree=worktree,
                          base_sha=base, head_sha=pr.source_commit,
                          static_checks=job.static_checks, post=post)
        client = make_client(api_key)
        outcome = run_agent(client, model=job.model, system=SYSTEM_PROMPT,
                            initial_context=initial_context(
                                pr, diffstat,
                                previous_reviews=previous_reviews),
                            toolbox=toolbox, max_turns=job.max_turns)
        log.info("review finished: posted=%s", outcome.posted)
        return ReviewResult(final_text=outcome.final_text,
                            posted=outcome.posted)
    finally:
        repos.remove_worktree(worktree)
