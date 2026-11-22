import base64
import hashlib
import hmac
import json
import queue
import threading
import time
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
    app = create_app(bitbucket_secret=SECRET,
                     bitbucket_workspace="hmdatasolutions",
                     enqueue=queued.append)
    return TestClient(app), queued


def post(client: TestClient, body: bytes, *, signature: str | None = None,
         event: str = "pullrequest:created"):
    headers = {"X-Event-Key": event}
    if signature is not None:
        headers["X-Hub-Signature"] = signature
    return client.post("/webhook/bitbucket", content=body, headers=headers)


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
    app = create_app(bitbucket_secret=SECRET,
                     bitbucket_workspace="hmdatasolutions",
                     enqueue=queued.append)
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/webhook/bitbucket", content=json.dumps(PAYLOAD).encode(),
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

    app = create_app(bitbucket_secret=SECRET,
                     bitbucket_workspace="hmdatasolutions",
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


def make_serve_settings(forge, *, with_secret):
    settings = SimpleNamespace(
        bitbucket_api_key=None, bitbucket_webhook_secret=None,
        bitbucket_workspace="ws", github_token=None, github_owner="octo",
        github_webhook_secret=None, gitlab_token=None,
        gitlab_url="https://gitlab.com", gitlab_namespace="engineering",
        gitlab_webhook_signing_token=None, port=8420)
    credential = {
        "bitbucket": "bitbucket_api_key",
        "github": "github_token",
        "gitlab": "gitlab_token",
    }[forge]
    secret = {
        "bitbucket": "bitbucket_webhook_secret",
        "github": "github_webhook_secret",
        "gitlab": "gitlab_webhook_signing_token",
    }[forge]
    setattr(settings, credential, "token")
    if with_secret:
        setattr(settings, secret, "secret")
    return settings


def test_bitbucket_route_is_absent_when_not_configured():
    # create_app without bitbucket_secret/bitbucket_workspace: the route
    # doesn't exist at all, not merely unauthorized — 404, not 403.
    queued = []
    app = create_app(enqueue=queued.append)
    client = TestClient(app)
    assert client.get("/health").status_code == 200
    assert client.post("/webhook/bitbucket", content=b"{}").status_code == 404


@pytest.mark.parametrize("forge,missing_env", [
    ("bitbucket", "BITBUCKET_WEBHOOK_SECRET"),
    ("github", "GITHUB_WEBHOOK_SECRET"),
    ("gitlab", "GITLAB_WEBHOOK_SIGNING_TOKEN"),
])
def test_single_forge_without_secret_refuses(
        forge, missing_env, monkeypatch):
    # one enabled forge, no secret: nothing can mount, so refuse to serve
    # with that forge's specific variable — never bind an unsigned port
    def fail_run(*args, **kwargs):
        raise AssertionError("server must not bind")

    monkeypatch.setattr("uvicorn.run", fail_run)
    settings = make_serve_settings(forge, with_secret=False)
    with pytest.raises(SystemExit, match=missing_env):
        serve(settings, process_factory=lambda: lambda req: None)


def test_all_secrets_missing_names_every_variable_in_configured_order(
        monkeypatch):
    # several enabled forges, no usable secret between them: the error
    # must name every missing variable, ordered bitbucket, github, gitlab
    settings = make_serve_settings("bitbucket", with_secret=False)
    settings.github_token = "gh-token"
    with pytest.raises(SystemExit) as exc:
        serve(settings, process_factory=lambda: lambda req: None)
    message = str(exc.value)
    assert "BITBUCKET_WEBHOOK_SECRET" in message
    assert "GITHUB_WEBHOOK_SECRET" in message
    assert (message.index("BITBUCKET_WEBHOOK_SECRET")
            < message.index("GITHUB_WEBHOOK_SECRET"))


def test_healthy_forge_warns_and_serves_when_other_secrets_are_missing(
        monkeypatch, caplog):
    settings = make_serve_settings("bitbucket", with_secret=True)
    settings.github_token = "gh-token"
    settings.gitlab_token = "gl-token"
    caplog.set_level("WARNING", logger="hone")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    serve(settings, process_factory=lambda: lambda req: None)
    assert "GITHUB_WEBHOOK_SECRET" in caplog.text
    assert "GITLAB_WEBHOOK_SIGNING_TOKEN" in caplog.text


def test_serve_binds_loopback_only(monkeypatch):
    # nginx is the public edge; the raw port must not be reachable from
    # outside, or request.body()-buffered traffic bypasses nginx's body
    # limits unauthenticated — a memory-amplification vector.
    captured: dict = {}

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_run)
    settings = make_serve_settings("bitbucket", with_secret=True)
    serve(settings, process_factory=lambda: lambda req: None)
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == settings.port


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
    app = create_app(bitbucket_secret=SECRET,
                     bitbucket_workspace="hmdatasolutions",
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


# --- GitLab webhook (/webhook/gitlab) -----------------------------------

GITLAB_KEY = b"a test signing key with adequate entropy"
GITLAB_SECRET = "whsec_" + base64.b64encode(GITLAB_KEY).decode()
GITLAB_NAMESPACE = "engineering"
GITLAB_NOW = 1_800_000_000


def sign_gitlab(body: bytes, *, timestamp=str(GITLAB_NOW), message_id="msg-1"):
    signed = f"{message_id}.{timestamp}.".encode() + body
    digest = hmac.new(GITLAB_KEY, signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


@pytest.fixture
def gitlab_receiver(monkeypatch):
    monkeypatch.setattr("hone.server.time.time", lambda: GITLAB_NOW)
    queued = []
    app = create_app(
        enqueue=queued.append,
        gitlab_signing_token=GITLAB_SECRET,
        gitlab_namespace=GITLAB_NAMESPACE)
    return TestClient(app), queued


@pytest.fixture
def gitlab_soft_receiver(monkeypatch):
    """gitlab_receiver's app on a client that turns a handler crash into
    a 500 response instead of re-raising into the test — for malformed-
    input tests that assert a 403/200 contract and must never see an
    exception, even while the underlying defect exists."""
    monkeypatch.setattr("hone.server.time.time", lambda: GITLAB_NOW)
    queued = []
    app = create_app(
        enqueue=queued.append,
        gitlab_signing_token=GITLAB_SECRET,
        gitlab_namespace=GITLAB_NAMESPACE)
    return TestClient(app, raise_server_exceptions=False), queued


def gitlab_mr_payload(action="open"):
    return {
        "project": {"path_with_namespace": "engineering/platform/hone"},
        "object_attributes": {"action": action, "iid": 9},
    }


def post_gitlab(client, body, *, event="Merge Request Hook",
                signature=None, timestamp=str(GITLAB_NOW), message_id="msg-1"):
    headers = {
        "X-Gitlab-Event": event,
        "webhook-id": message_id,
        "webhook-timestamp": timestamp,
    }
    if signature is not None:
        headers["webhook-signature"] = signature
    return client.post("/webhook/gitlab", content=body, headers=headers)


@pytest.mark.parametrize("action", ["open", "reopen", "update"])
def test_gitlab_review_actions_enqueue(gitlab_receiver, action):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload(action)).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert queued == [ReviewRequest(
        workspace="engineering/platform", repo="hone", pr_id=9,
        forge="gitlab")]


@pytest.mark.parametrize("action", ["close", "merge", "approved"])
def test_gitlab_non_review_actions_are_ignored(gitlab_receiver, action):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload(action)).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


def test_gitlab_unknown_event_is_ignored(gitlab_receiver):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(
        client, body, event="Push Hook", signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


def test_gitlab_route_is_absent_when_not_configured():
    client = TestClient(create_app(enqueue=lambda request: None))
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 404


@pytest.mark.parametrize("timestamp", [
    "", "not-a-number", str(GITLAB_NOW - 301), str(GITLAB_NOW + 301),
])
def test_gitlab_bad_or_stale_timestamp_is_forbidden(gitlab_receiver, timestamp):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(
        client, body, timestamp=timestamp,
        signature=sign_gitlab(body, timestamp=timestamp))
    assert response.status_code == 403
    assert queued == []


def test_gitlab_huge_timestamp_is_forbidden(gitlab_soft_receiver,
                                            monkeypatch):
    # int() parses a 400-digit timestamp, but converting it to a float
    # for the freshness comparison overflows — a malformed timestamp
    # from an unauthenticated sender must still get a plain 403, never
    # a 500. The clock is mocked to a FLOAT here: the shared fixture
    # mocks an int, and int subtraction never overflows, so an int
    # clock would hide the exact float-conversion crash production
    # (real time.time) hits.
    monkeypatch.setattr("hone.server.time.time", lambda: float(GITLAB_NOW))
    client, queued = gitlab_soft_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    timestamp = "9" * 400
    response = post_gitlab(
        client, body, timestamp=timestamp,
        signature=sign_gitlab(body, timestamp=timestamp))
    assert response.status_code == 403
    assert queued == []


def test_gitlab_accepts_one_of_multiple_signatures(gitlab_receiver):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(
        client, body,
        signature=f"v2,ignored v1,not-the-signature {sign_gitlab(body)}")
    assert response.status_code == 200
    assert len(queued) == 1


@pytest.mark.parametrize("missing_header", ["webhook-id", "webhook-signature"])
def test_gitlab_missing_standard_webhook_auth_is_forbidden(
        gitlab_receiver, missing_header):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    headers = {
        "X-Gitlab-Event": "Merge Request Hook",
        "webhook-id": "msg-1",
        "webhook-timestamp": str(GITLAB_NOW),
        "webhook-signature": sign_gitlab(body),
    }
    del headers[missing_header]
    response = client.post("/webhook/gitlab", content=body, headers=headers)
    assert response.status_code == 403
    assert queued == []


def test_gitlab_wrong_signature_is_forbidden(gitlab_receiver):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(
        client, body, signature=sign_gitlab(body, message_id="another-message"))
    assert response.status_code == 403
    assert queued == []


def test_gitlab_tampered_raw_body_is_forbidden(gitlab_receiver):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    tampered = body.replace(b'"open"', b'"update"')
    assert tampered != body
    response = post_gitlab(client, tampered, signature=sign_gitlab(body))
    assert response.status_code == 403
    assert queued == []


def test_gitlab_v2_only_signature_is_forbidden(gitlab_receiver):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    signature = "v2," + sign_gitlab(body).partition(",")[2]
    response = post_gitlab(client, body, signature=signature)
    assert response.status_code == 403
    assert queued == []


def test_gitlab_legacy_token_is_not_authentication(gitlab_receiver):
    client, queued = gitlab_receiver
    body = json.dumps(gitlab_mr_payload()).encode()
    response = client.post(
        "/webhook/gitlab", content=body,
        headers={
            "X-Gitlab-Event": "Merge Request Hook",
            "X-Gitlab-Token": GITLAB_SECRET,
            "webhook-id": "msg-1",
            "webhook-timestamp": str(GITLAB_NOW),
        })
    assert response.status_code == 403
    assert queued == []


@pytest.mark.parametrize("secret", ["not-whsec", "whsec_!!!", "whsec_"])
def test_gitlab_malformed_signing_token_is_forbidden(monkeypatch, secret):
    monkeypatch.setattr("hone.server.time.time", lambda: GITLAB_NOW)
    queued = []
    app = create_app(
        enqueue=queued.append, gitlab_signing_token=secret,
        gitlab_namespace=GITLAB_NAMESPACE)
    client = TestClient(app, raise_server_exceptions=False)
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 403
    assert queued == []


def test_gitlab_non_ascii_signature_header_is_forbidden(monkeypatch):
    monkeypatch.setattr("hone.server.time.time", lambda: GITLAB_NOW)
    queued = []
    app = create_app(
        enqueue=queued.append, gitlab_signing_token=GITLAB_SECRET,
        gitlab_namespace=GITLAB_NAMESPACE)
    # raise_server_exceptions=False is captured at client construction in
    # starlette, so mutating an existing client is a no-op — build one here.
    client = TestClient(app, raise_server_exceptions=False)
    body = json.dumps(gitlab_mr_payload()).encode()
    response = client.post(
        "/webhook/gitlab", content=body,
        headers={
            "X-Gitlab-Event": "Merge Request Hook",
            "webhook-id": b"msg-1",
            "webhook-timestamp": str(GITLAB_NOW).encode(),
            "webhook-signature": b"v1," + "é".encode("latin-1") * 44,
        })
    assert response.status_code == 403
    assert queued == []


@pytest.mark.parametrize("full_path", ["engineering-other/hone", "someone/hone"])
def test_gitlab_foreign_namespace_is_ignored(gitlab_receiver, full_path):
    client, queued = gitlab_receiver
    payload = gitlab_mr_payload()
    payload["project"]["path_with_namespace"] = full_path
    body = json.dumps(payload).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


@pytest.mark.parametrize("project", [
    {"path_with_namespace": "engineering"},
    {},
    {"path_with_namespace": ""},
    {"other_key": "engineering/platform/hone"},
])
def test_gitlab_malformed_project_is_ignored(gitlab_receiver, project):
    client, queued = gitlab_receiver
    payload = gitlab_mr_payload()
    payload["project"] = project
    body = json.dumps(payload).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert queued == []


def test_gitlab_signed_malformed_json_is_ignored(gitlab_receiver):
    client, queued = gitlab_receiver
    body = b"{not-json"
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


@pytest.mark.parametrize("iid", ["9", None])
def test_gitlab_non_integer_iid_is_ignored(gitlab_receiver, iid):
    client, queued = gitlab_receiver
    payload = gitlab_mr_payload()
    payload["object_attributes"]["iid"] = iid
    body = json.dumps(payload).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


@pytest.mark.parametrize("action", [[], {}])
def test_gitlab_unhashable_action_is_ignored(gitlab_soft_receiver, action):
    # a list/dict action is malformed payload shape; unguarded set
    # membership on it raises TypeError, surfacing as a 500 instead of
    # the fail-soft ignored 200 the shape contract promises
    client, queued = gitlab_soft_receiver
    body = json.dumps(gitlab_mr_payload(action)).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


@pytest.mark.parametrize("iid", [True, False])
def test_gitlab_boolean_iid_is_not_an_integer_iid(
        gitlab_soft_receiver, iid):
    # bool is an int subclass — an exact-type check is what keeps
    # "true" from meaning merge request 1
    client, queued = gitlab_soft_receiver
    payload = gitlab_mr_payload()
    payload["object_attributes"]["iid"] = iid
    body = json.dumps(payload).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


def test_gitlab_enqueue_failure_propagates_as_500(monkeypatch, caplog):
    monkeypatch.setattr("hone.server.time.time", lambda: GITLAB_NOW)
    calls = []

    def full_queue(request):
        calls.append(request)
        raise ValueError("queue write failed")

    app = create_app(
        enqueue=full_queue, gitlab_signing_token=GITLAB_SECRET,
        gitlab_namespace=GITLAB_NAMESPACE)
    client = TestClient(app, raise_server_exceptions=False)
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 500
    assert len(calls) == 1
    assert not any("unparseable gitlab webhook" in record.getMessage()
                   for record in caplog.records)


def gitlab_note_payload(
        text="@hone mute", *, action="create", actor=1001, author=1001,
        noteable_type="MergeRequest", system=False, internal=False,
        note_type=None, position=None):
    attributes = {
        "action": action, "note": text, "noteable_type": noteable_type,
        "author_id": actor, "system": system, "internal": internal,
    }
    if note_type is not None:
        attributes["type"] = note_type
    if position is not None:
        attributes["position"] = position
    return {
        "project": {"path_with_namespace": "engineering/platform/hone"},
        "object_attributes": attributes,
        "merge_request": {"iid": 9, "author_id": author},
    }


def post_gitlab_note(client, payload):
    body = json.dumps(payload).encode()
    return post_gitlab(
        client, body, event="Note Hook", signature=sign_gitlab(body))


@pytest.mark.parametrize("action", ["mute", "unmute"])
def test_gitlab_mute_commands_from_author_enqueue(gitlab_receiver, action):
    client, queued = gitlab_receiver
    response = post_gitlab_note(
        client, gitlab_note_payload(f"@hone {action}"))
    assert response.status_code == 200
    assert queued == [CommandRequest(
        workspace="engineering/platform", repo="hone", pr_id=9,
        action=action, forge="gitlab")]


def test_gitlab_review_command_enqueues_forced_review(gitlab_receiver):
    client, queued = gitlab_receiver
    response = post_gitlab_note(client, gitlab_note_payload("@hone review"))
    assert response.status_code == 200
    assert queued == [ReviewRequest(
        workspace="engineering/platform", repo="hone", pr_id=9,
        force=True, forge="gitlab")]


def test_gitlab_command_is_matched_after_strip_and_case_only(gitlab_receiver):
    client, queued = gitlab_receiver
    response = post_gitlab_note(
        client, gitlab_note_payload("  @Hone MUTE \n"))
    assert response.status_code == 200
    assert queued == [CommandRequest(
        workspace="engineering/platform", repo="hone", pr_id=9,
        action="mute", forge="gitlab")]


def test_gitlab_command_inside_prose_is_rejected(gitlab_receiver):
    client, queued = gitlab_receiver
    response = post_gitlab_note(
        client, gitlab_note_payload("should we ask @hone mute?"))
    assert response.status_code == 200
    assert queued == []


def test_gitlab_command_from_non_author_is_ignored(gitlab_receiver):
    client, queued = gitlab_receiver
    response = post_gitlab_note(client, gitlab_note_payload(actor=2002))
    assert response.status_code == 200
    assert queued == []


@pytest.mark.parametrize("missing_identity", ["actor", "author"])
def test_gitlab_command_with_missing_identity_is_ignored(
        gitlab_receiver, missing_identity):
    client, queued = gitlab_receiver
    payload = gitlab_note_payload()
    if missing_identity == "actor":
        del payload["object_attributes"]["author_id"]
    else:
        del payload["merge_request"]["author_id"]
    response = post_gitlab_note(client, payload)
    assert response.status_code == 200
    assert queued == []


def test_gitlab_hone_marker_comment_never_triggers_command(gitlab_receiver):
    client, queued = gitlab_receiver
    response = post_gitlab_note(
        client, gitlab_note_payload("<!-- hone:review:abc -->\n@hone mute"))
    assert response.status_code == 200
    assert queued == []


@pytest.mark.parametrize("overrides", [
    {"action": "update"},
    {"noteable_type": "Issue"},
    {"system": True},
    {"internal": True},
    {"note_type": "DiffNote"},
    {"position": {"new_line": 4}},
])
def test_gitlab_ineligible_notes_are_ignored(gitlab_receiver, overrides):
    client, queued = gitlab_receiver
    response = post_gitlab_note(
        client, gitlab_note_payload(**overrides))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


def test_gitlab_plain_comment_triggers_nothing(gitlab_receiver):
    client, queued = gitlab_receiver
    response = post_gitlab_note(
        client, gitlab_note_payload("looks good to me"))
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


def test_gitlab_malformed_note_payload_is_ignored(gitlab_receiver):
    client, queued = gitlab_receiver
    payload = gitlab_note_payload()
    del payload["merge_request"]["iid"]
    response = post_gitlab_note(client, payload)
    assert response.status_code == 200
    assert queued == []


def test_gitlab_null_note_attributes_is_ignored(gitlab_receiver):
    # object_attributes must be a mapping: null notes from a signature
    # holder are malformed authenticated payloads and must fail soft,
    # never surface as a 500 out of the eligibility filters.
    client, queued = gitlab_receiver
    payload = gitlab_note_payload()
    payload["object_attributes"] = None
    response = post_gitlab_note(client, payload)
    assert response.status_code == 200
    assert queued == []


@pytest.mark.parametrize("note", [12345, ["@hone mute"],
                                  {"body": "@hone mute"}])
def test_gitlab_non_string_note_is_ignored(gitlab_soft_receiver, note):
    # _route_command assumes a text note; a truthy non-string reaching it
    # raises TypeError/AttributeError out of the extractor — malformed
    # shape, so the contract here is a fail-soft ignored 200
    client, queued = gitlab_soft_receiver
    response = post_gitlab_note(client, gitlab_note_payload(note))
    assert response.status_code == 200
    assert queued == []


@pytest.mark.parametrize("iid", [True, False])
def test_gitlab_note_boolean_iid_is_ignored(gitlab_soft_receiver, iid):
    # same exact-type rule as the MR path: a boolean IID must not enqueue
    # a command against merge request 1 (True)
    client, queued = gitlab_soft_receiver
    payload = gitlab_note_payload("@hone mute")
    payload["merge_request"]["iid"] = iid
    response = post_gitlab_note(client, payload)
    assert response.status_code == 200
    assert response.text == "ignored"
    assert queued == []


def test_serve_mounts_configured_gitlab_route(monkeypatch):
    monkeypatch.setattr("hone.server.time.time", lambda: GITLAB_NOW)
    captured = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, **kwargs: captured.update(app=app))
    settings = make_serve_settings("gitlab", with_secret=True)
    settings.gitlab_webhook_signing_token = GITLAB_SECRET
    serve(settings, process_factory=lambda: lambda request: None)
    client = TestClient(captured["app"])
    body = json.dumps(gitlab_mr_payload()).encode()
    response = post_gitlab(client, body, signature=sign_gitlab(body))
    assert response.status_code == 200


def test_serve_refuses_gitlab_secret_without_namespace(monkeypatch):
    def fail_run(*args, **kwargs):
        raise AssertionError("server must not bind")

    monkeypatch.setattr("uvicorn.run", fail_run)
    settings = make_serve_settings("gitlab", with_secret=True)
    settings.gitlab_namespace = None
    with pytest.raises(SystemExit, match="GITLAB_NAMESPACE"):
        serve(settings, process_factory=lambda: lambda request: None)
