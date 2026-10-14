"""Webhook receiver. Accept fast, enqueue, return 200 — the worker thread
owes Bitbucket nothing."""

from __future__ import annotations

import hashlib
import hmac
import logging
import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from .config import Settings

log = logging.getLogger("hone")

_REVIEW_EVENTS = {"pullrequest:created", "pullrequest:updated"}
# comment_created carries the human control plane: @hone commands
_ALL_EVENTS = _REVIEW_EVENTS | {"pullrequest:comment_created"}

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
    workspace: str
    repo: str
    pr_id: int
    # @hone review: bypass mute state and the same-head dedup marker.
    force: bool = False


@dataclass(frozen=True)
class CommandRequest:
    """A control-plane command the worker executes by posting the matching
    marker comment. `action` is "mute" or "unmute"."""
    workspace: str
    repo: str
    pr_id: int
    action: str


def _command_from_comment(payload: dict, *, workspace: str, repo: str,
                          pr_id: int) -> ReviewRequest | CommandRequest | None:
    """Route one comment_created payload to its request, or None when the
    comment is not an authorized command. Authorization is exact-command
    text PLUS author identity (fail closed on missing identity)."""
    comment = payload.get("comment") or {}
    text = (comment.get("content") or {}).get("raw") or ""
    if _OWN_MARKER_SENTINEL in text:
        return None
    action = _COMMANDS.get(text.strip().casefold())
    if action is None:
        return None
    # Trust gate: only the PR author commands the agent. Compare Bitbucket
    # uuids (stable across renames, unlike display names); a missing uuid
    # means unauthorized, never authorized.
    actor = (payload.get("actor") or {}).get("uuid")
    author = ((payload.get("pullrequest") or {}).get("author")
              or {}).get("uuid")
    if not actor or actor != author:
        log.warning("comment command %r on pr #%d rejected: commenter is "
                    "not the PR author", action, pr_id)
        return None
    if action == "review":
        return ReviewRequest(workspace=workspace, repo=repo, pr_id=pr_id,
                             force=True)
    return CommandRequest(workspace=workspace, repo=repo, pr_id=pr_id,
                          action=action)


def _valid(secret: str, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare bytes, not str: starlette decodes header values as latin-1,
    # so obs-text reaches here as a non-ASCII str and compare_digest
    # raises TypeError on that — a 500 where the spec wants a plain 403.
    return hmac.compare_digest(expected.encode(),
                               header.removeprefix("sha256=").encode())


def create_app(secret: str, allowed_workspace: str,
               enqueue: Callable[[ReviewRequest | CommandRequest], None]
               ) -> FastAPI:
    """Build the webhook app. `enqueue` is injected so tests (and any
    future wiring) can swap the real queue for a sink of their choosing."""
    app = FastAPI()

    @app.get("/health")
    async def health() -> PlainTextResponse:
        # deployment convention: mekanik's staging health checks hit /health
        return PlainTextResponse('{"status":"ok"}')

    @app.post("/webhook")
    async def webhook(request: Request) -> PlainTextResponse:
        # Signature is over the RAW bytes: read the body before parsing
        # any JSON, or a parse/re-serialize gap could smuggle a different
        # payload past the HMAC.
        body = await request.body()
        if not _valid(secret, body, request.headers.get("x-hub-signature")):
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
            # from a signature holder is misconfiguration, not an attack to
            # refuse — fail open with 200 so Bitbucket doesn't redeliver.
            log.warning("unparseable webhook payload (event %r) ignored",
                        event)
            return PlainTextResponse("ignored", status_code=200)
        if workspace != allowed_workspace:
            # The payload names its own repo; pin it to ours or a forged
            # payload could clone arbitrary repos with our credentials.
            # Fail soft — a misconfigured webhook must not trip Bitbucket
            # redelivery retries.
            log.warning("webhook for foreign workspace %r ignored", workspace)
            return PlainTextResponse("ignored", status_code=200)
        if event == "pullrequest:comment_created":
            command = _command_from_comment(payload, workspace=workspace,
                                            repo=slug, pr_id=pr_id)
            if command is None:
                return PlainTextResponse("ignored", status_code=200)
            enqueue(command)
            return PlainTextResponse("queued", status_code=200)
        # Deliberately outside the shape guard: an enqueue failure (bounded
        # or disk-backed queue in a future phase) deserves a real 500 so
        # Bitbucket redelivers, and must not be mislabeled as unparseable.
        enqueue(ReviewRequest(workspace=workspace, repo=slug, pr_id=pr_id))
        return PlainTextResponse("queued", status_code=200)

    return app


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
    wanted. Raises SystemExit when no webhook secret is configured: a
    server that accepts unsigned webhooks is not a mode, it's a hole.
    """
    import uvicorn

    if not settings.webhook_secret:
        raise SystemExit("HONE_WEBHOOK_SECRET is required for serve")
    q: queue.Queue[ReviewRequest | CommandRequest | None] = queue.Queue()
    threading.Thread(target=run_worker, args=(q, process_factory()),
                     daemon=True).start()
    app = create_app(settings.webhook_secret,
                     allowed_workspace=settings.bitbucket_workspace,
                     enqueue=q.put)
    # Loopback only: nginx fronts this as the public edge on the droplet.
    # Binding a routable interface would expose the raw port — no body
    # limits in front of a handler that buffers request.body() before
    # auth, i.e. an unauthenticated memory-amplification vector.
    uvicorn.run(app, host="127.0.0.1", port=settings.port,
                log_level="warning")
