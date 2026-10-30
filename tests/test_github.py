import json

import httpx

from hone.github import GitHubClient


def make_client(handler):
    return GitHubClient(
        token="tok",
        owner="technopoetic",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


PR_PAYLOAD = {
    "title": "Add thing",
    "body": "does a thing",
    "user": {"login": "technopoetic"},
    "head": {"ref": "feature", "sha": "a" * 40},
    "base": {"ref": "main", "sha": "b" * 40},
}


def test_get_pull_request_maps_fields():
    def handler(request):
        assert request.url.path.endswith("/pulls/7")
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json=PR_PAYLOAD)

    pr = make_client(handler).get_pull_request("hone", 7)
    assert pr.title == "Add thing"
    assert pr.source_branch == "feature"
    assert pr.source_commit == "a" * 40
    assert pr.dest_branch == "main"
    assert pr.dest_commit == "b" * 40
    assert pr.author == "technopoetic"


def test_get_pull_request_defaults_missing_body():
    def handler(request):
        payload = dict(PR_PAYLOAD)
        payload["body"] = None
        return httpx.Response(200, json=payload)

    pr = make_client(handler).get_pull_request("hone", 7)
    assert pr.description == ""


def test_get_diffstat_paginates_until_short_page():
    pages = {
        1: [{"filename": f"f{i}.py", "status": "modified",
             "additions": 1, "deletions": 0} for i in range(100)],
        2: [{"filename": "last.py", "status": "added",
             "additions": 5, "deletions": 0}],
    }

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[page])

    entries = make_client(handler).get_diffstat("hone", 7)
    assert len(entries) == 101
    assert entries[-1] == ("last.py", "added", 5, 0)


def test_get_diffstat_single_short_page_stops_immediately():
    def handler(request):
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json=[
            {"filename": "a.py", "status": "added",
             "additions": 10, "deletions": 0},
        ])

    entries = make_client(handler).get_diffstat("hone", 7)
    assert entries == [("a.py", "added", 10, 0)]


def test_get_comments_returns_bodies():
    def handler(request):
        assert request.url.path.endswith("/issues/7/comments")
        return httpx.Response(200, json=[
            {"body": "<!-- hone:review:abc -->\nlgtm"},
            {"body": "nice work"},
        ])

    bodies = make_client(handler).get_comments("hone", 7)
    assert bodies == ["<!-- hone:review:abc -->\nlgtm", "nice work"]


def test_post_comment_sends_body():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        assert request.url.path.endswith("/issues/7/comments")
        return httpx.Response(201, json={"id": 1})

    make_client(handler).post_comment("hone", 7, "hello **world**")
    assert json.loads(seen["body"]) == {"body": "hello **world**"}
