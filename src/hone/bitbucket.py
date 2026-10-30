"""Thin Bitbucket Cloud client — metadata, diffstat, and comments only.
Diffs and file contents come from the local worktree, not this API."""

from __future__ import annotations

import httpx

from .forge import PullRequest

BASE_URL = "https://api.bitbucket.org/2.0"


class BitbucketClient:
    def __init__(self, email: str, api_key: str, workspace: str,
                 http: httpx.Client | None = None):
        self.workspace = workspace
        self._auth = (email, api_key)
        # PR diffstat 302s to the canonical /diffstat/<spec> endpoint
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)

    def _pr_url(self, repo: str, pr_id: int) -> str:
        return (f"{BASE_URL}/repositories/{self.workspace}/{repo}"
                f"/pullrequests/{pr_id}")

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequest:
        r = self._http.get(self._pr_url(repo, pr_id), auth=self._auth)
        r.raise_for_status()
        d = r.json()
        return PullRequest(
            id=pr_id,
            title=d["title"],
            description=d.get("description") or "",
            author=(d.get("author") or {}).get("display_name", "unknown"),
            source_branch=d["source"]["branch"]["name"],
            source_commit=d["source"]["commit"]["hash"],
            dest_branch=d["destination"]["branch"]["name"],
            dest_commit=d["destination"]["commit"]["hash"],
        )

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, str, int, int]]:
        """[(path, status, lines_added, lines_removed)], following pagination."""
        entries = []
        url: str | None = self._pr_url(repo, pr_id) + "/diffstat"
        while url:
            r = self._http.get(url, auth=self._auth, params={"pagelen": 100}
                               if "pagelen" not in url else None)
            r.raise_for_status()
            body = r.json()
            for v in body["values"]:
                f = v.get("new") or v.get("old") or {}
                entries.append(
                    (f.get("path", "?"), v["status"],
                     v.get("lines_added", 0), v.get("lines_removed", 0))
                )
            url = body.get("next")
        return entries

    def get_comments(self, repo: str, pr_id: int) -> list[str]:
        """Raw text bodies of all comments on the PR, following pagination."""
        bodies: list[str] = []
        url: str | None = self._pr_url(repo, pr_id) + "/comments"
        while url:
            r = self._http.get(url, auth=self._auth, params={"pagelen": 100}
                               if "pagelen" not in url else None)
            r.raise_for_status()
            body = r.json()
            bodies.extend(v.get("content", {}).get("raw", "")
                          for v in body["values"])
            url = body.get("next")
        return bodies

    def post_comment(self, repo: str, pr_id: int, markdown: str) -> None:
        r = self._http.post(self._pr_url(repo, pr_id) + "/comments",
                            auth=self._auth,
                            json={"content": {"raw": markdown}})
        r.raise_for_status()
