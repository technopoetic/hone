# hone

Autonomous Bitbucket Cloud PR reviewer — an agent-building experiment.
Raw Anthropic SDK pointed at Synthetic.new, six investigation tools on a
throwaway git worktree, one comment per review. No frameworks.

Formerly `pr-agent` — renamed to avoid colliding with Qodo's open-source
project of the same name.

## Setup

    uv sync
    cp .env.example .env   # fill in SYNTHETIC_API_KEY, BITBUCKET_EMAIL,
                           # BITBUCKET_API_KEY, and BITBUCKET_WORKSPACE

`BITBUCKET_API_KEY` = the same value as your `ATLASSIAN_API_KEY`.
Requires git and `ripgrep` (`rg`) on PATH. Static-check binaries run from
hone's own environment (never the target repo's) — `ruff`/`mypy` come
from the `uv`-managed venv; `eslint` needs `npm install` once at the project
root (see `package.json`). Which tools run against which repo is config,
not code — see `repos.toml`.

## Usage

    uv run hone review https://bitbucket.org/your-workspace/your-repo/pull-requests/7 --dry-run
    uv run hone review https://bitbucket.org/your-workspace/your-repo/pull-requests/7

`--dry-run` runs the full agent loop and prints the review; without it the
loop's final `post_review` call posts a comment on the PR.

## Server

    uv run hone serve

Long-running webhook receiver. Binds `127.0.0.1:8420` only — front it with
whatever reverse proxy and TLS termination you already run. Requires
`HONE_WEBHOOK_SECRET` in `.env` — the server refuses to start without
it (config error, not a bare crash).

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

### Controlling hone (PR comments)

Comment on a PR — the comment text must be exactly the command (case and
surrounding whitespace don't matter), and only the PR author can issue
one:

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
and register `https://<your-tunnel>/webhook` in Bitbucket — HMAC and
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
