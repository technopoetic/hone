import hashlib
import hmac
import json
import queue
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hone.server import CommandRequest, ReviewRequest, create_app, run_worker, serve

SECRET = "s3cr3t"
PAYLOAD = {
    "pullrequest": {"id": 57},
    "repository": {"full_name": "hmdatasolutions/mekanik"},
}


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body,
                                hashlib.sha256).hexdigest()


@pytest.fixture
def receiver():
    """App plus the list its enqueue lands in — list.append as the queue."""
    queued: list[ReviewRequest] = []
    app = create_app(secret=SECRET, allowed_workspace="hmdatasolutions",
                     enqueue=queued.append)
    return TestClient(app), queued


def post(client: TestClient, body: bytes, *, signature: str | None = None,
         event: str = "pullrequest:created"):
    headers = {"X-Event-Key": event}
    if signature is not None:
        headers["X-Hub-Signature"] = signature
    return client.post("/webhook", content=body, headers=headers)


def test_missing_signature_is_forbidden(receiver):
    client, queued = receiver
    r = post(client, json.dumps(PAYLOAD).encode())
    assert r.status_code == 403
    assert queued == []


def test_wrong_signature_is_forbidden(receiver):
    client, queued = receiver
    body = json.dumps(PAYLOAD).encode()
    r = post(client, body, signature=sign(body, "not-the-secret"))
    assert r.status_code == 403
    assert queued == []


def test_malformed_signature_prefix_is_forbidden(receiver):
    client, queued = receiver
    body = json.dumps(PAYLOAD).encode()
    r = post(client, body, signature="sha1=" + "0" * 40)
    assert r.status_code == 403
    assert queued == []


def test_created_event_enqueues_review(receiver):
    client, queued = receiver
    body = json.dumps(PAYLOAD).encode()
    r = post(client, body, signature=sign(body, SECRET))
    assert r.status_code == 200
    assert r.text == "queued"
    assert queued == [ReviewRequest(workspace="hmdatasolutions",
                                    repo="mekanik", pr_id=57)]


def test_updated_event_enqueues_review(receiver):
    client, queued = receiver
    body = json.dumps(PAYLOAD).encode()
    r = post(client, body, signature=sign(body, SECRET),
             event="pullrequest:updated")
    assert r.status_code == 200
    assert len(queued) == 1
    assert queued[0].pr_id == 57
    assert queued[0].repo == "mekanik"


def test_other_events_are_ignored(receiver):
    client, queued = receiver
    body = json.dumps(PAYLOAD).encode()
    r = post(client, body, signature=sign(body, SECRET),
             event="repo:push")
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


# --- comment commands (@hone mute / unmute / review) -------------------

AUTHOR_UUID = "{c989d8a5-fea2-4940-8973-c659ae2eea08}"
OTHER_UUID = "{00000000-0000-0000-0000-000000000001}"


def comment_payload(text: str, *, actor: str = AUTHOR_UUID,
                    author: str = AUTHOR_UUID, pr_id: int = 57,
                    marker: str | None = None) -> dict:
    body = text if marker is None else f"<!-- hone:review:abc -->\n{text}"
    return {
        "comment": {"content": {"raw": body}},
        "pullrequest": {"id": pr_id, "author": {"uuid": author}},
        "actor": {"uuid": actor},
        "repository": {"full_name": "hmdatasolutions/mekanik"},
    }


def post_comment(client: TestClient, payload: dict) -> "object":
    body = json.dumps(payload).encode()
    return post(client, body, signature=sign(body, SECRET),
                event="pullrequest:comment_created")


def test_mute_command_from_author_enqueues_command(receiver):
    client, queued = receiver
    r = post_comment(client, comment_payload("@hone mute"))
    assert r.status_code == 200
    assert r.text == "queued"
    assert queued == [CommandRequest(workspace="hmdatasolutions",
                                     repo="mekanik", pr_id=57,
                                     action="mute")]


def test_unmute_command_from_author_enqueues_command(receiver):
    client, queued = receiver
    r = post_comment(client, comment_payload("@hone unmute"))
    assert r.status_code == 200
    assert queued == [CommandRequest(workspace="hmdatasolutions",
                                     repo="mekanik", pr_id=57,
                                     action="unmute")]


def test_review_command_enqueues_forced_review(receiver):
    client, queued = receiver
    r = post_comment(client, comment_payload("@hone review"))
    assert r.status_code == 200
    assert queued == [ReviewRequest(workspace="hmdatasolutions",
                                    repo="mekanik", pr_id=57, force=True)]


def test_command_is_matched_after_strip_and_case_only(receiver):
    # surrounding whitespace and letter case don't block a command
    client, queued = receiver
    r = post_comment(client, comment_payload("  @Hone MUTE \n"))
    assert r.status_code == 200
    assert queued == [CommandRequest(workspace="hmdatasolutions",
                                     repo="mekanik", pr_id=57,
                                     action="mute")]


def test_command_inside_prose_is_not_a_command(receiver):
    # literal match contract: the phrase must BE the comment, not appear in
    # it — prose mentioning the agent must never flip state or spend LLM
    client, queued = receiver
    r = post_comment(client, comment_payload("should we ask @hone mute?"))
    assert r.status_code == 200
    assert queued == []


def test_command_from_non_author_is_ignored(receiver):
    # trust gate: a random commenter must not silence the reviewer or force
    # LLM spend. Fail soft (200) so Bitbucket doesn't redeliver.
    client, queued = receiver
    r = post_comment(client, comment_payload("@hone mute",
                                             actor=OTHER_UUID))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


def test_command_with_missing_actor_is_ignored(receiver):
    # fail closed: an unknown identity is exactly what the gate exists for
    client, queued = receiver
    payload = comment_payload("@hone mute")
    del payload["actor"]
    r = post_comment(client, payload)
    assert r.status_code == 200
    assert queued == []


def test_hone_own_comments_never_trigger_commands(receiver):
    # hone posts as the owner's account, so its comments' actor IS the
    # author — the marker sentinel, not the author gate, is what stops it
    # reacting to its own output (e.g. a review quoting a command)
    client, queued = receiver
    r = post_comment(client, comment_payload("@hone mute",
                                             marker="abc"))
    assert r.status_code == 200
    assert queued == []


def test_plain_comment_triggers_nothing(receiver):
    client, queued = receiver
    r = post_comment(client, comment_payload("looks good to me"))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


def test_comment_event_for_foreign_workspace_is_ignored(receiver):
    client, queued = receiver
    payload = comment_payload("@hone mute")
    payload["repository"]["full_name"] = "someoneelse/otherrepo"
    r = post_comment(client, payload)
    assert r.status_code == 200
    assert queued == []


def test_malformed_comment_payload_is_ignored(receiver):
    # signature holder, garbage shape — fail open like every other
    # mis-shaped payload so Bitbucket doesn't redeliver
    client, queued = receiver
    r = post_comment(client, {"pullrequest": {"id": 57},
                              "repository": {"full_name":
                                             "hmdatasolutions/mekanik"}})
    assert r.status_code == 200
    assert queued == []


def test_signature_covers_raw_body(receiver):
    # Signed over the original bytes, delivered with one byte changed —
    # catches any implementation that signs a parsed-and-reserialized form.
    client, queued = receiver
    body = json.dumps(PAYLOAD).encode()
    tampered = body.replace(b"57", b"58")
    assert tampered != body
    r = post(client, tampered, signature=sign(body, SECRET))
    assert r.status_code == 403
    assert queued == []


def test_foreign_workspace_is_ignored_not_enqueued(receiver):
    # A signed payload still names its own repo; without the workspace pin
    # the mirror path would clone arbitrary repos with our credentials.
    # Fail soft (200): a misconfigured webhook must not trigger Bitbucket
    # redelivery retries.
    client, queued = receiver
    foreign = {"pullrequest": {"id": 9},
               "repository": {"full_name": "someoneelse/otherrepo"}}
    body = json.dumps(foreign).encode()
    r = post(client, body, signature=sign(body, SECRET))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


def test_non_ascii_signature_header_is_forbidden():
    # starlette decodes header values as latin-1, so obs-text bytes in the
    # signature header reach the handler as a non-ASCII str — compare_digest
    # raises TypeError on a non-ASCII str pair, which surfaced as a 500
    # instead of the spec's plain 403 for malformed signatures. Sent as raw
    # bytes here because httpx would refuse the str client-side (ascii).
    queued: list[ReviewRequest] = []
    app = create_app(secret=SECRET, allowed_workspace="hmdatasolutions",
                     enqueue=queued.append)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/webhook", content=json.dumps(PAYLOAD).encode(),
                    headers={"X-Hub-Signature":
                             b"sha256=" + "é".encode("latin-1") * 64,
                             "X-Event-Key": "pullrequest:created"})
    assert r.status_code == 403
    assert queued == []


def test_signed_payload_missing_repository_is_ignored(receiver):
    # Only a secret holder can reach payload parsing, but a malformed shape
    # must still not 500: fail-open 200 so Bitbucket doesn't redeliver.
    client, queued = receiver
    shapeless = {"pullrequest": {"id": 57}}
    body = json.dumps(shapeless).encode()
    r = post(client, body, signature=sign(body, SECRET))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


def test_enqueue_failure_propagates_as_500(caplog):
    # enqueue sits outside the shape-error guard on purpose: if the queue
    # ever refuses a job (bounded/disk-backed queue in a future phase),
    # 500 is the honest answer — Bitbucket redelivers — and the failure
    # must not be mislabeled "unparseable webhook payload". The raise is
    # a ValueError deliberately: it's in the shape-error tuple, so this
    # pins that the enqueue call can never be masked by that guard.
    calls: list[ReviewRequest] = []

    def full_queue(req: ReviewRequest) -> None:
        calls.append(req)
        raise ValueError("queue write failed")

    app = create_app(secret=SECRET, allowed_workspace="hmdatasolutions",
                     enqueue=full_queue)
    client = TestClient(app, raise_server_exceptions=False)
    body = json.dumps(PAYLOAD).encode()
    r = post(client, body, signature=sign(body, SECRET))
    assert r.status_code == 500
    assert len(calls) == 1  # failure happened at enqueue, not earlier
    assert not any("unparseable webhook payload" in rec.getMessage()
                   for rec in caplog.records)


def test_worker_survives_failure_and_runs_next_job():
    q: queue.Queue[ReviewRequest | None] = queue.Queue()
    attempts: list[int] = []
    second_done = threading.Event()

    def process(req: ReviewRequest) -> None:
        attempts.append(req.pr_id)
        if req.pr_id == 1:
            raise RuntimeError("job one blows up")
        second_done.set()

    worker = threading.Thread(target=run_worker, args=(q, process),
                              daemon=True)
    worker.start()
    q.put(ReviewRequest(workspace="ws", repo="repo", pr_id=1))
    q.put(ReviewRequest(workspace="ws", repo="repo", pr_id=2))
    assert second_done.wait(timeout=5)
    q.join()
    assert attempts == [1, 2]

    q.put(None)  # shutdown sentinel
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_serve_binds_loopback_only(monkeypatch):
    # nginx is the public edge; the raw port must not be reachable from
    # outside, or request.body()-buffered traffic bypasses nginx's body
    # limits unauthenticated — a memory-amplification vector.
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    settings = SimpleNamespace(webhook_secret=SECRET,
                               bitbucket_workspace="hmdatasolutions",
                               port=8420, github_token=None,
                               github_owner=None, github_webhook_secret=None)
    serve(settings, process_factory=lambda: lambda req: None)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == settings.port


def test_serve_refuses_to_start_without_secret():
    settings = SimpleNamespace(webhook_secret=None,
                               bitbucket_workspace="hmdatasolutions",
                               port=8420, github_token=None,
                               github_owner=None, github_webhook_secret=None)
    with pytest.raises(SystemExit):
        serve(settings, process_factory=lambda: lambda req: None)


def test_health_endpoint_answers_unauthenticated(receiver):
    # deployment convention (mekanik): /health must answer 200 with no auth
    client, _ = receiver
    resp = client.get("/health")
    assert resp.status_code == 200
    assert "ok" in resp.text


# --- GitHub webhook (/webhook/github) -----------------------------------

GITHUB_SECRET = "gh-s3cr3t"
GITHUB_OWNER = "technopoetic"


@pytest.fixture
def github_receiver():
    queued: list[ReviewRequest | CommandRequest] = []
    app = create_app(secret=SECRET, allowed_workspace="hmdatasolutions",
                     enqueue=queued.append, github_secret=GITHUB_SECRET,
                     github_owner=GITHUB_OWNER)
    return TestClient(app), queued


def post_github(client: TestClient, body: bytes, *, event: str,
                signature: str | None = None):
    headers = {"X-GitHub-Event": event}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post("/webhook/github", content=body, headers=headers)


def pr_event_payload(action: str = "opened", pr_number: int = 7) -> dict:
    return {
        "action": action,
        "pull_request": {"number": pr_number},
        "repository": {"full_name": f"{GITHUB_OWNER}/hone"},
    }


def test_github_route_is_absent_when_not_configured(receiver):
    # create_app without github_secret/github_owner: the route doesn't
    # exist at all, not merely unauthorized — 404, not 403.
    client, _ = receiver
    r = client.post("/webhook/github", content=b"{}",
                    headers={"X-GitHub-Event": "pull_request"})
    assert r.status_code == 404


def test_github_missing_signature_is_forbidden(github_receiver):
    client, queued = github_receiver
    r = post_github(client, json.dumps(pr_event_payload()).encode(),
                    event="pull_request")
    assert r.status_code == 403
    assert queued == []


def test_github_wrong_signature_is_forbidden(github_receiver):
    client, queued = github_receiver
    body = json.dumps(pr_event_payload()).encode()
    r = post_github(client, body, event="pull_request",
                    signature=sign(body, "not-the-secret"))
    assert r.status_code == 403
    assert queued == []


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_github_review_actions_enqueue_review(github_receiver, action):
    client, queued = github_receiver
    body = json.dumps(pr_event_payload(action=action)).encode()
    r = post_github(client, body, event="pull_request",
                    signature=sign(body, GITHUB_SECRET))
    assert r.status_code == 200
    assert r.text == "queued"
    assert queued == [ReviewRequest(workspace=GITHUB_OWNER, repo="hone",
                                    pr_id=7, forge="github")]


def test_github_other_pr_actions_are_ignored(github_receiver):
    client, queued = github_receiver
    body = json.dumps(pr_event_payload(action="closed")).encode()
    r = post_github(client, body, event="pull_request",
                    signature=sign(body, GITHUB_SECRET))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


def test_github_foreign_owner_is_ignored(github_receiver):
    client, queued = github_receiver
    payload = pr_event_payload()
    payload["repository"]["full_name"] = "someoneelse/otherrepo"
    body = json.dumps(payload).encode()
    r = post_github(client, body, event="pull_request",
                    signature=sign(body, GITHUB_SECRET))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


GITHUB_AUTHOR_ID = 1001
GITHUB_OTHER_ID = 2002


def github_comment_payload(text: str, *, commenter: int = GITHUB_AUTHOR_ID,
                           author: int = GITHUB_AUTHOR_ID,
                           pr_number: int = 7,
                           is_pr: bool = True,
                           action: str = "created") -> dict:
    issue: dict = {"number": pr_number, "user": {"id": author}}
    if is_pr:
        issue["pull_request"] = {"url": "https://api.github.com/..."}
    return {
        "action": action,
        "comment": {"body": text, "user": {"id": commenter}},
        "issue": issue,
        "repository": {"full_name": f"{GITHUB_OWNER}/hone"},
    }


def post_github_comment(client: TestClient, payload: dict):
    body = json.dumps(payload).encode()
    return post_github(client, body, event="issue_comment",
                       signature=sign(body, GITHUB_SECRET))


def test_github_mute_command_from_author_enqueues_command(github_receiver):
    client, queued = github_receiver
    r = post_github_comment(client, github_comment_payload("@hone mute"))
    assert r.status_code == 200
    assert queued == [CommandRequest(workspace=GITHUB_OWNER, repo="hone",
                                     pr_id=7, action="mute", forge="github")]


def test_github_review_command_enqueues_forced_review(github_receiver):
    client, queued = github_receiver
    r = post_github_comment(client, github_comment_payload("@hone review"))
    assert r.status_code == 200
    assert queued == [ReviewRequest(workspace=GITHUB_OWNER, repo="hone",
                                    pr_id=7, force=True, forge="github")]


@pytest.mark.parametrize("action", ["edited", "deleted"])
def test_github_comment_non_created_action_is_ignored(github_receiver, action):
    # a deleted payload still carries the pre-delete body — without the
    # action gate, deleting (or editing into) an exact command string
    # would re-fire it, unlike Bitbucket's create-only comment_created
    client, queued = github_receiver
    r = post_github_comment(
        client, github_comment_payload("@hone mute", action=action))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []


def test_github_comment_on_plain_issue_is_ignored(github_receiver):
    # issue_comment fires for issues too; issue.pull_request is how GitHub
    # marks "this is actually a PR" — without it, never treat it as one.
    client, queued = github_receiver
    r = post_github_comment(
        client, github_comment_payload("@hone mute", is_pr=False))
    assert r.status_code == 200
    assert queued == []


def test_github_command_from_non_author_is_ignored(github_receiver):
    client, queued = github_receiver
    r = post_github_comment(
        client, github_comment_payload("@hone mute",
                                       commenter=GITHUB_OTHER_ID))
    assert r.status_code == 200
    assert queued == []


def test_github_own_marker_comments_never_trigger_commands(github_receiver):
    client, queued = github_receiver
    text = "<!-- hone:review:abc -->\n@hone mute"
    r = post_github_comment(client, github_comment_payload(text))
    assert r.status_code == 200
    assert queued == []


def test_github_plain_comment_triggers_nothing(github_receiver):
    client, queued = github_receiver
    r = post_github_comment(client, github_comment_payload("looks good"))
    assert r.status_code == 200
    assert r.text == "ignored"
    assert queued == []
