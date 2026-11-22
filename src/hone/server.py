"""Webhook receiver (Bitbucket + GitHub + GitLab). Accept fast, enqueue,
return 200 — the worker thread owes no forge anything."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from .config import ConfigError, Settings

log = logging.getLogger("hone")

_REVIEW_EVENTS = {"pullrequest:created", "pullrequest:updated"}
# comment_created carries the human control plane: @hone commands
_ALL_EVENTS = _REVIEW_EVENTS | {"pullrequest:comment_created"}

# GitHub's equivalents: opened/synchronize/reopened trigger a review the
# same way Bitbucket's created/updated do; issue_comment is GitHub's event
# for a top-level PR comment (there is no separate "pull request comment"
# event — PRs are issues as far as the Comments API is concerned).
_GITHUB_REVIEW_ACTIONS = {"opened", "synchronize", "reopened"}
_GITHUB_PR_EVENT = "pull_request"
_GITHUB_COMMENT_EVENT = "issue_comment"

# GitLab 19.1+ sends these exact X-Gitlab-Event values and signs with the
# Standard Webhooks scheme (see _valid_gitlab): raw-body HMAC-SHA256 over
# "<webhook-id>.<webhook-timestamp>.<body>", a whsec_-prefixed base64 key,
# and space-separated versioned signatures. Plain X-Gitlab-Token is never
# an authentication substitute.
_GITLAB_MR_EVENT = "Merge Request Hook"
_GITLAB_NOTE_EVENT = "Note Hook"
# An "update" can be metadata rather than a new head; still intentional to
# enqueue — the pre-mirror marker scan skips an unchanged head for free.
_GITLAB_REVIEW_ACTIONS = {"open", "reopen", "update"}

# hone's own comments all carry one of these markers; the sentinel stops
# the agent reacting to its own posts (it posts as the owner's account, so
# the author gate alone cannot distinguish its output from a human command)
_OWN_MARKER_SENTINEL = "<!-- hone:"

# Literal-match contract: the comment text, stripped and case-folded, must
# BE the command. A phrase inside prose is never a command — comment bodies
# are untrusted data and are never interpreted by the LLM.
_COMMANDS = {
    "@hone mute": "mute",
    "@hone unmute": "unmute",
    "@hone review": "review",
}


@dataclass(frozen=True)
class ReviewRequest:
    # "workspace" doubles as GitHub's "owner" — same role (the namespace a
    # repo lives under), different forge's word for it.
    workspace: str
    repo: str
    pr_id: int
    # @hone review: bypass mute state and the same-head dedup marker.
    force: bool = False
    forge: str = "bitbucket"


@dataclass(frozen=True)
class CommandRequest:
    """A control-plane command the worker executes by posting the matching
    marker comment. `action` is "mute" or "unmute"."""
    workspace: str
    repo: str
    pr_id: int
    action: str
    forge: str = "bitbucket"


def _route_command(text: str, actor_id: str | int | None,
                   author_id: str | int | None, *, forge: str,
                   workspace: str, repo: str, pr_id: int
                   ) -> ReviewRequest | CommandRequest | None:
    """Shared trust gate for both forges' comment-command surface, so a
    future change to either rule can't accidentally apply to only one
    forge. AGENTS.md is explicit that neither may be relaxed: the comment
    text must exactly BE the command (bodies are untrusted data, never
    fed to the LLM as instructions), and only the PR author's own identity
    (fail closed on a missing one) may issue one."""
    if _OWN_MARKER_SENTINEL in text:
        return None
    action = _COMMANDS.get(text.strip().casefold())
    if action is None:
        return None
    if not actor_id or actor_id != author_id:
        log.warning("comment command %r on pr #%d rejected: commenter is "
                    "not the PR author", action, pr_id)
        return None
    if action == "review":
        return ReviewRequest(workspace=workspace, repo=repo, pr_id=pr_id,
                             force=True, forge=forge)
    return CommandRequest(workspace=workspace, repo=repo, pr_id=pr_id,
                          action=action, forge=forge)


def _command_from_comment(payload: dict, *, workspace: str, repo: str,
                          pr_id: int) -> ReviewRequest | CommandRequest | None:
    """Route one Bitbucket comment_created payload."""
    comment = payload.get("comment") or {}
    text = (comment.get("content") or {}).get("raw") or ""
    # Compare Bitbucket uuids (stable across renames, unlike display names).
    actor = (payload.get("actor") or {}).get("uuid")
    author = ((payload.get("pullrequest") or {}).get("author")
              or {}).get("uuid")
    return _route_command(text, actor, author, forge="bitbucket",
                          workspace=workspace, repo=repo, pr_id=pr_id)


def _github_command_from_comment(payload: dict, *, owner: str, repo: str,
                                 pr_id: int
                                 ) -> ReviewRequest | CommandRequest | None:
    """Route one GitHub issue_comment payload (already confirmed to carry
    an `issue.pull_request` key, i.e. it's a comment on a PR, not a plain
    issue)."""
    text = (payload.get("comment") or {}).get("body") or ""
    # Compare GitHub's numeric user ids (stable across username renames).
    actor = (payload.get("comment") or {}).get("user", {}).get("id")
    author = (payload.get("issue") or {}).get("user", {}).get("id")
    return _route_command(text, actor, author, forge="github",
                          workspace=owner, repo=repo, pr_id=pr_id)


def _gitlab_command_from_comment(payload: dict, *, workspace: str,
                                 repo: str, pr_id: int
                                 ) -> ReviewRequest | CommandRequest | None:
    """Route one GitLab Note Hook payload (already filtered to a newly
    created, plain, top-level MR note — nothing here is a command unless
    the shared gate says so)."""
    attributes = payload.get("object_attributes") or {}
    merge_request = payload.get("merge_request") or {}
    return _route_command(
        attributes.get("note") or "", attributes.get("author_id"),
        merge_request.get("author_id"), forge="gitlab",
        workspace=workspace, repo=repo, pr_id=pr_id)


def _valid(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare bytes, not str: starlette decodes header values as latin-1,
    # so obs-text reaches here as a non-ASCII str and compare_digest
    # raises TypeError on that — a 500 where the spec wants a plain 403.
    return hmac.compare_digest(expected.encode(),
                               header.removeprefix("sha256=").encode())


def _valid_gitlab(secret: str, body: bytes, message_id: str | None,
                  timestamp: str | None, signatures: str | None) -> bool:
    """Standard Webhooks verification — a total function: every missing,
    malformed, stale, or non-ASCII input returns False rather than
    raising, so authentication can never surface as a 500."""
    try:
        if not message_id or not timestamp or not signatures:
            return False
        if not secret.startswith("whsec_"):
            return False
        timestamp_number = int(timestamp)
        if abs(time.time() - timestamp_number) > 300:
            return False
        key = base64.b64decode(secret[6:], validate=True)
        if not key:
            return False
        prefix = f"{message_id}.{timestamp}.".encode("ascii")
    except (ValueError, UnicodeError, binascii.Error):
        return False

    digest = hmac.new(key, prefix + body, hashlib.sha256).digest()
    expected = base64.b64encode(digest)
    for candidate in signatures.split():
        version, separator, encoded = candidate.partition(",")
        if separator and version == "v1" and hmac.compare_digest(
                expected, encoded.encode()):
            return True
    return False


def create_app(
    enqueue: Callable[[ReviewRequest | CommandRequest], None],
    *,
    bitbucket_secret: str | None = None,
    bitbucket_workspace: str | None = None,
    github_secret: str | None = None,
    github_owner: str | None = None,
    gitlab_signing_token: str | None = None,
    gitlab_namespace: str | None = None,
) -> FastAPI:
    """Build the webhook app. `enqueue` is injected so tests (and any
    future wiring) can swap the real queue for a sink of their choosing.
    Each route mounts only when its forge's secret AND namespace arg are
    both given — same "no entry = off" safe default repos.toml already
    uses, rather than a route that exists but can never validate
    anything."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> PlainTextResponse:
        # deployment convention: mekanik's staging health checks hit /health
        return PlainTextResponse('{"status":"ok"}')

    if bitbucket_secret and bitbucket_workspace:
        @app.post("/webhook/bitbucket")
        async def webhook_bitbucket(request: Request) -> PlainTextResponse:
            # Signature is over the RAW bytes: read the body before parsing
            # any JSON, or a parse/re-serialize gap could smuggle a different
            # payload past the HMAC.
            body = await request.body()
            if not _valid(bitbucket_secret, body,
                          request.headers.get("x-hub-signature")):
                log.warning("webhook rejected: bad signature")
                return PlainTextResponse("forbidden", status_code=403)
            event = request.headers.get("x-event-key", "")
            if event not in _ALL_EVENTS:
                return PlainTextResponse("ignored", status_code=200)
            try:
                payload = await request.json()
                workspace, _, slug = (
                    payload["repository"]["full_name"].partition("/"))
                pr_id = payload["pullrequest"]["id"]
            except (KeyError, TypeError, ValueError, AttributeError):
                # Malformed JSON or a missing/mis-shaped repository/pullrequest
                # from a signature holder is misconfiguration, not an attack
                # to refuse — fail open with 200 so Bitbucket doesn't redeliver.
                log.warning("unparseable webhook payload (event %r) ignored",
                            event)
                return PlainTextResponse("ignored", status_code=200)
            if workspace != bitbucket_workspace:
                # The payload names its own repo; pin it to ours or a forged
                # payload could clone arbitrary repos with our credentials.
                # Fail soft — a misconfigured webhook must not trip Bitbucket
                # redelivery retries.
                log.warning("webhook for foreign workspace %r ignored",
                            workspace)
                return PlainTextResponse("ignored", status_code=200)
            if event == "pullrequest:comment_created":
                command = _command_from_comment(payload, workspace=workspace,
                                                repo=slug, pr_id=pr_id)
                if command is None:
                    return PlainTextResponse("ignored", status_code=200)
                enqueue(command)
                return PlainTextResponse("queued", status_code=200)
            # Deliberately outside the shape guard: an enqueue failure
            # (bounded or disk-backed queue in a future phase) deserves a
            # real 500 so Bitbucket redelivers, and must not be mislabeled
            # as unparseable.
            enqueue(ReviewRequest(workspace=workspace, repo=slug,
                                  pr_id=pr_id))
            return PlainTextResponse("queued", status_code=200)

    if github_secret and github_owner:
        @app.post("/webhook/github")
        async def webhook_github(request: Request) -> PlainTextResponse:
            body = await request.body()
            if not _valid(github_secret, body,
                          request.headers.get("x-hub-signature-256")):
                log.warning("github webhook rejected: bad signature")
                return PlainTextResponse("forbidden", status_code=403)
            event = request.headers.get("x-github-event", "")
            if event not in (_GITHUB_PR_EVENT, _GITHUB_COMMENT_EVENT):
                return PlainTextResponse("ignored", status_code=200)
            try:
                payload = await request.json()
                owner, _, slug = (
                    payload["repository"]["full_name"].partition("/"))
            except (KeyError, TypeError, ValueError, AttributeError):
                log.warning("unparseable github webhook payload "
                            "(event %r) ignored", event)
                return PlainTextResponse("ignored", status_code=200)
            if owner != github_owner:
                # Same reasoning as the Bitbucket workspace pin above: a
                # forged payload must not pick the clone target.
                log.warning("github webhook for foreign owner %r ignored",
                            owner)
                return PlainTextResponse("ignored", status_code=200)
            if event == _GITHUB_COMMENT_EVENT:
                try:
                    # issue_comment also fires on edited/deleted, unlike
                    # Bitbucket's create-only comment_created — a deleted
                    # payload still carries the pre-delete body, so without
                    # this gate, deleting (or editing into) an exact
                    # command string would re-fire it.
                    if payload.get("action") != "created":
                        return PlainTextResponse("ignored", status_code=200)
                    # issue_comment fires for plain issues too; this key is
                    # how GitHub marks "this issue is actually a PR".
                    if not payload["issue"].get("pull_request"):
                        return PlainTextResponse("ignored", status_code=200)
                    pr_id = payload["issue"]["number"]
                except (KeyError, TypeError, AttributeError):
                    log.warning("unparseable github comment payload ignored")
                    return PlainTextResponse("ignored", status_code=200)
                command = _github_command_from_comment(
                    payload, owner=owner, repo=slug, pr_id=pr_id)
                if command is None:
                    return PlainTextResponse("ignored", status_code=200)
                enqueue(command)
                return PlainTextResponse("queued", status_code=200)
            try:
                if payload.get("action") not in _GITHUB_REVIEW_ACTIONS:
                    return PlainTextResponse("ignored", status_code=200)
                pr_id = payload["pull_request"]["number"]
            except (KeyError, TypeError, AttributeError):
                log.warning("unparseable github pull_request payload ignored")
                return PlainTextResponse("ignored", status_code=200)
            enqueue(ReviewRequest(workspace=owner, repo=slug, pr_id=pr_id,
                                  forge="github"))
            return PlainTextResponse("queued", status_code=200)

    if gitlab_signing_token and gitlab_namespace:
        @app.post("/webhook/gitlab")
        async def webhook_gitlab(request: Request) -> PlainTextResponse:
            # Same rule as the other forges: sign the RAW bytes before any
            # parsing, or a parse/re-serialize gap could smuggle a
            # different payload past the HMAC.
            body = await request.body()
            if not _valid_gitlab(
                    gitlab_signing_token, body,
                    request.headers.get("webhook-id"),
                    request.headers.get("webhook-timestamp"),
                    request.headers.get("webhook-signature")):
                log.warning("gitlab webhook rejected: bad signature")
                return PlainTextResponse("forbidden", status_code=403)
            event = request.headers.get("x-gitlab-event", "")
            if event not in (_GITLAB_MR_EVENT, _GITLAB_NOTE_EVENT):
                return PlainTextResponse("ignored", status_code=200)
            try:
                payload = await request.json()
                # GitLab namespaces nest, so the LAST slash splits
                # "group/sub/project" into namespace "group/sub" and slug.
                full_path = payload["project"]["path_with_namespace"]
                namespace, separator, slug = full_path.rpartition("/")
                if not separator or not namespace or not slug:
                    raise ValueError("invalid GitLab project path")
            except (KeyError, TypeError, ValueError, AttributeError):
                # Same fail-open contract as the other forges: a
                # signature holder sending misconfiguration is not an
                # attack, and a 200 stops GitLab's redeliveries.
                log.warning("unparseable gitlab webhook payload "
                            "(event %r) ignored", event)
                return PlainTextResponse("ignored", status_code=200)
            if not (namespace == gitlab_namespace
                    or namespace.startswith(gitlab_namespace + "/")):
                # Same pin as the other forges: the payload names its own
                # repo, so a forged one must not pick the clone target —
                # descendants of the configured namespace ARE ours.
                log.warning("gitlab webhook for foreign namespace %r "
                            "ignored", namespace)
                return PlainTextResponse("ignored", status_code=200)
            if event == _GITLAB_NOTE_EVENT:
                # Eligibility sits INSIDE the shape guard, like GitHub's
                # comment path: a signature holder can send a null or
                # scalar object_attributes, and .get on that must land in
                # the guard's fail-soft 200, not a 500.
                try:
                    attributes = payload["object_attributes"]
                    pr_id = payload["merge_request"]["iid"]
                    # Only a newly created, plain, top-level MR note is
                    # the human control plane: edits re-deliver the
                    # pre-edit body, system/internal notes are GitLab's
                    # own bookkeeping, and diff-attached ones are inline
                    # review commentary (a present `position` key marks
                    # them even when null, the same key-presence rule the
                    # client's get_comments uses).
                    if (not isinstance(pr_id, int)
                            or attributes.get("action") != "create"
                            or attributes.get(
                                "noteable_type") != "MergeRequest"
                            or attributes.get("system")
                            or attributes.get("internal")
                            or "position" in attributes
                            or attributes.get("type") == "DiffNote"):
                        return PlainTextResponse("ignored", status_code=200)
                except (KeyError, TypeError, AttributeError):
                    log.warning("unparseable gitlab webhook payload "
                                "(event %r) ignored", event)
                    return PlainTextResponse("ignored", status_code=200)
                command = _gitlab_command_from_comment(
                    payload, workspace=namespace, repo=slug, pr_id=pr_id)
                if command is None:
                    return PlainTextResponse("ignored", status_code=200)
                # Deliberately outside the shape guard, like the other
                # forges: an enqueue failure is a real 500 GitLab should
                # retry, never mislabeled as an unparseable payload.
                enqueue(command)
                return PlainTextResponse("queued", status_code=200)
            try:
                attributes = payload["object_attributes"]
                pr_id = attributes["iid"]
                action = attributes["action"]
            except (KeyError, TypeError, AttributeError):
                log.warning("unparseable gitlab webhook payload "
                            "(event %r) ignored", event)
                return PlainTextResponse("ignored", status_code=200)
            if (not isinstance(pr_id, int)
                    or action not in _GITLAB_REVIEW_ACTIONS):
                return PlainTextResponse("ignored", status_code=200)
            enqueue(ReviewRequest(workspace=namespace, repo=slug,
                                  pr_id=pr_id, forge="gitlab"))
            return PlainTextResponse("queued", status_code=200)

    return app


def validate_serve_webhooks(settings: Settings) -> list[str]:
    """Return missing secrets when at least one forge can still mount.

    Raises ConfigError when no enabled forge has a usable secret: a
    single enabled forge keeps its specific error, while several enabled
    forges with none usable get one message naming every missing variable
    in configured order (bitbucket, github, gitlab). Also raises when an
    enabled forge has its secret but not its mount namespace: load_settings
    refuses partial groups, so that only guards directly constructed
    Settings — where it would otherwise silently mount a zero-route
    server despite full credentials."""
    # (has_credential, has_secret, has_namespace, secret env, namespace env)
    configured = [
        (bool(settings.bitbucket_api_key),
         bool(settings.bitbucket_webhook_secret),
         bool(settings.bitbucket_workspace),
         "BITBUCKET_WEBHOOK_SECRET", "BITBUCKET_WORKSPACE"),
        (bool(settings.github_token),
         bool(settings.github_webhook_secret),
         bool(settings.github_owner),
         "GITHUB_WEBHOOK_SECRET", "GITHUB_OWNER"),
        (bool(settings.gitlab_token),
         bool(settings.gitlab_webhook_signing_token),
         bool(settings.gitlab_namespace),
         "GITLAB_WEBHOOK_SIGNING_TOKEN", "GITLAB_NAMESPACE"),
    ]
    enabled = [entry for entry in configured if entry[0]]
    missing = [env for _, has_secret, _, env, _ in enabled if not has_secret]
    if not enabled or len(missing) == len(enabled):
        if len(enabled) == 1:
            message = f"{missing[0]} is required for serve"
        elif missing:
            message = ("no forge has a webhook secret configured for "
                       "serve: " + ", ".join(missing))
        else:
            # only reachable from direct calls: load_settings already
            # refuses an environment with no forge credentials at all
            message = "no forge has a webhook secret configured for serve"
        raise ConfigError(message)
    unmountable = [namespace_env
                   for _, has_secret, has_namespace, _, namespace_env
                   in enabled if has_secret and not has_namespace]
    if unmountable:
        raise ConfigError(
            "webhook secret set without its mount namespace: "
            + ", ".join(unmountable))
    return missing


# Each missing secret means exactly this route stays unmounted — used by
# serve to name it in the startup warning.
_UNMOUNTED_ROUTES = {
    "BITBUCKET_WEBHOOK_SECRET": "/webhook/bitbucket",
    "GITHUB_WEBHOOK_SECRET": "/webhook/github",
    "GITLAB_WEBHOOK_SIGNING_TOKEN": "/webhook/gitlab",
}


def run_worker(q: queue.Queue[ReviewRequest | CommandRequest | None],
               process: Callable[[ReviewRequest | CommandRequest], None]
               ) -> None:
    """Drain the queue serially forever. `process` is called once per job
    and must never raise out of here — fail open means the *server* never
    dies because one review did. A None item stops the loop (shutdown
    seam; serve never sends one)."""
    while True:
        req = q.get()
        try:
            if req is None:
                break
            process(req)
        except Exception:
            log.exception("review job failed for %s/%s#%d",
                          req.workspace, req.repo, req.pr_id)
        finally:
            q.task_done()


def serve(settings: Settings, *,
          process_factory: Callable[[], Callable[[ReviewRequest], None]]
          ) -> None:
    """Block on uvicorn until the process dies.

    Logging is the caller's job: configure_logging() is not idempotent
    (a second call stacks duplicate root handlers), so serve() never
    calls it — call it exactly once before serve() if file logging is
    wanted. Raises SystemExit when no enabled forge has a usable webhook
    secret: a server that accepts unsigned webhooks is not a mode, it's
    a hole. Enabled forges missing only their secret keep the server up
    with their route unmounted (one warning each, naming the route).
    """
    import uvicorn

    try:
        missing = validate_serve_webhooks(settings)
    except ConfigError as error:
        raise SystemExit(str(error))
    for env in missing:
        log.warning("%s is not set — %s will not be mounted",
                    env, _UNMOUNTED_ROUTES[env])
    q: queue.Queue[ReviewRequest | CommandRequest | None] = queue.Queue()
    threading.Thread(target=run_worker, args=(q, process_factory()),
                     daemon=True).start()
    # Mount a route only for a forge with both credentials and a secret:
    # a route that could never validate anything is worse than a 404.
    app = create_app(
        q.put,
        bitbucket_secret=(settings.bitbucket_webhook_secret
                          if settings.bitbucket_api_key else None),
        bitbucket_workspace=settings.bitbucket_workspace,
        github_secret=(settings.github_webhook_secret
                        if settings.github_token else None),
        github_owner=settings.github_owner,
        gitlab_signing_token=(
            settings.gitlab_webhook_signing_token
            if settings.gitlab_token else None),
        gitlab_namespace=(
            settings.gitlab_namespace if settings.gitlab_token else None),
    )
    # Loopback only: nginx fronts this as the public edge on the droplet.
    # Binding a routable interface would expose the raw port — no body
    # limits in front of a handler that buffers request.body() before
    # auth, i.e. an unauthenticated memory-amplification vector.
    uvicorn.run(app, host="127.0.0.1", port=settings.port,
                log_level="warning")
