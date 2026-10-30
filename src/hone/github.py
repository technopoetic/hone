"""Thin GitHub REST client — metadata, diffstat, and comments only. Diffs
and file contents come from the local worktree, not this API (see
bitbucket.py's module docstring — same split applies here)."""

from __future__ import annotations

import httpx

from .forge import PullRequest

BASE_URL = "https://api.github.com"
PER_PAGE = 100


class GitHubClient:
    def __init__(self, token: str, owner: str,
                 http: httpx.Client | None = None):
        self.owner = owner
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)

    def _repo_url(self, repo: str) -> str:
        return f"{BASE_URL}/repos/{self.owner}/{repo}"

    def _paginate(self, url: str) -> list[dict]:
        """GitHub doesn't hand back a next-page URL in the body (unlike
        Bitbucket) — page by page/per_page and stop once a page comes back
        short, which is simpler than parsing the Link header and just as
        correct for an endpoint with no filtering params to preserve."""
        items: list[dict] = []
        page = 1
        while True:
            r = self._http.get(url, headers=self._headers,
                               params={"per_page": PER_PAGE, "page": page})
            r.raise_for_status()
            batch = r.json()
            items.extend(batch)
            if len(batch) < PER_PAGE:
                return items
            page += 1

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequest:
        r = self._http.get(f"{self._repo_url(repo)}/pulls/{pr_id}",
                           headers=self._headers)
        r.raise_for_status()
        d = r.json()
        return PullRequest(
            id=pr_id,
            title=d["title"],
            description=d.get("body") or "",
            author=(d.get("user") or {}).get("login", "unknown"),
            source_branch=d["head"]["ref"],
            source_commit=d["head"]["sha"],
            dest_branch=d["base"]["ref"],
            dest_commit=d["base"]["sha"],
        )

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, str, int, int]]:
        files = self._paginate(f"{self._repo_url(repo)}/pulls/{pr_id}/files")
        return [(f["filename"], f["status"], f["additions"], f["deletions"])
                for f in files]

    def get_comments(self, repo: str, pr_id: int) -> list[str]:
        comments = self._paginate(
            f"{self._repo_url(repo)}/issues/{pr_id}/comments")
        return [c.get("body") or "" for c in comments]

    def post_comment(self, repo: str, pr_id: int, markdown: str) -> None:
        r = self._http.post(f"{self._repo_url(repo)}/issues/{pr_id}/comments",
                            headers=self._headers, json={"body": markdown})
        r.raise_for_status()
