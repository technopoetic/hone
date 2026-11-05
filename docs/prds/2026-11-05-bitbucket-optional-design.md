# Optional Bitbucket — Design (forge symmetry)

Date: 2026-11-05
Status: approved in conversation, pending implementation
Supersedes: the "GitHub is the optional forge" assumption threaded
through `AGENTS.md` and `docs/prds/2026-09-17-pr-agent-design.md`
(that PRD's own "Phases" section is stale past Phase 2; where the two
documents disagree, this one and `AGENTS.md` win).

## Goal

A user configuring **only GitHub** — no `BITBUCKET_*` env vars at all —
can run both `hone review` and `hone serve` against their own instance,
correctly gated with clean startup errors for anything they left
half-configured. This is motivated by a real user being onboarded
(GitHub-only deployment), not anticipation.

Bitbucket stops being "the platform" and becomes "a forge": an optional
group symmetric with the existing GitHub group. Two renames make the
symmetry visible in config and routes:

- `HONE_WEBHOOK_SECRET` → `BITBUCKET_WEBHOOK_SECRET` (the secret is
  forge-scoped; `HONE_` is reserved for tool-scoped vars like
  `HONE_PORT`).
- Route `POST /webhook` → `POST /webhook/bitbucket` (the bare path is a
  fossil from when Bitbucket was the only forge).

## Non-goals

- Multi-owner / multi-workspace support — one owner per deployment,
  explicitly parked.
- Gitea or any third forge (but the shapes here are what a third
  `Forge` module would slot into).
- Backward-compat aliases of any kind: no old env var name, no old
  route. Both controlled deployments are known (one live, one
  greenfield); the migration section below is the whole story.
- Docker/packaging/publishing. Users run from a clone with `uv sync`.

## Decisions

1. **Mirror the existing all-or-nothing group pattern.** Phase 4
   already established it for GitHub (`GITHUB_TOKEN` + `GITHUB_OWNER`).
   The Bitbucket trio gets identical treatment. No new mechanism
   (an explicit `HONE_FORGES` flag was rejected: it re-encodes state
   the env vars already carry and must itself be validated), and no
   lazy fail-at-use-site gating (rejected on `config.py`'s stated
   contract: missing secrets are a *startup* error, not something to
   discover mid-review).
2. **At least one forge must be fully configured.** A credential-less
   hone has nothing to review against.
3. **Secret leniency is symmetric per forge:** an enabled forge missing
   its webhook secret downgrades to route-not-mounted with a logged
   warning **if any other route still mounts**; if no route would
   mount, `serve` refuses to start. (Generalizes today's asymmetric
   rule under which the GitHub secret was always optional.)
4. **Unconfigured forge routes 404.** Not 422 (nothing is being
   validated), not 403 (would leak that the route exists-but-
   disabled; unauthenticated probers learn nothing). Zero extra code —
   unmounted FastAPI routes already 404 — and miswired webhooks show
   as red deliveries in the forge's webhook panel, surfacing the
   error at the sender instead of silently 200-ing.
5. **Clean breaks, no transition aliases.** Mounting both `/webhook`
   and `/webhook/bitbucket` would be a second permanent
   unauthenticated public edge for zero benefit.

## Configuration

### Env var groups

| Group | Vars | Rule |
|---|---|---|
| Tool | `LLM_API_KEY`, `LLM_MODEL` | always required |
| Tool, optional | `LLM_BASE_URL`, `HONE_*` | unchanged semantics |
| Bitbucket | `BITBUCKET_EMAIL`, `BITBUCKET_API_KEY`, `BITBUCKET_WORKSPACE` | all set or all unset |
| Bitbucket, optional modifier | `BITBUCKET_USERNAME` | outside the group; defaults to `BITBUCKET_EMAIL` when itself unset |
| Bitbucket | `BITBUCKET_WEBHOOK_SECRET` | checked only at serve time (see Serve gating) |
| GitHub | `GITHUB_TOKEN`, `GITHUB_OWNER` | all set or all unset |
| GitHub | `GITHUB_WEBHOOK_SECRET` | checked only at serve time |
| Forge floor | — | at least one forge group fully set |

### `load_settings` / `Settings`

- LLM vars required as today (missing → the existing
  "Missing required env vars: …" error).
- Bitbucket trio: any subset set → `ConfigError`
  *"BITBUCKET_EMAIL, BITBUCKET_API_KEY, and BITBUCKET_WORKSPACE must
  be set together (or all left unset to disable Bitbucket support)"*
  (mirrors the existing GitHub-pair message verbatim in shape).
- Both groups unset → `ConfigError` *"no forge configured: set
  Bitbucket (BITBUCKET_EMAIL, BITBUCKET_API_KEY, BITBUCKET_WORKSPACE)
  and/or GitHub (GITHUB_TOKEN, GITHUB_OWNER)"*.
- `Settings` field types: `bitbucket_email`, `bitbucket_api_key`,
  `bitbucket_workspace`, `bitbucket_username` become `str | None`;
  `bitbucket_username = BITBUCKET_USERNAME or bitbucket_email`.
- Field rename: `Settings.webhook_secret` →
  `Settings.bitbucket_webhook_secret` (symmetric with
  `github_webhook_secret`, and it's the BITBUCKET_WEBHOOK_SECRET
  value). All readers updated; no deprecation shim.

## CLI `review`

`parse_pr_url` is unchanged. Client construction already builds only
the URL-matched forge's client, so the change is gating errors only.
The existing GitHub check (`cli.py`: "GITHUB_TOKEN and GITHUB_OWNER
are required to review a GitHub PR") stays; add its mirror:

> Bitbucket URL + Bitbucket group unset → exit 2:
> *"BITBUCKET_EMAIL, BITBUCKET_API_KEY, and BITBUCKET_WORKSPACE are
> required to review a Bitbucket PR"*.

A GitHub-only deployment therefore has a fully working `review`
against GitHub PRs with nothing Bitbucket present anywhere.

## Serve gating

Per enabled forge, at process start:

| Deployment | Secret state | Result |
|---|---|---|
| Bitbucket-only | `BITBUCKET_WEBHOOK_SECRET` missing | refuse to start (exit 2 config error from `cli._serve`, `SystemExit` from `serve()` as defense in depth) |
| GitHub-only | `GITHUB_WEBHOOK_SECRET` missing | refuse to start (same shape; **new** — today's server would mount zero routes and idle) |
| Both | one secret missing | boot; the secret-less forge's route unmounted with a logged warning; the other forge serves normally (today's lenient behavior, now symmetric) |
| Neither (impossible via `load_settings`, but `serve()` keeps the check) | — | refuse |

The strict single-forge refusal is the "zero routes would mount" rule
from Decision 3; the both-forges leniency means one typo'd secret
never takes down reviews for the other forge. Exact refusal messages,
mirroring the existing `_serve` style:

- Bitbucket-only: *"error: BITBUCKET_WEBHOOK_SECRET is required for
  serve"*
- GitHub-only: *"error: GITHUB_WEBHOOK_SECRET is required for serve"*

Implementation shape:

- `cli._serve()` constructs `forges` / `repo_managers` entries
  conditionally per enabled group (Bitbucket client + RepoManager only
  when the group is set; GitHub as today), and enforces the
  error-message-bearing checks above (exit 2, `ConfigError` text
  style).
- `serve()` keeps `SystemExit` checks mirroring the same rules —
  it is the defense-in-depth layer and must not depend on the CLI
  having run its checks.
- `create_app` becomes fully symmetric:

```python
def create_app(enqueue, *,
               bitbucket_secret: str | None = None,
               bitbucket_workspace: str | None = None,
               github_secret: str | None = None,
               github_owner: str | None = None) -> FastAPI
```

  `/webhook/bitbucket` mounts only when both Bitbucket args are given
  — the same "no entry = off" condition `/webhook/github` has today.
  `/health` mounts unconditionally.
- Route bodies, HMAC checks, event sets, command gating, workspace /
  owner pinning: **byte-for-byte unchanged** — the Bitbucket handler
  just moves under the new path (Bitbucket still sends
  `X-Hub-Signature`; GitHub still sends `X-Hub-Signature-256`).

## What deliberately does not change

- `ReviewRequest.forge` / `CommandRequest.forge` default of
  `"bitbucket"`: requests only arrive via a mounted route, and every
  internal creator sets `forge` explicitly.
- Both forges' trust boundaries: literal-match commands, author-only
  gate, fail-closed identities, sentinel skip of hone's own comments,
  HMAC over raw bytes, namespace pinning, 200-fail-soft on foreign
  namespaces, 403 on bad signature.
- Delta reviews, marker protocol, dedup, mute state.
- One-owner-per-deployment. Multi-org stays parked; a deployment
  serves one Bitbucket workspace and/or one GitHub owner.
- `repos.toml` static-check allowlist per deployment.
- `ReviewJob.dry_run` gating (serve path stays live-only).

## AGENTS.md — the normative rewrite

This spec *is* the "spec change" the trust-boundary section requires.
Concretely:

- The `HONE_WEBHOOK_SECRET` paragraph: rename throughout, and the
  invariant *"never refuses to start the server (which still has to
  serve Bitbucket)"* becomes *"never refuses to start the server while
  another forge still serves; a single-forge deployment refuses
  without that forge's secret."*
- "GitHub support is optional and all-or-nothing" → both forges
  optional and all-or-nothing; at least one required.
- Webhook route names in the trust-boundary and inbound-auth sections:
  `/webhook/bitbucket` + `/webhook/github`.

## Documentation

- `.env.example`: regrouped to present the two forge blocks side by
  side, Bitbucket trio commented as a group, rename applied,
  `BITBUCKET_USERNAME` labeled as the optional email-alias.
- `README.md`: Setup presents two independent optional blocks
  ("configure Bitbucket, GitHub, or both — at least one"); a new
  short "GitHub-only deployment" path showing the minimal five-var
  `.env` (`LLM_API_KEY`, `LLM_MODEL`, `GITHUB_TOKEN`, `GITHUB_OWNER`,
  `GITHUB_WEBHOOK_SECRET`-when-serving); Server section states the
  mount conditions and the 404-on-unconfigured rule; webhook setup
  sections use the new route path; tunnel note updated.

## Migration for existing deployments

One deploy window, then done. (Instance detail belongs in each
deployment's gitignored local file, not tracked docs.)

1. `.env`: rename `HONE_WEBHOOK_SECRET` → `BITBUCKET_WEBHOOK_SECRET`.
   Value unchanged. Nothing on the forge settings pages changes from
   the rename itself.
2. Bitbucket, per onboarded repo: Repository settings → Webhooks →
   edit URL `https://<host>/webhook` →
   `https://<host>/webhook/bitbucket`.
3. Deploy. GitHub side untouched (`/webhook/github` and the GitHub env
   vars keep their names).
4. Until step 2 is done for a repo, its deliveries show red in
   Bitbucket's webhook panel — visible failure, not silent; review
   resumes the moment the URL is fixed. Same-head dedup markers mean
   nothing re-reviews just because the endpoint changed.
5. Greenfield (GitHub-only) deployments: nothing to migrate.

## Testing

All offline (`uv run pytest`), extending the existing patterns:

- `test_config.py`: partial Bitbucket trio → the all-or-nothing
  error naming all three vars; both groups unset → "no forge
  configured"; GitHub-only env (no `BITBUCKET_*`) loads with
  `bitbucket_* = None` and `bitbucket_username = None`; `bitbucket_*
  ` group with `BITBUCKET_USERNAME` set → alias applies; rename
  `HONE_WEBHOOK_SECRET` → `BITBUCKET_WEBHOOK_SECRET` in fixtures; all
  existing both-forges cases unchanged.
- `test_cli.py`: Bitbucket URL against GitHub-only settings → the
  mirror error, exit 2; `serve` GitHub-only without
  `GITHUB_WEBHOOK_SECRET` → refusal; `serve` GitHub-only with it →
  boots with no `BITBUCKET_*` present at all; existing
  `HONE_WEBHOOK_SECRET`-refusal test updated for the rename; existing
  both-forges serve behavior unchanged (missing one secret → warn +
  boot).
- `test_server.py`: `create_app` fixtures move to the new signature
  and `/webhook/bitbucket` path; app built with no Bitbucket args →
  `/webhook/bitbucket` 404 (unmounted) while `/health` 200s;
  symmetric coverage for the GitHub route; handler-behavior tests
  (HMAC, pins, events, commands) unchanged under the new path.

## Touch points (for the implementation plan)

`src/hone/config.py` (group logic, field renames/types),
`src/hone/cli.py` (mirror gate, conditional `_serve` construction),
`src/hone/server.py` (`create_app` signature, route path, symmetric
secret gating, message renames), `tests/test_config.py`,
`tests/test_cli.py`, `tests/test_server.py`, `.env.example`,
`README.md`, `AGENTS.md`. Not code, per-deployment: each deployment's
`.env` and Bitbucket webhook URLs (see Migration).
