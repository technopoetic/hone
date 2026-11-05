# hone

Autonomous PR review agent for Bitbucket Cloud and GitHub — an
agent-building experiment.
Raw Anthropic SDK, `base_url` pointed at any Anthropic-Messages-API-
compatible endpoint (Synthetic.new, Anthropic directly, a self-hosted
proxy), six investigation tools on a throwaway git worktree, one comment
per review. No frameworks.

Formerly `pr-agent` — renamed to avoid colliding with Qodo's open-source
project of the same name.

## Setup

    uv sync
    cp .env.example .env   # fill in LLM_API_KEY, LLM_MODEL, BITBUCKET_EMAIL,
                           # BITBUCKET_API_KEY, and BITBUCKET_WORKSPACE
                           # (LLM_BASE_URL only if not using Anthropic
                           # directly; BITBUCKET_USERNAME optional, defaults
                           # to the email)

`BITBUCKET_API_KEY` = the same value as your `ATLASSIAN_API_KEY`.
`BITBUCKET_USERNAME` is optional — git-over-HTTPS authenticates with a
username, not an email address; it defaults to `BITBUCKET_EMAIL`.
Requires git and `ripgrep` (`rg`) on PATH. Static-check binaries run from
hone's own environment (never the target repo's) — `ruff`/`mypy` come
from the `uv`-managed venv; `eslint` needs `npm install` once at the project
root (see `package.json`). Which tools run against which repo is config,
not code — see `repos.toml`.

GitHub support is optional and all-or-nothing: set both `GITHUB_TOKEN`
(a fine-grained PAT) and `GITHUB_OWNER` to review GitHub PRs, or leave
both unset to disable it — setting one without the other is a startup
config error. `GITHUB_WEBHOOK_SECRET` is checked only at `serve` time:
missing it just skips mounting `/webhook/github` (logged warning); it
never stops the server. **PAT permissions gotcha:** posting a PR comment
uses `POST /issues/{n}/comments` but requires the fine-grained PAT's
**Pull requests: write** permission, not just **Issues: write**. With
only Issues: write, every write fails `403 "Resource not accessible by
personal access token"` while every read succeeds — which looks exactly
like a wrong-token or repo-access problem and isn't.

## Usage

    uv run hone review https://bitbucket.org/your-workspace/your-repo/pull-requests/7 --dry-run
    uv run hone review https://bitbucket.org/your-workspace/your-repo/pull-requests/7
    uv run hone review https://github.com/your-owner/your-repo/pull/7 --dry-run

The forge is picked from the URL; reviewing a GitHub PR requires
`GITHUB_TOKEN` + `GITHUB_OWNER` (see Setup). `--dry-run` runs the full
agent loop and prints the review; without it the loop's final
`post_review` call posts a comment on the PR.

## Server

    uv run hone serve

Long-running webhook receiver. Binds `127.0.0.1:8420` only — front it with
whatever reverse proxy and TLS termination you already run. Requires
`HONE_WEBHOOK_SECRET` in `.env` — the server refuses to start without
it (config error, not a bare crash). `/webhook` (Bitbucket) is always
mounted; `/webhook/github` is mounted only when GitHub support is fully
configured — both `GITHUB_TOKEN`/`GITHUB_OWNER` and `GITHUB_WEBHOOK_SECRET`
(see Setup).

### Webhook setup (Bitbucket)

The server is pinned to one workspace (`BITBUCKET_WORKSPACE`) but not to
one repo — any repo in that workspace works once it has both a webhook
(below) and an entry in `repos.toml` (static checks default to none, not
an error, if it's missing from `repos.toml`).

For each repo you want reviewed → **Repository settings → Webhooks → Add**:

- **URL:** `https://<your-host>/webhook`
- **Secret:** the value of `HONE_WEBHOOK_SECRET` in your deployment's `.env`
- **Events:** **Pull request created**, **Pull request updated**, and
  **Pull request comment created**

The server enforces HMAC-SHA256 over the raw body (`X-Hub-Signature`)
and pins the payload's workspace to `BITBUCKET_WORKSPACE`. A
signed-but-forged payload for another workspace is rejected with 200
(not 403) so Bitbucket doesn't redeliver.

### Webhook setup (GitHub)

When GitHub support is enabled (see Setup), for each repo →
**Settings → Webhooks → Add webhook**:

- **Payload URL:** `https://<your-host>/webhook/github`
- **Content type:** `application/json`
- **Secret:** the value of `GITHUB_WEBHOOK_SECRET` in your deployment's `.env`
- **Events:** **Pull requests** and **Issue comments**

Same guards as Bitbucket, reshaped for GitHub: HMAC-SHA256 over the raw
body (`X-Hub-Signature-256`), payload pinned to `GITHUB_OWNER` (the
equivalent of the workspace pin), and signed-but-foreign payloads
rejected with 200 (not 403) so GitHub doesn't redeliver. Without both
of those events subscribed, push-triggered reviews and the `@hone`
commands never fire on GitHub.

### Controlling hone (PR comments)

Comment on a PR (either forge) — the comment text must be exactly the
command (case and surrounding whitespace don't matter), and only the PR
author can issue one:

- `@hone mute` — stop reviewing this PR on push (hone replies with
  a hidden marker comment; the state lives on the forge, so it survives
  redeploys)
- `@hone unmute` — resume push-triggered reviews
- `@hone review` — force a review right now, bypassing mute and the
  same-head dedup marker

Reviews themselves are delta reviews: each round receives hone's
prior review comments and only reports findings that are new or changed
since the last reviewed head — "no new findings" is a valid, short
review.

### Tunnel note (local dev)

For local development without exposing a public URL, SSH-tunnel your
server's loopback to your laptop (`ssh -L 8420:127.0.0.1:8420 your-host`)
and register `https://<your-tunnel>/webhook` in Bitbucket — or
`https://<your-tunnel>/webhook/github` in GitHub; HMAC and
dedup both work end to end.

## Logs

`HONE_DATA_DIR/logs/hone.log` — rotating, 3×1 MB. Same line at INFO to
stderr.

## Tests

    uv run pytest              # offline — fixture git repo via file://

## Docs

- Design: `docs/prds/2026-09-17-pr-agent-design.md`

Deploying your own instance? Keep your host, deploy path, and any
rename/migration runbook in a gitignored local file (same treatment as
`.env`) rather than in tracked docs.
