# hone — agent notes

Renamed from `pr-agent` (name collided with Qodo's open-source `pr-agent`,
~10k stars, same category). Code, package, CLI, and marker/env-var strings
below all say `hone`. Instance-specific deploy detail (real hostname, SSH
alias, deploy path, pending rename runbook) lives in the gitignored
`DEPLOY.local.md`, not here.

- **Build/test:** `uv sync` then `uv run pytest` (all offline). JS-based
  static checks (eslint) additionally need `npm install` once at the
  project root — see Phase 3 below.
- **Run:** `uv run hone review <pr-url> [--dry-run]`; `--dry-run` never
  posts to Bitbucket. `hone serve` is the webhook-server entry point.
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
  `pullrequest:comment_created`, or commands never fire.
- **Stack:** Python ≥3.12, uv, raw `anthropic` SDK (base URL pointed at
  Synthetic.new), `httpx`, `python-dotenv`, `fastapi` + `uvicorn`.
  Node (eslint + eslint-plugin-vue) for the one static check that needs
  it — see `package.json`, installed separately from the Python venv.
- **Layout:** `src/hone/` — config, bitbucket, repo (mirror/worktree +
  sweep), tools, markers (comment marker protocol: dedup/mute state +
  prior-review extraction), prompts, agent (loop), review (orchestrator),
  cli, server.
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
  behind the configured allowlist; the only code path that can post to
  Bitbucket sits behind `ReviewJob.dry_run` — the `serve` path is
  live-only by design (no dry-run mode on the server). The webhook route
  is the only public edge and the only unauthenticated-by-design path
  in the system; it is HMAC-gated and 403s on signature mismatch.
  Comments are gated on the dedup marker (`<!-- hone:review:{sha} -->`)
  checked against `bitbucket.get_comments` BEFORE `ensure_mirror`, so a
  refire on the same head costs one paginated GET and no LLM spend. The
  same pre-mirror scan also carries the mute state (`markers.py`).
  Comment commands (`@hone ...`) are a second trigger surface on the
  webhook route with two hard rules — do not relax either: commands are
  LITERAL string matches (comment text stripped+casefolded must equal the
  command; bodies are untrusted data and are never sent through the LLM as
  instructions), and only the PR author's uuid may issue one (fail closed
  on a missing identity). hone's own comments carry the
  `<!-- hone:` sentinel and are skipped as command sources — it posts
  as the owner's account, so the author gate alone cannot stop it reacting
  to its own output.
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
- **Inbound auth:** the webhook route is the only public edge; it
  validates `X-Hub-Signature` (HMAC-SHA256 of raw body with the shared
  secret) and 403s on mismatch, and pins the payload's workspace to
  `settings.bitbucket_workspace` — without that guard, a forged payload's
  mirror path takes the workspace from the body and would clone arbitrary
  repos with the server's credentials. Do not drop this check.
- **Credentials:** Bitbucket auth goes to git only via per-command
  `http.extraHeader`; never write tokens into remote URLs or git config.
- **Deployment:** runs behind whatever reverse proxy and TLS termination
  you already have for other services on the same host. Bind uvicorn on
  `127.0.0.1:8420` only and front it from there — a second reverse proxy
  just for this buys nothing. Instance-specific detail (hostname, SSH
  alias, deploy path, rename runbook) is in the gitignored
  `DEPLOY.local.md`.
- **Spec:** `docs/prds/2026-09-17-pr-agent-design.md`.
- **Phase 4 (not started):** GitHub/Gitea support. `bitbucket.py` is a
  Bitbucket-specific client (REST shapes, HMAC header names, PR url regex
  in `cli.parse_pr_url`) with no abstraction over "forge" yet — that
  abstraction is Phase 4's actual work, not a config flag.
