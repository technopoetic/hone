"""Agent tools: JSON schemas for the LLM and the code that backs them.
All code-facing tools operate inside the job's throwaway worktree."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .repo import RepoManager, WorktreeHandle, run_git

DIFF_LIMIT = 100 * 1024
TEXT_LIMIT = 50 * 1024

# Review marker — dedup key on the PR. review.py scans existing comments for
# MARKER_PREFIX + head sha to skip re-reviewing a commit we already covered.
MARKER_PREFIX = "<!-- hone:review:"

# hone's own project root — where ITS OWN node_modules/venv live, as
# opposed to the worktree's. Builtins below resolve tool binaries/plugins
# from here, never from the (untrusted) PR branch.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _eslint_command(worktree: Path) -> list[str]:
    # ESLint flat config (eslint.config.js) is executable JS, not data —
    # ESLint imports it directly, so it runs with this process's privileges
    # the moment this tool is called. There is no sandboxing that; letting
    # each repo own its config (as decided for the ruleset itself) is an
    # accepted risk for single-author repos only — see AGENTS.md trust
    # boundaries. --resolve-plugins-relative-to doesn't exist for flat
    # config, and Node's ESM resolver won't see our node_modules from a
    # file living in the worktree, so eslint-plugin-vue etc. would fail to
    # resolve — symlink ours in rather than `npm install` inside the
    # worktree, which would run arbitrary postinstall scripts from the PR
    # branch's own package.json/lockfile.
    our_modules = _PROJECT_ROOT / "node_modules"
    link = worktree / "node_modules"
    if link.is_symlink():
        if link.resolve() != our_modules.resolve():
            raise RuntimeError(
                f"{link} is a symlink to something other than our own "
                "node_modules — refusing to lint")
    elif link.exists():
        raise RuntimeError(
            f"{link} already exists in this PR branch — a repo should "
            "never ship its own node_modules; refusing to lint")
    else:
        link.symlink_to(our_modules, target_is_directory=True)
    eslint_bin = our_modules / ".bin" / "eslint"
    return [str(eslint_bin), "."]


# Tools whose command can't just be literal argv in repos.toml because they
# need a path resolved at runtime. Everything else (ruff, mypy, a future
# golangci-lint/stylelint/...) is pure config — add it to repos.toml only,
# no code change needed. Add here only when a tool needs the same
# own-environment-vs-worktree split eslint does.
_BUILTIN_COMMANDS: dict[str, Callable[[Path], list[str]]] = {
    "eslint": _eslint_command,
}


@dataclass(frozen=True)
class ToolResult:
    content: str
    is_error: bool = False


def _truncate(text: str, limit: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= limit:
        return text
    return raw[:limit].decode("utf-8", errors="replace") + \
        f"\n\n[truncated: exceeded {limit // 1024} KB]"


class ToolBox:
    def __init__(self, repo_manager: RepoManager, worktree: WorktreeHandle,
                 base_sha: str, head_sha: str,
                 static_checks: dict[str, tuple[str, ...] | None],
                 post: Callable[[str], None]):
        self._repo = repo_manager
        self._wt = worktree
        self._base = base_sha
        self._head = head_sha
        # name -> argv, or None for a builtin (see _BUILTIN_COMMANDS).
        # Membership in this dict IS the allowlist for run_static_check.
        self._static = dict(static_checks)
        self._post = post

    def schemas(self) -> list[dict]:
        return [
            {"name": "get_pr_diff",
             "description": ("Unified diff of the PR (merge-base of "
                             "destination and head → head). Omit file_path "
                             "for the full diff."),
             "input_schema": {"type": "object",
                              "properties": {"file_path": {"type": "string"}}}},
            {"name": "rg_search",
             "description": ("Search the repo with ripgrep. Use to find "
                             "callers, usages, definitions — anything the "
                             "diff touches."),
             "input_schema": {"type": "object",
                              "properties": {
                                  "pattern": {"type": "string"},
                                  "glob": {"type": "string"}},
                              "required": ["pattern"]}},
            {"name": "read_file",
             "description": ("Read a file's full content at the PR head. "
                             "Path is relative to repo root."),
             "input_schema": {"type": "object",
                              "properties": {"path": {"type": "string"}},
                              "required": ["path"]}},
            {"name": "git_log",
             "description": ("Recent commit history, optionally filtered to "
                             "a path. Use to understand when/why code changed."),
             "input_schema": {"type": "object",
                              "properties": {
                                  "path": {"type": "string"},
                                  "max_count": {"type": "integer"}}}},
            {"name": "run_static_check",
             "description": ("Run an allowlisted static check in the repo "
                             "and return its output."),
             "input_schema": {"type": "object",
                              "properties": {"tool": {"type": "string",
                              "enum": list(self._static)}},
                              "required": ["tool"]}},
            {"name": "post_review",
             "description": ("Post the completed review as a comment on the "
                             "PR. Call exactly once, when the review is ready."),
             "input_schema": {"type": "object",
                              "properties": {"markdown": {"type": "string"}},
                              "required": ["markdown"]}},
        ]

    def execute(self, name: str, inputs: dict | None) -> ToolResult:
        handler = {
            "get_pr_diff": self._get_pr_diff,
            "rg_search": self._rg_search,
            "read_file": self._read_file,
            "git_log": self._git_log,
            "run_static_check": self._run_static_check,
            "post_review": self._post_review,
        }.get(name)
        if handler is None:
            return ToolResult(f"unknown tool '{name}'", is_error=True)
        try:
            return handler(**(inputs or {}))
        except TypeError as e:
            # bad arguments from the model — tell it rather than crash
            return ToolResult(f"invalid arguments for {name}: {e}",
                              is_error=True)
        except Exception as e:
            return ToolResult(f"{name} failed: {e}", is_error=True)

    def _confine(self, raw: str) -> Path:
        p = (self._wt.path / raw).resolve()
        if not p.is_relative_to(self._wt.path.resolve()):
            raise ValueError(f"path escapes the worktree: {raw}")
        return p

    def _get_pr_diff(self, file_path: str | None = None) -> ToolResult:
        out = self._repo.diff(self._wt.mirror, self._base, self._head,
                              file_path)
        if not out:
            target = file_path or "this PR"
            return ToolResult(f"no diff content for {target}")
        return ToolResult(_truncate(out, DIFF_LIMIT))

    def _rg_search(self, pattern: str, glob: str | None = None) -> ToolResult:
        args = ["rg", "--line-number", "--max-count", "100",
                "--color=never", "--no-heading"]
        if glob:
            args += ["-g", glob]
        args += ["--", pattern]  # '--' so a leading - can't be a flag
        try:
            proc = subprocess.run(args, cwd=self._wt.path,
                                  capture_output=True, text=True, timeout=30)
        except FileNotFoundError:
            return ToolResult("ripgrep (rg) is not installed",
                              is_error=True)
        if proc.returncode == 1:
            return ToolResult("no matches found")
        if proc.returncode != 0:
            return ToolResult(f"rg failed: {proc.stderr.strip()}",
                              is_error=True)
        return ToolResult(_truncate(proc.stdout, TEXT_LIMIT))

    def _read_file(self, path: str) -> ToolResult:
        p = self._confine(path)
        if not p.is_file():
            return ToolResult(f"no such file: {path}", is_error=True)
        data = p.read_bytes()[: TEXT_LIMIT + 1]
        text = data.decode("utf-8", errors="replace")
        return ToolResult(_truncate(text, TEXT_LIMIT))

    def _git_log(self, path: str | None = None,
                 max_count: int | None = None) -> ToolResult:
        try:
            n = max(1, min(int(max_count or 20), 100))
        except (TypeError, ValueError):
            n = 20
        args = ["log", f"--max-count={n}",
                "--format=%h %ad %an %s", "--date=short"]
        if path:
            p = self._confine(path)
            args += ["--", str(p.relative_to(self._wt.path))]
        return ToolResult(_truncate(
            run_git(args, cwd=self._wt.path), TEXT_LIMIT))

    def _run_static_check(self, tool: str) -> ToolResult:
        if tool not in self._static:
            allowed = ", ".join(self._static) or "(none configured)"
            return ToolResult(
                f"'{tool}' is not on the allowlist: {allowed}",
                is_error=True)
        cmd = self._static[tool]
        if cmd is None:
            builder = _BUILTIN_COMMANDS.get(tool)
            cmd = builder(self._wt.path) if builder else None
        if cmd is None:
            return ToolResult(f"no command configured for '{tool}'",
                              is_error=True)
        try:
            proc = subprocess.run(cmd, cwd=self._wt.path, capture_output=True,
                                  text=True, timeout=180)
        except FileNotFoundError:
            return ToolResult(f"'{tool}' runner is not installed",
                              is_error=True)
        output = (proc.stdout + proc.stderr).strip() or "(no output)"
        note = "" if proc.returncode == 0 else \
            f"\n[exit code {proc.returncode}]"
        return ToolResult(_truncate(output, TEXT_LIMIT) + note)

    def _post_review(self, markdown: str) -> ToolResult:
        wrapped = f"{MARKER_PREFIX}{self._head} -->\n{markdown}"
        try:
            self._post(wrapped)
        except Exception as e:
            return ToolResult(f"post failed: {e}", is_error=True)
        return ToolResult("Review posted.")
