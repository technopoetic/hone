# GitLab forge — Design

Date: 2026-11-21
Status: implemented
Builds on: `docs/prds/2026-11-05-bitbucket-optional-design.md`

## Goal

Add GitLab as a third forge with the same review capabilities as Bitbucket
and GitHub:

- `hone review <merge-request-url>` works against GitLab.com and one
  configurable GitLab Self-Managed instance.
- `hone serve` accepts authenticated GitLab webhooks for automatic reviews.
- `@hone mute`, `@hone unmute`, and `@hone review` work through merge-request
  comments.
- Existing marker-based mute state, same-head deduplication, delta reviews,
  local git mirrors, and dry-run behavior are preserved.
- `hone review` works for every configured forge without requiring
  credentials for any other forge. Bitbucket, GitHub, or GitLab may each be
  the only configured forge.

The implementation also executes the already-approved Bitbucket-optional
spec. That is a prerequisite for GitLab-only deployments, not a separate new
design direction.

## Non-goals

- Multiple GitLab instances or multiple pinned webhook namespaces in one
  deployment.
- A generic forge registry or a rewrite of the existing `Forge` protocol.
- Inline/diff review comments. Reviews and commands use ordinary notes.
- GitLab's legacy plaintext `X-Gitlab-Token` authentication.
- Backward-compatible aliases for the old Bitbucket environment variable or
  webhook route.
- Docker, packaging, publishing, or deployment-specific configuration.

## External GitLab contracts

The design uses these documented interfaces:

- Merge Requests API, including `/merge_requests/:iid` and
  `/merge_requests/:iid/diffs`:
  <https://docs.gitlab.com/api/merge_requests>
- Notes API, including merge-request note listing and creation:
  <https://docs.gitlab.com/api/notes>
- Offset pagination through `X-Next-Page`:
  <https://docs.gitlab.com/api/rest#pagination>
- Merge request and comment webhook payloads:
  <https://docs.gitlab.com/user/project/integrations/webhook_events>
- Standard Webhooks signing tokens and signature verification:
  <https://docs.gitlab.com/user/project/integrations/webhooks>
- Merge-request head refs, including fork-origin merge requests:
  <https://docs.gitlab.com/user/project/merge_requests/merge_request_troubleshooting>
- Personal access token authentication for API and Git over HTTPS:
  <https://docs.gitlab.com/user/profile/personal_access_tokens>

## Configuration

### Forge groups

All forge credential groups are optional and all-or-nothing. At least one
complete group is required.

| Group | Required together | Optional modifiers |
|---|---|---|
| Bitbucket | `BITBUCKET_EMAIL`, `BITBUCKET_API_KEY`, `BITBUCKET_WORKSPACE` | `BITBUCKET_USERNAME`, `BITBUCKET_WEBHOOK_SECRET` |
| GitHub | `GITHUB_TOKEN`, `GITHUB_OWNER` | `GITHUB_WEBHOOK_SECRET` |
| GitLab | `GITLAB_TOKEN`, `GITLAB_URL`, `GITLAB_NAMESPACE` | `GITLAB_WEBHOOK_SIGNING_TOKEN` |

`BITBUCKET_USERNAME` continues to default to `BITBUCKET_EMAIL` when
Bitbucket is enabled. `HONE_WEBHOOK_SECRET` is renamed to
`BITBUCKET_WEBHOOK_SECRET`; its `Settings` field is renamed from
`webhook_secret` to `bitbucket_webhook_secret`. No old-name alias remains.

`GITLAB_URL` is explicit rather than defaulted. Examples:

```text
GITLAB_URL=https://gitlab.com
GITLAB_URL=https://git.example.com
GITLAB_URL=https://git.example.com/gitlab
```

Trailing slashes are normalized away. Retaining an optional relative URL
path allows self-managed installations hosted below the domain root.
`GITLAB_NAMESPACE` is a full user/group path such as `engineering` or
`engineering/platform`; webhook matching includes its descendant subgroups.

The signing-token variable is checked only at serve time. CLI reviews do not
require webhook credentials. Every other existing tool variable retains its
current meaning.

### Settings loading

`load_settings` applies these rules before constructing any client:

1. `LLM_API_KEY` and `LLM_MODEL` remain mandatory.
2. A partial Bitbucket, GitHub, or GitLab group raises `ConfigError` naming
   the whole group and explaining that all members must be set together or
   all left unset.
3. No complete forge group raises `ConfigError` explaining that at least one
   of the three groups must be configured.
4. Enabled groups populate their existing/new `Settings` fields; disabled
   groups populate `None` fields.
5. `repos.toml`, static-check, model, data-directory, port, and turn-limit
   behavior does not change.

### CLI review dispatch

Settings load before URL dispatch so GitLab recognition can use the
configured base URL. `parse_pr_url` accepts an optional GitLab base URL and
continues to return `(forge, namespace, repo, pr_id)`.

- Existing Bitbucket and GitHub URL forms are unchanged.
- GitLab accepts
  `<GITLAB_URL>/<namespace>/<project>/-/merge_requests/<iid>`, including
  nested namespace segments.
- A GitLab URL on a different host or base path is not a match.
- GitLab recognition is available when GitLab is configured; without a
  configured base URL there is no safe way to distinguish an arbitrary
  self-managed host from other web URLs.

After parsing, a URL whose forge is disabled exits 2 with a forge-specific
message naming its required environment group. A configured forge constructs
only its own client and mirror manager. In particular, GitLab-only review
requires no Bitbucket or GitHub values, and the same independence applies to
Bitbucket-only and GitHub-only reviews.

For GitLab, `namespace` contains every group/subgroup segment and `repo` is
the final project slug. The configured `GITLAB_NAMESPACE` pins webhooks; as
with the existing GitHub CLI behavior, a CLI user may review another
namespace on the same configured instance when their token can access it.

## GitLab API client

Add `src/hone/gitlab.py` with a `GitLabClient` satisfying the existing
structural `Forge` protocol. It takes the token, base URL, namespace, and an
optional injected `httpx.Client`. Normal HTTP behavior matches the other
clients: a 30-second timeout, redirects enabled, and `raise_for_status()` on
API failures.

### URL and authentication

The API root is `<GITLAB_URL>/api/v4`. Requests authenticate with
`PRIVATE-TOKEN: <GITLAB_TOKEN>`. The client forms a project endpoint by
URL-encoding the complete `<namespace>/<repo>` path with `quote(...,
safe="")`, so GitLab receives one encoded project identifier.

### `get_pull_request`

`GET /projects/:encoded-project/merge_requests/:iid` maps to `PullRequest`
as follows:

| `PullRequest` field | GitLab source |
|---|---|
| `id` | merge-request IID, not global ID |
| `title` | `title` |
| `description` | `description` or empty string |
| `author` | `author.username`, with the existing unknown fallback |
| `source_branch` | `source_branch` |
| `source_commit` | `sha` |
| `dest_branch` | `target_branch` |
| `dest_commit` | `diff_refs.start_sha`, or empty while GitLab has not populated diff refs |
| `source_ref` | `refs/merge-requests/<iid>/head` |

`source_ref` is a new optional field at the end of the shared dataclass,
defaulting to `None`; existing Bitbucket/GitHub constructors need no new
value. An empty destination SHA is acceptable during GitLab's asynchronous
diff-ref initialization because review merging uses `dest_branch` and the
source SHA, not `dest_commit`.

### `get_diffstat`

Use `GET /projects/:encoded-project/merge_requests/:iid/diffs`, not the
deprecated `/changes` endpoint. Follow offset pagination using
`X-Next-Page`, with page size 100.

For every returned diff:

- Path is `new_path`, falling back to `old_path` when needed.
- Status is `added` for `new_file`, `removed` for `deleted_file`, `renamed`
  for `renamed_file`, and `modified` otherwise.
- Added and removed totals are computed from the patch's content lines,
  excluding unified-diff `+++` and `---` headers.
- A collapsed, oversized, or otherwise omitted patch still contributes its
  file entry and status, with zero available line totals. The local git
  worktree remains the source of actual review content.

### `get_comments` and `post_comment`

List notes from
`GET /projects/:encoded-project/merge_requests/:iid/notes` with page size
100, `order_by=created_at`, and `sort=asc`. Explicit ascending order is
required because hone's marker protocol treats the final marker as latest;
GitLab's default note order is descending.

Return bodies in oldest-first order, following `X-Next-Page`. Exclude:

- system notes;
- internal notes;
- diff-attached notes (entries carrying a diff `position`).

This retains ordinary merge-request notes and general discussion notes while
excluding inline review threads, matching hone's flat-comment scope.
Hone-authored review and control markers are ordinary notes and therefore
remain visible to this method.

Post reviews and control comments with
`POST /projects/:encoded-project/merge_requests/:iid/notes` and JSON
`{"body": markdown}`.

## Mirrors, source refs, and nested groups

Each GitLab deployment gets an isolated mirror subtree under
`HONE_DATA_DIR/gitlab`, parallel to GitHub's subtree. Construct the clone URL
as `<GITLAB_URL>/<full-namespace>/<repo>.git`. Git authentication uses
username `oauth2` and the access token as its password through the existing
per-command Basic `http.extraHeader`. Tokens never enter remote URLs or git
config.

A full GitLab namespace can contain slashes. `RepoManager.ensure_mirror`
therefore creates nested mirror directories naturally, and
`sweep_stale_worktrees` changes from one fixed `*/*.git` level to recursive
mirror discovery beneath its own `mirrors` root. It retains all existing
prune, path-boundary, per-mirror warning, and removal behavior.

`RepoManager.ensure_mirror` accepts optional extra source refs. After its
normal branch clone/fetch, it force-fetches each supplied ref to the same
local ref name with the same credential mechanism. `run_review` passes
`PullRequest.source_ref` when present.

GitLab's target project exposes `refs/merge-requests/<iid>/head` even when
the source project is a fork. Fetching that ref makes `source_commit`
available before worktree creation. This supports same-project and
fork-origin merge requests without cloning the fork, accepting a
payload-supplied source URL, or changing the source/destination diff logic.
The existing per-review flow then continues unchanged: merge-base against
the target branch, detached worktree at source SHA, agent review, cleanup.

`repos.toml` remains keyed by repo slug and continues to control static
checks independently of the forge.

## Webhook server

### Mounting and startup

`create_app` becomes symmetric for all three forges and keeps `/health`
unconditional:

```python
def create_app(
    enqueue,
    *,
    bitbucket_secret: str | None = None,
    bitbucket_workspace: str | None = None,
    github_secret: str | None = None,
    github_owner: str | None = None,
    gitlab_signing_token: str | None = None,
    gitlab_namespace: str | None = None,
) -> FastAPI:
    ...
```

- `/webhook/bitbucket` mounts only with its secret and workspace.
- `/webhook/github` mounts only with its secret and owner.
- `/webhook/gitlab` mounts only with its signing token and namespace.
- An unconfigured forge has no route and returns 404.

The CLI `_serve` and `serve` defense-in-depth checks both enforce the same
rule: if zero forge routes can mount, startup fails. A single-forge
deployment missing its secret produces that forge's specific configuration
error; a multi-forge deployment with no usable secrets identifies all
missing signing secrets. If at least one route can mount, every other
enabled forge missing its secret is skipped with a logged warning rather
than stopping healthy forges.

The only new GitLab serve-specific error is equivalent to:

```text
GITLAB_WEBHOOK_SIGNING_TOKEN is required for serve
```

Everything else follows the approved optional-Bitbucket serve-gating matrix.

### GitLab signature verification

GitLab webhook serving requires GitLab 19.1 or newer, where Standard
Webhooks signing tokens are generally available. Plain `X-Gitlab-Token` is
never accepted as a fallback.

Verify these headers together:

- `webhook-id`
- `webhook-timestamp`
- `webhook-signature`

The timestamp must be an integer Unix timestamp within five minutes of the
server clock in either direction. This bounds replay while tolerating normal
clock skew. Missing, malformed, stale, or excessively future timestamps fail
closed.

Decode the configured signing token by removing `whsec_` and base64-decoding
the remainder. Construct the signed bytes as the ASCII
`<webhook-id>.<webhook-timestamp>.` prefix followed by the raw request body.
Compute HMAC-SHA256 with the decoded key and base64-encode the digest.

`webhook-signature` can contain multiple space-separated entries. Split them
and accept any `v1,<base64>` entry matching the computed signature with a
constant-time comparison. Ignore unsupported signature versions. Any absent
or malformed key material, message ID, timestamp, or signature returns 403;
none may cause a 500. Authentication precedes JSON parsing and event
handling.

### Event handling

Recognize `X-Gitlab-Event` values `Merge Request Hook` and `Note Hook`.
Other events are ignored with 200 after valid authentication.

For both event types, read the target project from
`project.path_with_namespace` and split it at the last slash into namespace
and project slug. Accept only the configured namespace or a descendant whose
path begins `<GITLAB_NAMESPACE>/`. Other projects fail soft with 200, as on
the existing forges. No URL from the payload selects a clone host.

#### Merge Request Hook

Read the IID from `object_attributes.iid`. Enqueue
`ReviewRequest(workspace=namespace, repo=slug, pr_id=iid, forge="gitlab")`
only for actions:

- `open`
- `reopen`
- `update`

Other actions, including close, merge, and approval actions, are ignored.
An `update` can represent metadata rather than a new commit; enqueuing is
intentional because the existing pre-mirror marker scan cheaply skips an
unchanged head without an LLM call.

#### Note Hook

Accept only notes satisfying all of these conditions:

- `object_attributes.action == "create"`;
- `object_attributes.noteable_type == "MergeRequest"`;
- not a system note;
- not an internal note;
- not diff-attached: `object_attributes.position` is absent and
  `object_attributes.type` is not `DiffNote`.

Read IID from `merge_request.iid`, comment text from
`object_attributes.note`, actor ID from `object_attributes.author_id`, and
merge-request author ID from `merge_request.author_id`. Pass these unchanged
through the existing `_route_command` with `forge="gitlab"`.

The shared command invariants do not change:

- the stripped/case-folded note must exactly equal a supported command;
- only the merge-request author's stable numeric ID may issue a command;
- missing identities fail closed;
- comments containing hone's marker sentinel never trigger commands;
- comment text is data, never an LLM instruction.

A valid command enqueues its normal `ReviewRequest` or `CommandRequest`.
Other notes are ignored with 200. Edited notes cannot re-fire commands.
Authenticated malformed payloads fail soft with 200, while enqueue failures
remain unhandled 500s so the forge can retry delivery.

### Shared behavior that remains unchanged

- `ReviewRequest.forge` and `CommandRequest.forge` keep their Bitbucket
  defaults; every webhook and CLI creator sets its forge explicitly.
- `build_process` dispatches on `req.forge`, now including `gitlab`, and the
  server remains live-only.
- One serial worker queue handles all configured forges.
- HMAC validation, namespace pinning, fail-soft foreign payloads, dry-run
  enforcement, marker state, and delta reviews retain their existing
  semantics on Bitbucket and GitHub.
- The accepted eslint execution risk and all static-check allowlists are
  outside this change.

## Testing

All automated verification remains offline under `uv run pytest`.

### GitLab client

Add `tests/test_gitlab.py` with injected HTTP fixtures covering:

- encoded full project paths and API base paths, including self-managed
  relative URL paths;
- every `PullRequest` field, absent description/author behavior, empty diff
  refs, and IID-based source refs;
- multiple diff pages, every status mapping, patch line counts, renamed and
  deleted paths, binary/collapsed/oversized patches, and HTTP failures;
- ascending note parameters, multi-page ordering, filtering of system,
  internal, and diff-attached notes, and note posting;
- pagination termination when `X-Next-Page` is empty or absent.

### Repository behavior

Extend repository/review tests to cover:

- recursive discovery and sweeping of mirrors under nested group paths;
- preserving the existing rejection of worktrees outside the job root;
- fetching an extra merge-request ref into both new and existing mirrors;
- a source commit reachable only through a merge-request ref, demonstrating
  the fork-origin mechanism with fixture repositories;
- no extra fetch for Bitbucket/GitHub pull requests whose `source_ref` is
  `None`.

### Configuration and CLI

Extend configuration and CLI tests for:

- each partial forge group and the no-forge error;
- each forge working alone;
- Bitbucket username defaulting and the secret field rename;
- GitLab.com, self-managed hosts, relative URL paths, nested groups, and
  trailing-slash normalization;
- wrong-host, wrong-base-path, malformed, and non-MR GitLab URLs;
- configured-forge dispatch and unconfigured-forge exit-2 messages;
- server client/mirror construction only for enabled groups;
- `build_process` and command dispatch with `forge="gitlab"`;
- all zero-route startup failures and partial-secret warning/start cases.

### Server

Extend server tests for:

- valid Standard Webhooks signatures, multiple signatures, unsupported
  versions, malformed tokens/headers, incorrect signatures, stale
  timestamps, and future-skewed timestamps;
- unmounted 404s and unconditional health checks;
- valid open/reopen/update requests, ignored actions and event types;
- project paths with nested groups, descendant acceptance, exact-boundary
  matching, and foreign-project 200s;
- note creation versus edit, noteable type, system/internal/diff filtering;
- exact commands, wrong/missing actor IDs, author equality, forced reviews,
  mute/unmute commands, and hone-marker self-suppression;
- malformed authenticated payloads versus enqueue failures.

Existing Bitbucket/GitHub tests are updated for optional configuration and
route/signature renames only. Their event and command semantics remain
covered unchanged.

## Documentation and migration

Update:

- `.env.example` with three independent forge groups and the new names;
- `README.md` with GitLab review/setup examples, all-forges-optional
  behavior, serve gating, token scopes, webhook events, and routes;
- `pyproject.toml` descriptions to include GitLab;
- `AGENTS.md` so its current architecture, serve invariants, route names,
  trust boundaries, and Phase 4 status describe the implemented three-forge
  system rather than the pre-GitLab state.

GitLab setup documents a personal access token with `api` and
`read_repository` scopes. The webhook uses `/webhook/gitlab`, a GitLab 19.1+
signing token, merge-request events, and comment events. Older self-managed
versions can use CLI review but cannot use hone's authenticated GitLab
webhook route.

The approved one-time Bitbucket migration remains:

1. Rename `HONE_WEBHOOK_SECRET` to `BITBUCKET_WEBHOOK_SECRET`.
2. Change each subscribed Bitbucket webhook from `/webhook` to
   `/webhook/bitbucket`.
3. Deploy the new build.

No compatibility route or environment alias is added. Instance-specific
hosts, namespaces, credentials, and deployment paths stay in gitignored
local files.

## Touch points

- New: `src/hone/gitlab.py`, `tests/test_gitlab.py`
- Shared forge/review/repository: `src/hone/forge.py`, `src/hone/review.py`,
  `src/hone/repo.py`, `tests/test_repo.py`, `tests/test_review.py`
- Configuration and dispatch: `src/hone/config.py`, `src/hone/cli.py`,
  `tests/test_config.py`, `tests/test_cli.py`
- Webhooks: `src/hone/server.py`, `tests/test_server.py`
- Documentation: `.env.example`, `README.md`, `pyproject.toml`, `AGENTS.md`

No changes are intended to `markers.py`, `prompts.py`, the agent loop, static
check execution, or `repos.toml`.
