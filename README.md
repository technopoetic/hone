# hone

Autonomous PR/MR review agent for Bitbucket Cloud, GitHub, and GitLab — an
agent-building experiment.
Raw Anthropic SDK, `base_url` pointed at any Anthropic-Messages-API-
compatible endpoint (Synthetic.new, Anthropic directly, a self-hosted
proxy), six investigation tools on a throwaway git worktree, one comment
per review. No frameworks.

Formerly `pr-agent` — renamed to avoid colliding with Qodo's open-source
project of the same name.

## Setup

    uv sync
    cp .env.example .env   # fill in LLM_API_KEY and LLM_MODEL, plus ONE
                           # complete forge group — Bitbucket, GitHub,
                           # or GitLab credentials

Every forge group is optional but all-or-nothing, and at least one must
be complete: partial groups are a startup config error. `hone review`
uses only the forge matched by the URL — reviewing a GitLab merge
request needs no Bitbucket or GitHub credentials at all, and vice versa.

**Bitbucket** — set `BITBUCKET_EMAIL`, `BITBUCKET_API_KEY`, and
`BITBUCKET_WORKSPACE`. `BITBUCKET_API_KEY` = the same value as your
`ATLASSIAN_API_KEY`. `BITBUCKET_USERNAME` is optional — git-over-HTTPS
authenticates with a username, not an email address; it defaults to
`BITBUCKET_EMAIL`.

**GitHub** — set `GITHUB_TOKEN` (a fine-grained PAT) and `GITHUB_OWNER`
together, or leave both unset. **PAT permissions gotcha:** posting a PR
comment uses `POST /issues/{n}/comments` but requires the fine-grained
PAT's **Pull requests: write** permission, not just **Issues: write**.
With only Issues: write, every write fails `403 "Resource not accessible
by personal access token"` while every read succeeds — which looks
exactly like a wrong-token or repo-access problem and isn't.

**GitLab** — set `GITLAB_TOKEN`, `GITLAB_URL` (e.g.
`https://gitlab.com`, or your self-managed base including any relative
URL path), and `GITLAB_NAMESPACE` (a full user/group path such as
`engineering` or `group/subgroup`; nested groups work). One GitLab
instance and one pinned namespace are supported per deployment. The
token must be a PAT with `api` and `read_repository` scopes — `api` for
reading and posting merge-request notes, `read_repository` for cloning
and fetching through the local mirror.

Requires git and `ripgrep` (`rg`) on PATH. Static-check binaries run
from hone's own environment (never the target repo's) — `ruff`/`mypy`
come from the `uv`-managed venv; `eslint` needs `npm install` once at
the project root (see `package.json`). Which tools run against which
repo is config, not code — see `repos.toml`.

## Usage

    uv run hone review https://bitbucket.org/your-workspace/your-repo/pull-requests/7 --dry-run
    uv run hone review https://github.com/your-owner/your-repo/pull/7 --dry-run
    uv run hone review https://gitlab.com/your-group/your-project/-/merge_requests/7 --dry-run
    uv run hone review https://git.example.com/gitlab/your-group/subgroup/your-project/-/merge_requests/7 --dry-run

The forge is picked from the URL — GitLab URLs are recognized only
against the configured `GITLAB_URL`, and review uses only that forge's
credentials (see Setup). `--dry-run` runs the full agent loop and prints
the review; without it the loop's final `post_review` call posts a
comment on the PR/MR.

## Server

    uv run hone serve

Long-running webhook receiver. Binds `127.0.0.1:8420` only — front it
with whatever reverse proxy and TLS termination you already run. Each
route mounts only when its forge's credentials AND webhook secret are
both configured:

- `/webhook/bitbucket` — needs the Bitbucket group +
  `BITBUCKET_WEBHOOK_SECRET`
- `/webhook/github` — needs `GITHUB_TOKEN`/`GITHUB_OWNER` +
  `GITHUB_WEBHOOK_SECRET`
- `/webhook/gitlab` — needs the GitLab group +
  `GITLAB_WEBHOOK_SIGNING_TOKEN`

`serve` refuses to start when no route can mount (config error, not a
bare crash — a server with no authenticated route is not a mode, it's a
hole). When at least one route is healthy, an otherwise-enabled forge
missing only its secret just gets skipped with a logged warning naming
the unmounted route.

### Webhook setup (Bitbucket)

The server is pinned to one workspace (`BITBUCKET_WORKSPACE`) but not
to one repo — any repo in that workspace works once it has both a
webhook (below) and an entry in `repos.toml` (static checks default to
none, not an error, if it's missing from `repos.toml`).

For each repo you want reviewed → **Repository settings → Webhooks →
Add**:

- **URL:** `https://<your-host>/webhook/bitbucket`
- **Secret:** the value of `BITBUCKET_WEBHOOK_SECRET` in your
  deployment's `.env`
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
- **Secret:** the value of `GITHUB_WEBHOOK_SECRET` in your deployment's
  `.env`
- **Events:** **Pull requests** and **Issue comments**

Same guards as Bitbucket, reshaped for GitHub: HMAC-SHA256 over the raw
body (`X-Hub-Signature-256`), payload pinned to `GITHUB_OWNER` (the
equivalent of the workspace pin), and signed-but-foreign payloads
rejected with 200 (not 403) so GitHub doesn't redeliver. Without both
of those events subscribed, push-triggered reviews and the `@hone`
commands never fire on GitHub.

### Webhook setup (GitLab)

When GitLab support is enabled (see Setup), for each project (or the
group) → **Settings → Webhooks**:

- **URL:** `https://<your-host>/webhook/gitlab`
- **Secret token:** the value of `GITLAB_WEBHOOK_SIGNING_TOKEN` in your
  deployment's `.env` — a GitLab 19.1+ signing token with the `whsec_`
  prefix
- **Events:** **Merge request events** and **Comment events**
  (Note Hook)

Authentication is Standard Webhooks, not a shared-secret comparison:
GitLab 19.1+ signs the raw body with HMAC-SHA256 keyed by the decoded
`whsec_` token, spread across `Webhook-Id`, `Webhook-Timestamp`, and
`Webhook-Signature` headers, and the server rejects deliveries whose
timestamp is more than five minutes stale. Legacy secret tokens
(`X-Gitlab-Token` authentication) are not accepted — an older GitLab
instance or a plain secret value cannot authenticate to this route.

The route pins payloads to the configured `GITLAB_NAMESPACE`; projects
in that namespace and in any subgroup under it are accepted, everything
else is ignored (200, not 403) so GitLab doesn't redeliver. Merge
requests opened from a fork work without extra setup: hone fetches the
MR head through the target project's merge-request refs
(`refs/merge-requests/<iid>/head`), so it never needs access to the
fork itself.

### Migration (pre-2026-11 deployments)

The old Bitbucket-only names are gone, and no aliases exist:

- rename `HONE_WEBHOOK_SECRET` → `BITBUCKET_WEBHOOK_SECRET` in `.env`
- update the Bitbucket webhook URL from `/webhook` →
  `/webhook/bitbucket`

### Controlling hone (PR/MR comments)

Comment on a PR or merge request (any of the three forges) — the
comment text must be exactly the command (case and surrounding
whitespace don't matter), and only the PR/MR author can issue one:

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
and register your tunnel URL with the route path for each forge you use
— `https://<your-tunnel>/webhook/bitbucket` (Bitbucket),
`https://<your-tunnel>/webhook/github` (GitHub), or
`https://<your-tunnel>/webhook/gitlab` (GitLab); HMAC and dedup both
work end to end.

## Logs

`HONE_DATA_DIR/logs/hone.log` — rotating, 3×1 MB. Same line at INFO to
stderr.

## Tests

    uv run pytest              # offline — fixture git repo via file://

Covers all three forges (Bitbucket, GitHub, GitLab) plus config, CLI,
markers, prompts, repo/mirror handling, review orchestration, server,
and tools.

## Docs

- Original design: `docs/prds/2026-09-17-pr-agent-design.md`
- GitLab forge design: `docs/prds/2026-11-21-gitlab-forge-design.md`

Deploying your own instance? Keep your host, deploy path, and any
rename/migration runbook in a gitignored local file (same treatment as
`.env`) rather than in tracked docs.
