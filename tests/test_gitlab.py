import json

import httpx
import pytest

from hone.forge import PullRequest
from hone.gitlab import GitLabClient

BASE_URL = "https://git.example.com/gitlab/"
MR = {
    "iid": 7,
    "title": "Add thing",
    "description": "does a thing",
    "author": {"username": "octo"},
    "source_branch": "feature",
    "sha": "a" * 40,
    "target_branch": "main",
    "diff_refs": {"start_sha": "b" * 40},
}


def make_client(handler):
    return GitLabClient(
        token="gl-token", base_url=BASE_URL,
        namespace="engineering/platform",
        http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_get_pull_request_maps_iid_and_encoded_project_path():
    def handler(request):
        assert request.headers["private-token"] == "gl-token"
        assert b"/gitlab/api/v4/projects/engineering%2Fplatform%2Fhone/" in request.url.raw_path
        assert request.url.path.endswith("/merge_requests/7")
        return httpx.Response(200, json=MR)

    pr = make_client(handler).get_pull_request("hone", 7)
    assert pr.id == 7
    assert pr.author == "octo"
    assert pr.source_branch == "feature"
    assert pr.source_commit == "a" * 40
    assert pr.dest_branch == "main"
    assert pr.dest_commit == "b" * 40
    assert pr.source_ref == "refs/merge-requests/7/head"


def test_get_pull_request_defaults_missing_optional_fields():
    def handler(request):
        payload = dict(MR)
        payload["description"] = None
        payload["author"] = None
        payload["diff_refs"] = None
        return httpx.Response(200, json=payload)

    pr = make_client(handler).get_pull_request("hone", 7)
    assert pr.description == ""
    assert pr.author == "unknown"
    assert pr.dest_commit == ""


def test_get_diffstat_maps_patch_counts_statuses_and_pages():
    def handler(request):
        assert request.url.path.endswith("/merge_requests/7/diffs")
        page = request.url.params["page"]
        if page == "1":
            return httpx.Response(200, headers={"X-Next-Page": "2"}, json=[{
                "new_path": "new.py", "old_path": "old.py",
                "new_file": False, "deleted_file": False,
                "renamed_file": True,
                "diff": "@@ -1 +1 @@\n-old\n+new\n+++literal\n---literal\n",
            }])
        assert page == "2"
        return httpx.Response(200, json=[{
            "new_path": "gone.py", "old_path": "gone.py",
            "new_file": False, "deleted_file": True,
            "renamed_file": False, "diff": "",
        }])

    assert make_client(handler).get_diffstat("hone", 7) == [
        ("new.py", "renamed", 2, 2),
        ("gone.py", "removed", 0, 0),
    ]


def test_get_diffstat_counts_header_shaped_lines_after_hunk_start():
    diff = (
        "--- a/example.txt\n"
        "+++ b/example.txt\n"
        "@@ -1 +1 @@\n"
        "--- \n"
        "+++ \n"
        "---literal\n"
        "+++literal\n"
    )

    def handler(request):
        return httpx.Response(200, json=[{
            "new_path": "example.txt", "old_path": "example.txt",
            "new_file": False, "deleted_file": False,
            "renamed_file": False, "diff": diff,
        }])

    assert make_client(handler).get_diffstat("hone", 7) == [
        ("example.txt", "modified", 2, 2),
    ]


def test_get_diffstat_maps_added_modified_and_opaque_entries():
    def handler(request):
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json=[
            {
                "new_path": "added.py", "old_path": "added.py",
                "new_file": True, "deleted_file": False,
                "renamed_file": False,
                "diff": "@@ -0,0 +1,2 @@\n+one\n+two\n",
            },
            {
                "new_path": "mod.py", "old_path": "mod.py",
                "new_file": False, "deleted_file": False,
                "renamed_file": False,
                "diff": "@@ -1,2 +1,2 @@\n-keep\n+keep\n context\n",
            },
            {
                "new_path": "big.bin", "old_path": "big.bin",
                "new_file": False, "deleted_file": False,
                "renamed_file": False,
                "collapsed": True, "too_large": True, "diff": "",
            },
        ])

    assert make_client(handler).get_diffstat("hone", 7) == [
        ("added.py", "added", 2, 0),
        ("mod.py", "modified", 1, 1),
        ("big.bin", "modified", 0, 0),
    ]


def test_get_diffstat_raises_on_http_failure():
    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler).get_diffstat("hone", 7)


def test_get_diffstat_raises_on_malformed_next_page():
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(
                200, headers={"X-Next-Page": "2"},
                json=[{
                    "new_path": "a.py", "old_path": "a.py",
                    "new_file": True, "deleted_file": False,
                    "renamed_file": False, "diff": "+a\n",
                }])
        return httpx.Response(
            200, headers={"X-Next-Page": "invalid"},
            json=[{
                "new_path": "b.py", "old_path": "b.py",
                "new_file": True, "deleted_file": False,
                "renamed_file": False, "diff": "+b\n",
            }])

    with pytest.raises(ValueError):
        make_client(handler).get_diffstat("hone", 7)


@pytest.mark.parametrize("bad_next_page", ["1", "0", "-2"])
def test_pagination_rejects_non_increasing_next_page(bad_next_page):
    requested_pages = []

    def handler(request):
        requested_pages.append(request.url.params["page"])
        if len(requested_pages) > 1:
            raise AssertionError("a non-increasing next page must not be "
                                 "requested")
        return httpx.Response(
            200, headers={"X-Next-Page": bad_next_page},
            json=[{"body": "review"}])

    with pytest.raises(ValueError, match="increasing"):
        make_client(handler).get_comments("hone", 7)
    assert requested_pages == ["1"]


def test_get_pull_request_raises_on_http_failure():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler).get_pull_request("hone", 7)


def test_comments_are_ascending_and_exclude_non_flat_notes():
    def handler(request):
        assert request.url.path.endswith("/merge_requests/7/notes")
        assert request.url.params["sort"] == "asc"
        assert request.url.params["order_by"] == "created_at"
        assert request.url.params["per_page"] == "100"
        return httpx.Response(200, json=[
            {"body": "review", "system": False, "internal": False},
            {"body": "system", "system": True},
            {"body": "internal", "internal": True},
            {"body": "diff", "position": {"new_line": 4}},
        ])

    assert make_client(handler).get_comments("hone", 7) == ["review"]


def test_comments_follow_next_page_without_reordering():
    def handler(request):
        if request.url.params["page"] == "1":
            return httpx.Response(
                200, headers={"X-Next-Page": "2"},
                json=[{"body": "oldest"}])
        return httpx.Response(200, json=[{"body": "newest"}])

    assert make_client(handler).get_comments("hone", 7) == [
        "oldest", "newest"]


def test_comments_raise_on_http_failure():
    def handler(request):
        return httpx.Response(500)

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler).get_comments("hone", 7)


def test_post_comment_sends_body():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        assert request.method == "POST"
        assert request.url.path.endswith("/merge_requests/7/notes")
        return httpx.Response(201, json={"id": 1})

    make_client(handler).post_comment("hone", 7, "hello **world**")
    assert json.loads(seen["body"]) == {"body": "hello **world**"}


def test_post_comment_raises_on_http_failure():
    def handler(request):
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(httpx.HTTPStatusError):
        make_client(handler).post_comment("hone", 7, "nope")


def test_pull_request_source_ref_defaults_to_none_for_other_forges():
    pr = PullRequest(
        id=1, title="t", description="", author="a",
        source_branch="s", source_commit="c", dest_branch="d",
        dest_commit="e")
    assert pr.source_ref is None
