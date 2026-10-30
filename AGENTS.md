# hone — agent notes

Renamed from `pr-agent` (name collided with Qodo's open-source `pr-agent`,
~10k stars, same category). Code, package, CLI, and marker/env-var strings
below all say `hone`. Instance-specific deploy detail (real hostname, SSH
alias, deploy path, pending rename runbook) lives in the gitignored
`DEPLOY.local.md`, not here.

- **Build/test:** `uv sync` then `uv run pytest` (all offline). JS-based
  static checks (eslint) additionally need `npm install` once at the
  project root — see Phase 3 below.
- **Run:** `uv run hone review <pr-url> [--dry-run]` — works against a
  Bitbucket or a GitHub PR URL, dispatched by `cli.parse_pr_url`;
  `--dry-run` never posts. `hone serve` is the webhook-server entry
  point. GitHub support is optional and all-or-nothing: set `GITHUB_TOKEN`
  (a PAT) + `GITHUB_OWNER` to turn it on for both `review` and `serve`;
  one set without the other is a startup `ConfigError`. `GITHUB_WEBHOOK_SECRET`
  is checked only at `serve`-time — missing it just skips mounting
  `/webhook/github` (logged warning), it never refuses to start the
  server (which still has to serve Bitbucket).
- **PR control plane:** comment `@hone mute` / `@hone unmute` /
  `@hone review` on a PR (comment must BE the command — exact text, any
  case/whitespace; only the PR author may issue one; hone's own marker
  comments never self-trigger). Mute state is a `<!-- hone:muted -->`
  marker comment hone posts — forge-resident, no server-side state,
  latest-marker-wins. Muted PRs skip push-triggered reviews at the same
  pre-mirror scan as dedup; `@hone review` bypasses both mute and the
  same-head dedup marker (dry-run bypasses both by design — it's the
  human's re-test path). Reviews are DELTA reviews: prior hone review
  comments are injected as "previous rounds" and the prompt forbids
  re-reporting unchanged prior findings; "no new findings" is a valid
  short review. **Deploy prerequisite:** the Bitbucket webhook
  subscription needs `pullrequest:created` + `pullrequest:updated` +
  `pullrequest:comment_created`, or commands never fire. The GitHub
  webhook (when GitHub support is on) needs `pull_request` +
  `issue_comment`, content type `application/json`, same reason.
- **Stack:** Python ≥3.12, uv, raw `anthropic` SDK (base URL pointed at
  Synthetic.new), `httpx`, `python-dotenv`, `fastapi` + `uvicorn`.
  Node (eslint + eslint-plugin-vue) for the one static check that needs
  it — see `package.json`, installed separately from the Python venv.
- **Layout:** `src/hone/` — config, forge (shared `PullRequest` + the
  `Forge` Protocol both clients satisfy structurally), bitbucket, github,
  repo (mirror/worktree + sweep), tools, markers (comment marker
  protocol: dedup/mute state + prior-review extraction), prompts, agent
  (loop), review (orchestrator, takes any `Forge`), cli, server.
- **Workspace-wide rollout:** `repos.toml` at the project root is the
  tool registry (`[tools.<name>]`, argv) + per-repo enablement
  (`[repos]`, list of tool names). A repo with no entry gets zero static
  checks — that's the safe default, not an oversight. Add a new repo or
  swap a repo's tools by editing this file only; add a genuinely new kind
  of *tool* there too unless it needs a runtime-resolved path (see
  `tools._BUILTIN_COMMANDS`, currently just `eslint`). `HONE_REPO_CONFIG`
  overrides the path.
- **Trust boundaries** (do not relax without a spec change): filesystem
  paths confined to the worktree (`tools._confine`); static checks live
  behind the configured allowlist; the only code path that can post
  sits behind `ReviewJob.dry_run` — the `serve` path is live-only by
  design (no dry-run mode on the server). The webhook routes (`/webhook`
  for Bitbucket, `/webhook/github` for GitHub) are the only public edges
  and the only unauthenticated-by-design paths in the system; each is
  HMAC-gated on its own secret and 403s on signature mismatch.
  Comments are gated on the dedup marker (`<!-- hone:review:{sha} -->`)
  checked against `forge.get_comments` BEFORE `ensure_mirror`, so a
  refire on the same head costs one paginated GET and no LLM spend. The
  same pre-mirror scan also carries the mute state (`markers.py`).
  Comment commands (`@hone ...`) are a second trigger surface on both
  webhook routes, routed through one shared gate (`server._route_command`)
  with two hard rules — do not relax either, and do not reimplement
  per-forge: commands are LITERAL string matches (comment text
  stripped+casefolded must equal the command; bodies are untrusted data
  and are never sent through the LLM as instructions), and only the PR
  author's own identity may issue one — Bitbucket's `uuid`, GitHub's
  numeric user `id` — fail closed on a missing identity. hone's own
  comments carry the `<!-- hone:` sentinel and are skipped as command
  sources on both forges — it posts as the owner's account, so the
  author gate alone cannot stop it reacting to its own output.
- **Accepted risk — eslint runs the PR branch's own `eslint.config.js`:**
  unlike `ruff.toml`/`pyproject.toml` (pure data), ESLint flat config is
  executable JS that ESLint `import()`s directly — proven empirically
  during design (a one-line file write in `eslint.config.js` executed
  with this process's privileges, no plugin needed). Letting the repo own
  that file is therefore letting any PR branch run code on the deployment
  host unattended, the moment the agent calls `run_static_check` — no
  human review gate. **Explicitly accepted for the two JS repos currently
  onboarded, on the grounds both are single-author** — if that stops
  being true (a second contributor, external PRs, a fork), revisit before
  onboarding the next JS repo. `tools._eslint_command` symlinks hone's
  own `node_modules` into the worktree (never `npm install`/`ci` there —
  that would be its own, arguably worse, postinstall-script RCE) so
  `eslint-plugin-vue` resolves; it refuses to run if the worktree already
  has a real `node_modules` of its own.
- **Inbound auth:** `/webhook` validates `X-Hub-Signature` and pins the
  payload's workspace to `settings.bitbucket_workspace`; `/webhook/github`
  validates `X-Hub-Signature-256` (same HMAC-SHA256, GitHub's header
  name) and pins to `settings.github_owner`. Both 403 on signature
  mismatch and fail soft (200, ignored) on a foreign namespace — without
  that guard, a forged payload's mirror path takes the namespace from the
  body and would clone arbitrary repos with the server's credentials. Do
  not drop either check.
- **Credentials:** Bitbucket and GitHub auth both go to git only via a
  per-command `http.extraHeader` (`repo.RepoManager`, one instance per
  forge) — never write tokens into remote URLs or git config. GitHub's
  git-over-HTTPS convention: username `x-access-token`, the PAT as the
  password (see `cli._github_repos`).
- **Deployment:** runs behind whatever reverse proxy and TLS termination
  you already have for other services on the same host. Bind uvicorn on
  `127.0.0.1:8420` only and front it from there — a second reverse proxy
  just for this buys nothing. Instance-specific detail (hostname, SSH
  alias, deploy path, rename runbook) is in the gitignored
  `DEPLOY.local.md`.
- **Spec:** `docs/prds/2026-09-17-pr-agent-design.md` (predates Phase 3/4;
  its own "Phases" section was never updated past Phase 2 — treat this
  file as current, not that section).
- **Phase 4 (GitHub done; Gitea not started):** `forge.py` holds the
  shared `PullRequest` + a `Forge` Protocol (structural, not an ABC — no
  inheritance exists elsewhere in this codebase); `bitbucket.py` and
  `github.py` both satisfy it with no shared base class. `get_comments`
  intentionally returns only flat/top-level comments on both forges —
  GitHub's inline review comments are out of scope, same as Bitbucket's
  (inline comments are still "Parked," see the PRD). `ReviewRequest`/
  `CommandRequest` carry a `forge` field (default `"bitbucket"`, so old
  callers/tests didn't need updating) that `cli.build_process` dispatches
  on against per-forge `forges`/`repo_managers` dicts. A Gitea client
  would be a third module satisfying `Forge`, a third URL pattern in
  `cli.parse_pr_url`, and a third webhook route in `server.py` — same
  shape, no new abstraction needed.
