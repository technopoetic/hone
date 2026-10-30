"""Forge-agnostic types shared by every forge client. Diffs and file
contents never come from here — they come from the local git mirror
(see repo.py) — so a `Forge` only needs to speak metadata/diffstat/comments."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PullRequest:
    id: int
    title: str
    description: str
    author: str
    source_branch: str
    source_commit: str  # PR head sha
    dest_branch: str
    dest_commit: str    # destination tip sha


class Forge(Protocol):
    def get_pull_request(self, repo: str, pr_id: int) -> PullRequest: ...

    def get_diffstat(
        self, repo: str, pr_id: int
    ) -> list[tuple[str, str, int, int]]: ...

    def get_comments(self, repo: str, pr_id: int) -> list[str]: ...

    def post_comment(self, repo: str, pr_id: int, markdown: str) -> None: ...
