import httpx
import pytest

from hone.bitbucket import BitbucketClient


def make_client(handler):
    return BitbucketClient(
        email="rich@example.com",
        api_key="tok",
        workspace="ws",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


PR_PAYLOAD = {
    "title": "Add thing",
    "description": "does a thing",
    "author": {"display_name": "Richard"},
    "source": {"branch": {"name": "feature"}, "commit": {"hash": "a" * 40}},
    "destination": {"branch": {"name": "main"}, "commit": {"hash": "b" * 40}},
}


def test_get_pull_request_maps_fields():
    def handler(request):
        assert request.url.path.endswith("/pullrequests/7")
        return httpx.Response(200, json=PR_PAYLOAD)

    pr = make_client(handler).get_pull_request("mekanik", 7)
    assert pr.title == "Add thing"
    assert pr.source_branch == "feature"
    assert pr.source_commit == "a" * 40
    assert pr.dest_branch == "main"


def test_get_diffstat_follows_pagination():
    def handler(request):
        if "page2" in str(request.url):
            return httpx.Response(200, json={
                "values": [{"status": "modified", "lines_added": 3,
                            "lines_removed": 1,
                            "new": {"path": "b.py"}, "old": None}],
            })
        return httpx.Response(200, json={
            "values": [{"status": "added", "lines_added": 10,
                        "lines_removed": 0,
                        "new": {"path": "a.py"}, "old": None}],
            "next": str(request.url) + "&page2=1",
        })

    entries = make_client(handler).get_diffstat("mekanik", 7)
    assert entries == [
        ("a.py", "added", 10, 0),
        ("b.py", "modified", 3, 1),
    ]


def test_post_comment_sends_raw_markdown():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(201, json={"id": 1})

    import json

    make_client(handler).post_comment("mekanik", 7, "hello **world**")
    assert json.loads(seen["body"]) == {"content": {"raw": "hello **world**"}}


def test_get_comments_single_page_returns_raw_bodies():
    def handler(request):
        assert request.url.path.endswith("/pullrequests/7/comments")
        return httpx.Response(200, json={
            "values": [
                {"content": {"raw": "<!-- hone:review:abc -->\nlgtm"}},
                {"content": {"raw": "nice work"}},
            ],
        })

    bodies = make_client(handler).get_comments("mekanik", 7)
    assert bodies == ["<!-- hone:review:abc -->\nlgtm", "nice work"]


def test_get_comments_follows_pagination():
    def handler(request):
        if "page2" in str(request.url):
            return httpx.Response(200, json={
                "values": [{"content": {"raw": "second page"}}],
            })
        return httpx.Response(200, json={
            "values": [{"content": {"raw": "first page"}}],
            "next": str(request.url) + "&page2=1",
        })

    bodies = make_client(handler).get_comments("mekanik", 7)
    assert bodies == ["first page", "second page"]


def test_get_comments_sends_pagelen_on_first_request_only():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "page=2" in str(request.url):
            return httpx.Response(200, json={"values": []})
        return httpx.Response(200, json={
            "values": [{"content": {"raw": "x"}}],
            # Bitbucket's next URL embeds its own pagination params; the
            # client must follow it verbatim. httpx replaces a URL's query
            # when params= is passed, so if the client resent pagelen here,
            # "page=2" would be clobbered and the next assert would fail.
            "next": str(request.url) + "&page=2",
        })

    bodies = make_client(handler).get_comments("mekanik", 7)
    assert bodies == ["x"]
    assert len(seen) == 2
    assert "pagelen=100" in seen[0]
    assert seen[1].endswith("pagelen=100&page=2")
