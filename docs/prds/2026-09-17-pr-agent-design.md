# PR Agent — Design

**Date:** 2026-09-17
**Status:** Phase 2 implemented (2026-10-01, all checks green; live on the deployment host)
**Repo:** `hone` (renamed from `pr-agent`)

## Goal

Build an autonomous PR reviewer for Bitbucket Cloud as a first agent-building
experiment. The primary deliverable is the learning: an LLM in a loop with
tools it chooses to call. The secondary deliverable is a genuinely useful
solo safety net for Mekanik — not a proof-of-concept — that reviews every
PR with real codebase context: the diff, search across the whole repo,
file history, and static analysis.

## Non-goals (v1)

- No approve/request-changes actions. Comments only — a hallucinated LGTM is
  worse than no review.
- No inline/line-level comments.
- No git hook trigger (parked — see Phases).
- No n8n, no LangChain or any agent framework. Raw Anthropic SDK only.
- No multi-agent orchestration, no memory between reviews. Each review is a
  fresh loop.
- No full test-suite execution. Tests need the Mekanik docker stack; v1
  runs allowlisted static checks only (ruff/mypy).

## Decisions

| Decision | Choice | Why |
|---|---|---|
| LLM provider | Synthetic.new, Anthropic-compatible endpoint | Flat $30/mo includes all models; full tool-calling support on `/messages` |
| Model | `hf:moonshotai/Kimi-K3` (pinned in `.env`) | The default `syn:large:text` alias hung on GLM during live testing; pinning Kimi-K3 sidesteps both the hang and Synthetic's alias drift (the alias now routes to DeepSeek-V4.1-Flash). 512k context is still the reason this class of model works. |
| SDK | Raw `anthropic` Python SDK, `base_url` pointed at Synthetic | The loop is ~40 lines; frameworks hide the thing being learned |
| Trigger (v1) | Bitbucket webhook → FastAPI receiver | Bitbucket Cloud has no server hooks; webhook is the native mechanism |
| **Repo access** | **Persistent mirror + git worktrees** | An agent with only "read one file by exact path, via HTTP" can fetch but can't investigate. Search, history, and static analysis are what make a reviewer useful — those need a local checkout |
| Git credentials | `http.extraHeader` per-fetch, remote URL has no creds | Never persist the token in `.git/config` |
| Code execution | Strict allowlist (configurable static checks; default ruff) running the checker binary from pr-agent's own venv, not the worktree's | Arbitrary `run_command` is an LLM-driven RCE path; allowlisting costs us pytest this version and buys a bounded blast radius. Running from pr-agent's venv (via the subprocess PATH of the Python process) avoids requiring the worktree to be a runnable Python project |
| Failure posture | Fail open | Dead API or timeout logs a warning; never blocks anything. Comments-only means worst case is a missing review |
| Comment strategy | One new comment per review, tagged with an HTML marker | Find-and-update of old bot comments is fiddly; defer until comment spam actually annoys |

## Architecture

```
Bitbucket Cloud ──webhook──▶ FastAPI receiver (:8420)
                               │ validate HMAC signature, return 200 fast
                               ▼
                         background task: ReviewJob(pr_id, repo, head_sha, dest_tip)
                               │
                               ├─ fetch mirror (bare clone under data dir)
                               ├─ git worktree add --detach <dir> <head_sha>
                               ▼
                     ┌─── agent loop ───┐
                     │  system prompt    │
                     │  + PR metadata    │
                     │  + diffstat       │
                     │        │          │
                     │  LLM ──▶ tools ──▶│  get_pr_diff / rg_search / read_file
                     │        │          │  git_log / run_static_check / post_review
                     └────────┴──────────┘
                               │
                               ▼
                    comment posted on PR (markdown)
                    worktree removed (always, also on failure)
```

The webhook returns HTTP 200 immediately and processes in a background task.
Bitbucket retries non-2xx webhook deliveries; answering slowly means
duplicate reviews.

A review job is "stateless" in the LLM sense — no memory between reviews —
but it gets a **disposable local checkout** for the duration of the job.
The mirror is the one persistent piece: a bare clone, fetched on each job,
shared by all jobs for that repo.

## Components

### `config.py`

Loads env vars (see Configuration). No defaults for secrets — a missing key
is a startup error, not a silent failure.

### `bitbucket.py` — Bitbucket client

Thin wrapper over `httpx` with Basic auth (`email` + `ATLASSIAN_API_KEY`).
Endpoints used:

| Operation | Call |
|---|---|
| PR metadata | `GET /2.0/repositories/{ws}/{repo}/pullrequests/{id}` |
| Diffstat (changed files) | `GET .../pullrequests/{id}/diffstat` |
| Post comment | `POST .../pullrequests/{id}/comments` `{"content": {"raw": md}}` |

The diff itself and file contents now come from the local worktree, not the
API — the API's job shrunk to metadata and commenting.

### `repo.py` — mirror and worktree manager

- `ensure_mirror(workspace, repo)` — creates a bare clone at
  `{DATA_DIR}/mirrors/{ws}/{repo}.git` on first use (clone via HTTPS with
  auth header); runs `git fetch --prune` per job.
- `create_worktree(sha)` — `git worktree add --detach
  {DATA_DIR}/worktrees/{job_id} {sha}`. The PR head sha is the source branch
  head, so it's always fetchable.
- `cleanup(worktree)` — `git worktree remove --force`, called in a
  `finally`. Worktrees are disposable by construction.
- Diff base: `git merge-base <dest_branch_tip> <head_sha>`, then
  `git diff base...head` — matches what Bitbucket displays in the PR.

### `tools.py` — tool schemas and dispatch

Six tools. JSON schemas double as the Anthropic `tools` payload; dispatch is
a dict of name → function. All code-facing tools operate inside the job's
worktree. Initial context already includes the diffstat file list, so there
is no separate list-files tool.

### `agent.py` — the loop

```python
def run_agent(client, system, initial_context, tools, max_turns=10):
    messages = [{"role": "user", "content": initial_context}]
    for _ in range(max_turns):
        resp = client.messages.create(
            model=settings.model, max_tokens=8192,
            system=system, tools=tools.schemas(), messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason != "tool_use":
            return text_of(resp), False        # ended without posting
        results = []
        for block in resp.content:
            if block.type == "tool_use":
                result = tools.execute(block)  # tool_result block
                results.append(result)
                if block.name == "post_review" and not result.is_error:
                    return text_of(resp), True # posted — done
        messages.append({"role": "user", "content": results})
    return None, False                         # turn limit hit
```

- `post_review` is the terminal tool: a successful execution posts the
  comment and ends the loop immediately, rather than waiting for the model
  to wind down. The second return value reports whether a review was posted.
- Cap of 10 turns. Turn-limit exit = review not posted, logged, nothing
  destructive.

### `reviewer.py` — prompt assembly and job orchestration

System prompt establishes the reviewer persona and policy:

- What to produce: summary of changes, risk assessment, convention issues,
  each with file references and a severity (HIGH / MEDIUM / LOW).
- How to investigate: don't guess from the diff alone — use `rg_search` to
  find callers/usage of anything the diff touches, `read_file` for
  surrounding context, `git_log` when "why is this here" matters,
  `run_static_check` before signing off.
- Convention checks sourced from `CONVENTIONS.md` in the repo if present,
  else a built-in minimal set.
- The review is advisory. No approvals, no demands.
- Output is Markdown; Bitbucket renders it.

The orchestrator: fetch mirror → create worktree → build context → run loop
→ cleanup worktree (finally) → log outcome.

### `server.py` — webhook receiver

FastAPI, single route `POST /webhook`:

1. Validate `X-Hub-Signature` (HMAC-SHA256 of raw body with the webhook
   secret). Reject mismatch with 403.
2. Filter events: act on `pullrequest:created` and `pullrequest:updated`
   only. Ignore everything else with 200.
3. Enqueue a `ReviewJob` (in-process queue — serial execution respects
   Synthetic's 1-concurrent-request-per-model limit, and also keeps mirror
   fetches and worktrees from racing). Return 200 within the same request
   cycle.

### `cli.py` — manual trigger

`uv run pr-agent review <pr-url> [--dry-run]` — fetches PR metadata by URL,
runs the same loop through the same mirror/worktree path, prints the review
to stdout. `--dry-run` never posts. Phase 1 surface; stays useful (re-run a
review on demand).

## Tool schemas

```json
[
  {
    "name": "get_pr_diff",
    "description": "Unified diff of the PR (merge-base of destination and head → head). Omit file_path for the full diff.",
    "input_schema": {
      "type": "object",
      "properties": {"file_path": {"type": "string"}}
    }
  },
  {
    "name": "rg_search",
    "description": "Search the repo with ripgrep. Use to find callers, usages, definitions — anything the diff touches.",
    "input_schema": {
      "type": "object",
      "properties": {
        "pattern": {"type": "string"},
        "glob": {"type": "string", "description": "Optional file glob, e.g. '*.py'"}
      },
      "required": ["pattern"]
    }
  },
  {
    "name": "read_file",
    "description": "Read a file's full content at the PR head. Path is relative to repo root.",
    "input_schema": {
      "type": "object",
      "properties": {"path": {"type": "string"}},
      "required": ["path"]
    }
  },
  {
    "name": "git_log",
    "description": "Recent commit history, optionally filtered to a path. Use to understand when/why code changed.",
    "input_schema": {
      "type": "object",
      "properties": {
        "path": {"type": "string"},
        "max_count": {"type": "integer", "description": "Default 20"}
      }
    }
  },
  {
    "name": "run_static_check",
    "description": "Run an allowlisted static check (e.g. ruff) in the repo and return its output.",
    "input_schema": {
      "type": "object",
      "properties": {
        "tool": {"type": "string", "enum": ["ruff"]}
      },
      "required": ["tool"]
    }
  },
  {
    "name": "post_review",
    "description": "Post the completed review as a comment on the PR. Call exactly once, when the review is ready.",
    "input_schema": {
      "type": "object",
      "properties": {"markdown": {"type": "string"}},
      "required": ["markdown"]
    }
  }
]
```

## Safety and failure handling

| Failure | Behavior |
|---|---|
| LLM call error / timeout | Log, fail open, no comment |
| Turn limit reached | Log, no comment |
| Bitbucket API error | Log with status code, fail open |
| Mirror fetch fails | Log, kill job, no comment |
| Oversized diff | `get_pr_diff` truncates at ~100 KB with a notice |
| Oversized `read_file` / `rg_search` result | Truncates at ~50 KB similarly |
| Webhook bad signature | 403, logged |
| Unknown event type | 200, ignored |
| Worktree path traversal (`read_file`, `git_log`) | All path args resolved and confined to the worktree root; anything outside is a tool error |
| Job crashes mid-review | `finally` removes the worktree; mirror is bare and fetched fresh each time |
| Credentials | Token lives only in env/config and the per-fetch `http.extraHeader` argument; never written into remote URLs or `.git/config` |

Every review comment is headed with the reviewed commit sha and wrapped in a
marker `<!-- pr-agent:review:{sha} -->` so a future version can find and
update its own comments.

## Configuration

| Env var | Required | Default | Notes |
|---|---|---|---|
| `SYNTHETIC_API_KEY` | yes | — | Synthetic.new key |
| `SYNTHETIC_MODEL` | no | `syn:large:text` | Code default. The `syn:large:text` alias hung on GLM during live testing; `.env` pins `hf:moonshotai/Kimi-K3` — keep the override |
| `BITBUCKET_EMAIL` | yes | — | set in `.env` |
| `BITBUCKET_API_KEY` | yes | — | Same value as `ATLASSIAN_API_KEY` |
| `BITBUCKET_WORKSPACE` | yes | — | pins the webhook's trust boundary; no default on purpose |
| `BITBUCKET_USERNAME` | no | (defaults to `BITBUCKET_EMAIL`) | git-over-HTTPS needs a username, not the email used for REST auth — set this when the git account differs from the Bitbucket account email |
| `PR_AGENT_WEBHOOK_SECRET` | for server | — | Bitbucket webhook secret |
| `PR_AGENT_DATA_DIR` | no | `~/.local/share/pr-agent` | Mirrors + worktrees |
| `PR_AGENT_REPO_CONFIG` | no | `repos.toml` | Tool registry + per-repo static-check enablement (Phase 3) — see that file, not an env var, for what runs where |
| `PR_AGENT_MAX_TURNS` | no | `15` | Updated default after live reviews showed the loop could use the extra budget; the agent loop's max_turns cap (Agent.py) matches |
| `PR_AGENT_PORT` | no | `8420` | |

## Testing

- `conftest.py` builds a small fixture git repo (a few commits and branches)
  and serves it as the "mirror" via `file://` — worktree creation and all
  local tools are testable offline.
- Unit tests with a fake LLM (scripted tool-call sequences) and fake
  Bitbucket client: loop termination, tool dispatch, turn limit, truncation,
  path confinement, signature validation, event filtering, worktree cleanup
  on failure.
- Integration smoke test (manual, opt-in via env flag): CLI `--dry-run`
  against a real open PR, verify a sensible review prints.
- No CI requirement for v1 — personal tool.

## Phases

1. **Core (Phase 1):** `config.py`, `bitbucket.py`, `repo.py`, `tools.py`,
   `agent.py`, `reviewer.py`, `cli.py` with `--dry-run`. Success: a real
   Mekanik PR reviewed end-to-end with search/history/static-check evidence
   in the output.
2. **Webhook (Phase 2):** `server.py`, signature validation, serial queue,
   live comments. Success: push a branch, get a review comment without
   manual action.
3. **Parked:** pytest execution (needs docker stack — likely its own small
   design), git pre-push hook adapter (same agent, different trigger),
   comment update/dedup, inline comments, Slack notification on HIGH
   severity.

## Open questions

None blocking Phase 1. Test execution strategy deferred to the parked
phase. Comment dedup deferred until volume justifies it.
