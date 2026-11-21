"""Local repo management: one bare mirror per repo, one throwaway worktree
per review job. Credentials travel only in a per-command http.extraHeader —
never in remote URLs or .git/config."""

from __future__ import annotations

import base64
import logging
import subprocess
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("hone")


class GitError(RuntimeError):
    pass


def run_git(args: list[str], cwd: Path,
            extra_header: str | None = None, timeout: int = 300) -> str:
    """Run git, returning stdout; raise GitError with stderr on failure.
    Also used by the tools layer for read-only queries inside a worktree."""
    cmd = ["git"]
    if extra_header:
        cmd += ["-c", f"http.extraHeader=Authorization: Basic {extra_header}"]
    cmd += args
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise GitError(f"git timed out: {' '.join(args[:2])}") from e
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass(frozen=True)
class WorktreeHandle:
    path: Path
    mirror: Path


class RepoManager:
    def __init__(self, data_dir: Path, username: str, api_key: str,
                 remote_url_for: Callable[[str, str], str] | None = None):
        self.data_dir = Path(data_dir)
        self._auth_header = base64.b64encode(
            f"{username}:{api_key}".encode()).decode()
        self._remote_url_for = remote_url_for or (
            lambda ws, repo: f"https://bitbucket.org/{ws}/{repo}.git")

    def ensure_mirror(
        self, workspace: str, repo: str,
        source_refs: Sequence[str] = (),
    ) -> Path:
        mirrors_root = self.data_dir / "mirrors"
        mirror = mirrors_root / workspace / f"{repo}.git"
        # GitLab namespaces nest (group/subgroup/repo), so `workspace` is
        # untrusted path material: refuse any combination that leaves the
        # mirrors root before touching the filesystem.
        if not mirror.resolve().is_relative_to(mirrors_root.resolve()):
            raise GitError("mirror path escapes the configured data directory")
        # A plain `git clone --bare` records no fetch refspec, so
        # `git fetch origin` only touches FETCH_HEAD and the mirror silently
        # never updates. Set it explicitly — records it for new clones and
        # heals mirrors already cloned the broken way.
        refspec = "+refs/heads/*:refs/heads/*"
        if mirror.exists():
            run_git(["config", "remote.origin.fetch", refspec], cwd=mirror)
            run_git(["fetch", "--prune", "origin"], cwd=mirror,
                    extra_header=self._auth_header)
        else:
            mirror.parent.mkdir(parents=True, exist_ok=True)
            run_git(["clone", "--bare",
                     self._remote_url_for(workspace, repo), str(mirror)],
                    cwd=mirror.parent, extra_header=self._auth_header)
            run_git(["config", "remote.origin.fetch", refspec], cwd=mirror)
        # Forges whose PR head is not a branch tip (GitLab merge-request
        # refs, including from forks — always fetchable from the target
        # project, never a source URL) name the refs to pull explicitly.
        for ref in source_refs:
            run_git(["fetch", "origin", f"+{ref}:{ref}"], cwd=mirror,
                    extra_header=self._auth_header)
        return mirror

    def create_worktree(self, mirror: Path, head_sha: str) -> WorktreeHandle:
        job_dir = self.data_dir / "worktrees" / f"job-{uuid.uuid4().hex[:12]}"
        job_dir.parent.mkdir(parents=True, exist_ok=True)
        run_git(["worktree", "add", "--detach", str(job_dir), head_sha],
                cwd=mirror)
        return WorktreeHandle(path=job_dir, mirror=mirror)

    def remove_worktree(self, handle: WorktreeHandle) -> None:
        run_git(["worktree", "remove", "--force", str(handle.path)],
                cwd=handle.mirror)

    def sweep_stale_worktrees(self) -> int:
        """Remove every job worktree registered under data_dir/worktrees.
        Call only at process start, when no job can be active. Returns the
        number removed.

        Ask each mirror what it has registered rather than scanning
        directories: a deleted-on-disk dir can't confuse us, and we never
        remove a path git doesn't know about. Prune first so a ghost
        registration (dir already gone) is dropped silently and never
        counted as "removed": whether `worktree remove` tolerates ghosts
        varies by git version (2.43 exits 0; older gits refuse), and
        prune-first makes the behavior and the count the same everywhere."""
        removed = 0
        # Resolve both sides so a symlinked data_dir can't silently make
        # every registered path mismatch the job root.
        root = (self.data_dir / "worktrees").resolve()
        mirrors = self.data_dir / "mirrors"
        if not root.exists() or not mirrors.exists():
            return 0
        # rglob, not glob: GitLab namespaces nest, so mirrors can sit at
        # any depth under the mirrors root.
        for mirror in mirrors.rglob("*.git"):
            # Best-effort per mirror: a leaked job dir is cheap; blocking
            # every review because one mirror is corrupt (or holds a locked
            # worktree, which --force refuses) is not.
            try:
                run_git(["worktree", "prune"], cwd=mirror)
                out = run_git(["worktree", "list", "--porcelain"], cwd=mirror)
                for line in out.splitlines():
                    if not line.startswith("worktree "):
                        continue
                    path = Path(line.removeprefix("worktree "))
                    if path.resolve().parent != root:
                        continue  # the mirror's own entry; anything not ours
                    run_git(["worktree", "remove", "--force", str(path)],
                            cwd=mirror)
                    removed += 1
            except GitError as e:
                _log.warning("skipping mirror %s: %s", mirror, e)
        return removed

    def merge_base(self, mirror: Path, dest_branch: str, head_sha: str) -> str:
        return run_git(["merge-base", dest_branch, head_sha],
                       cwd=mirror).strip()

    def diff(self, mirror: Path, base: str, head: str,
             file_path: str | None = None) -> str:
        args = ["diff", f"{base}...{head}"]
        if file_path:
            args += ["--", file_path]
        return run_git(args, cwd=mirror)
