"""Thin GitLab REST v4 client — metadata, diffstat, and comments only.
Diffs and file contents come from the local mirror, not this API."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .forge import PullRequest

PER_PAGE = 100


class GitLabClient:
    def __init__(self, token: str, base_url: str, namespace: str,
                 http: httpx.Client | None = None):
        self.namespace = namespace
        self._headers = {"PRIVATE-TOKEN": token}
        self._api_url = base_url.rstrip("/") + "/api/v4"
        self._http = http or httpx.Client(timeout=30, follow_redirects=True)

    def _project_url(self, repo: str) -> str:
        """One encoded project identifier — GitLab namespaces may nest."""
        project = quote(f"{self.namespace}/{repo}", safe="")
        return f"{self._api_url}/projects/{project}"

    def _paginate(self, url: str,
                  params: dict | None = None) -> list[dict]:
        """Follow X-Next-Page until GitLab stops sending it. A non-numeric
        header is malformed server state, so surface it instead of either
        looping or dropping the remaining pages — and so is a header that
        doesn't advance the page: a value <= the current page (a server
        echoing the same next page forever) would otherwise loop to
        infinity, since the page-number trust here is the only thing
        keeping the loop finite."""
        items: list[dict] = []
        page = 1
        while True:
            r = self._http.get(
                url, headers=self._headers,
                params={**(params or {}), "per_page": PER_PAGE, "page": page})
            r.raise_for_status()
            items.extend(r.json())
            header = r.headers.get("x-next-page")
            if not header:
                return items
            next_page = int(header)
            if next_page <= page:
                raise ValueError(
                    f"x-next-page must be strictly increasing, got {header}")
            page = next_page

    @staticmethod
    def _line_counts(diff: str | None) -> tuple[int, int]:
        added = removed = 0
        in_hunk = False
        for line in (diff or "").splitlines():
            if line.startswith("@@"):
                in_hunk = True
            # Only file headers before the first hunk are metadata. Once a
            # hunk starts, header-shaped lines are content, like '+++literal'.
            if not in_hunk and line.startswith(("+++ ", "--- ")):
                continue
            added += line.startswith("+")
            removed += line.startswith("-")
        return added, removed

    def get_pull_request(self, repo: str, pr_id: int) -> PullRequest:
        r = self._http.get(
            f"{self._project_url(repo)}/merge_requests/{pr_id}",
            headers=self._headers)
        r.raise_for_status()
        d = r.json()
        return PullRequest(
            id=pr_id,
            title=d["title"],
            description=d.get("description") or "",
            author=(d.get("author") or {}).get("username", "unknown"),
            source_branch=d["source_branch"],
            source_commit=d["sha"],
            dest_branch=d["target_branch"],
            dest_commit=(d.get("diff_refs") or {}).get("start_sha") or "",
            source_ref=f"refs/merge-requests/{pr_id}/head",
        )

    def get_diffstat(self, repo: str, pr_id: int) -> list[tuple[str, str, int, int]]:
        """[(path, status, lines_added, lines_removed)], following pagination.
        Statuses match the other forges' vocabulary; collapsed/too-large
        files arrive with an empty diff and therefore zero line totals."""
        # /diffs is the paginated per-file endpoint; /changes is deprecated.
        entries = self._paginate(
            f"{self._project_url(repo)}/merge_requests/{pr_id}/diffs")
        result = []
        for c in entries:
            if c.get("new_file"):
                status = "added"
            elif c.get("deleted_file"):
                status = "removed"
            elif c.get("renamed_file"):
                status = "renamed"
            else:
                status = "modified"
            added, removed = self._line_counts(c.get("diff"))
            result.append(
                (c.get("new_path") or c.get("old_path"), status,
                 added, removed))
        return result

    def get_comments(self, repo: str, pr_id: int) -> list[str]:
        """Flat note bodies oldest-first, skipping system, internal, and
        diff-positioned notes — inline comments are out of scope."""
        notes = self._paginate(
            f"{self._project_url(repo)}/merge_requests/{pr_id}/notes",
            params={"sort": "asc", "order_by": "created_at"})
        return [n.get("body") or "" for n in notes
                if not n.get("system") and not n.get("internal")
                and "position" not in n]

    def post_comment(self, repo: str, pr_id: int, markdown: str) -> None:
        r = self._http.post(
            f"{self._project_url(repo)}/merge_requests/{pr_id}/notes",
            headers=self._headers, json={"body": markdown})
        r.raise_for_status()
